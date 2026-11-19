"""AI 추천 성적표 — 추천 이력 저장(SQLite)과 수익률·적중률 계산.

설계 원칙
- 가볍게: 외부 DB 없이 SQLite 파일 하나. 경로는 `HISTORY_DB_PATH` 환경변수로 바꿀 수 있고,
  기본값은 `data/recommendations.db`(저장소 기준)이며, 그 디렉터리를 만들 수 없으면
  `/tmp/stock_scanner_history.db`로 떨어진다. 부모 디렉터리는 안전하게 만든다.
- 스크리닝을 망치지 않게: 모든 DB 함수는 예외를 그대로 올리되, `api.py`는 호출부에서
  감싸 경고만 남긴다. 저장이 실패해도 `/api/screen` 응답은 그대로 나간다.
- 같은 날 중복 금지: `(recommended_date, ticker, strategy)` UNIQUE. 같은 조건으로 하루에
  여러 번 분석해도 행은 하나이며, 처음 기록한 `entry_price`·`recommended_at`을 유지한다
  (진입가는 최초 추천 시점의 값이어야 수익률이 왜곡되지 않는다). 점수·mode·criteria만 갱신.
- 평가는 배치: 현재가는 `yf.download` 한 번(최대 50종목)으로 받아 5분 메모리 캐시한다.
  `t.info`는 절대 부르지 않는다(Yahoo 429/401 차단 회피, 응답 속도).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd
import yfinance as yf

logger = logging.getLogger("history_store")

# 한국 증시/사용자 기준 날짜. tzdata 없는 환경에서도 동작하도록 고정 오프셋을 쓴다.
KST = timezone(timedelta(hours=9))

DEFAULT_DB_PATH = os.path.join("data", "recommendations.db")
FALLBACK_DB_PATH = "/tmp/stock_scanner_history.db"

TABLE = "recommendations"

# 한 번에 평가할 최근 추천 수(성적표 기본값).
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 현재가 메모리 캐시 유효시간(초). 성적표를 연달아 새로고침해도 Yahoo를 다시 부르지 않는다.
PRICE_TTL = 300

# ticker -> (저장 시각, 가격 또는 None)
_PRICE_CACHE: dict[str, tuple[float, float | None]] = {}
_PRICE_LOCK = threading.Lock()

# SQLite 쓰기 직렬화(스레드풀에서 /api/screen이 동시에 들어올 수 있다).
_WRITE_LOCK = threading.Lock()

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT    NOT NULL,
    recommended_date TEXT    NOT NULL,          -- KST YYYY-MM-DD
    recommended_at   TEXT    NOT NULL,          -- ISO 8601 (KST 오프셋 포함)
    entry_price      REAL,
    strategy         TEXT    NOT NULL DEFAULT '',
    mode             TEXT    NOT NULL DEFAULT '',
    quant_score      REAL,
    criteria         TEXT    NOT NULL DEFAULT ''
);
"""

UNIQUE_INDEX = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{TABLE}_date_ticker_strategy "
    f"ON {TABLE} (recommended_date, ticker, strategy)"
)
DATE_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_recommended_at ON {TABLE} (recommended_at DESC)"
)


# --- 날짜/숫자 유틸 ----------------------------------------------------------


def kst_now() -> datetime:
    return datetime.now(KST)


def kst_today(now: datetime | None = None) -> date:
    return (now or kst_now()).astimezone(KST).date()


def _num(value: Any) -> float | None:
    """숫자로 바꿀 수 있으면 float, 아니면 None. NaN/불리언은 None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


# --- DB 연결 -----------------------------------------------------------------


def get_db_path() -> str:
    """사용할 SQLite 경로를 정한다(환경변수 > 저장소 data/ > /tmp 폴백).

    경로를 정하는 것 자체는 부작용이 없도록 디렉터리 생성은 `_connect`에서 한다.
    """
    env = (os.environ.get("HISTORY_DB_PATH") or "").strip()
    if env:
        return env
    return DEFAULT_DB_PATH


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _resolve_writable_path() -> str:
    """설정된 경로의 부모를 만들 수 없으면 /tmp로 대체한다."""
    path = get_db_path()
    try:
        _ensure_parent(path)
        return path
    except OSError as exc:
        if path == FALLBACK_DB_PATH:
            raise
        logger.warning("추천 이력 DB 경로 %s 사용 불가(%s) → %s 로 대체", path, exc, FALLBACK_DB_PATH)
        _ensure_parent(FALLBACK_DB_PATH)
        return FALLBACK_DB_PATH


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    """짧게 열고 닫는 연결. 호출마다 스키마를 보장한다(멱등)."""
    path = db_path or _resolve_writable_path()
    if db_path:
        _ensure_parent(path)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:  # 읽기 전용 FS 등 — 기본 모드로 계속
        pass
    conn.executescript(SCHEMA)
    for ddl in (UNIQUE_INDEX, DATE_INDEX):
        try:
            conn.execute(ddl)
        except sqlite3.DatabaseError as exc:
            # 예전 스키마에 중복 행이 있어 UNIQUE 인덱스를 못 만들 수 있다. 저장은 계속.
            logger.warning("추천 이력 인덱스 생성 실패(계속 진행): %s", exc)
    return conn


def init_db(db_path: str | None = None) -> str:
    """스키마를 만들거나 확인하고 실제 사용 경로를 돌려준다."""
    with _WRITE_LOCK:
        conn = _connect(db_path)
        try:
            conn.commit()
        finally:
            conn.close()
    return db_path or get_db_path()


def _clean(value: Any, default: str = "") -> str:
    return str(value).strip() if value is not None else default


# --- 기록 --------------------------------------------------------------------


def record_picks(
    picks: Iterable[dict],
    strategy: str = "",
    mode: str = "",
    criteria: str = "",
    recommended_date: str | None = None,
    recommended_at: str | None = None,
    db_path: str | None = None,
) -> dict:
    """추천 종목들을 이력에 남긴다.

    같은 `(recommended_date, ticker, strategy)`가 이미 있으면 새 행을 만들지 않고
    점수·mode·criteria만 갱신한다(첫 진입가·첫 추천 시각 유지).

    반환: {"inserted": n, "updated": n, "skipped": n, "db_path": path}
    """
    now = kst_now()
    if recommended_at:
        try:
            now = datetime.fromisoformat(recommended_at)
            now = now.astimezone(KST) if now.tzinfo else now.replace(tzinfo=KST)
        except ValueError:
            now = kst_now()
    day = recommended_date or now.strftime("%Y-%m-%d")
    strat = _clean(strategy)
    m = _clean(mode)
    crit = _clean(criteria)

    rows = []
    skipped = 0
    for pick in picks or []:
        if not isinstance(pick, dict):
            skipped += 1
            continue
        ticker = _clean(pick.get("ticker")).upper()
        if not ticker:
            skipped += 1
            continue
        rows.append((ticker, day, now.isoformat(timespec="seconds"),
                     _num(pick.get("entry_price", pick.get("price"))), strat, m,
                     _num(pick.get("quant_score")), crit))

    if not rows:
        return {"inserted": 0, "updated": 0, "skipped": skipped, "db_path": None}

    inserted = updated = 0
    with _WRITE_LOCK:
        conn = _connect(db_path)
        try:
            with conn:  # 트랜잭션
                for (ticker, d, at, entry, s, md, score, cr) in rows:
                    cur = conn.execute(
                        f"INSERT OR IGNORE INTO {TABLE} "
                        "(ticker, recommended_date, recommended_at, entry_price, "
                        " strategy, mode, quant_score, criteria) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ticker, d, at, entry, s, md, score, cr),
                    )
                    if cur.rowcount:
                        inserted += 1
                    else:
                        conn.execute(
                            f"UPDATE {TABLE} SET quant_score = ?, mode = ?, criteria = ? "
                            "WHERE recommended_date = ? AND ticker = ? AND strategy = ?",
                            (score, md, cr, d, ticker, s),
                        )
                        updated += 1
            path = conn.execute("PRAGMA database_list").fetchone()[2]
        finally:
            conn.close()
    logger.info("추천 이력 %s: 신규 %d · 갱신 %d · 제외 %d", day, inserted, updated, skipped)
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "db_path": path}


def recent_picks(limit: int = DEFAULT_LIMIT, db_path: str | None = None) -> list[dict]:
    """최신 추천부터 최대 limit개를 돌려준다(저장된 원본 필드 그대로)."""
    n = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"SELECT ticker, recommended_date, recommended_at, entry_price, strategy, "
            f"       mode, quant_score, criteria "
            f"FROM {TABLE} ORDER BY recommended_date DESC, id DESC LIMIT ?",
            (n,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --- 현재가 배치 조회 --------------------------------------------------------


def _extract_last_closes(df, tickers: list[str]) -> dict[str, float]:
    """yf.download 결과(단일/멀티 인덱스 컬럼 모두)에서 종목별 마지막 종가를 뽑는다."""
    out: dict[str, float] = {}
    if df is None or getattr(df, "empty", True):
        return out
    try:
        close = df["Close"]
    except (KeyError, TypeError):
        return out

    if isinstance(close, pd.DataFrame):  # 여러 종목 → 컬럼이 티커
        for ticker in tickers:
            if ticker not in close.columns:
                continue
            series = close[ticker].dropna()
            if len(series):
                out[ticker] = float(series.iloc[-1])
    else:  # 한 종목 → Series
        series = close.dropna()
        if len(series) and len(tickers) == 1:
            out[tickers[0]] = float(series.iloc[-1])
        elif len(series) and len(tickers) > 1:
            # columns가 티커 레벨로 접혀 있지 않은 드문 경우: 이름을 모르므로 버린다.
            logger.debug("단일 컬럼 시계열인데 종목이 %d개 → 가격 매칭 생략", len(tickers))
    return out


def _fetch_prices_batch(tickers: list[str]) -> dict[str, float]:
    """Yahoo에서 최근 5일 종가를 한 번에 받아 종목별 마지막 종가를 돌려준다.

    `t.info`는 부르지 않는다. 실패하면 빈 dict(예외를 밖으로 던지지 않는다).
    """
    syms = [t for t in tickers if t]
    if not syms:
        return {}
    try:
        df = yf.download(
            tickers=syms if len(syms) > 1 else syms[0],
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=len(syms) > 1,
            group_by="column",
        )
    except Exception as exc:  # 429/401/네트워크 모두 흡수
        logger.warning("현재가 배치 조회 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        return {}
    return _extract_last_closes(df, syms)


def latest_prices(tickers: Iterable[str], ttl: int = PRICE_TTL,
                  fetcher=None) -> dict[str, float | None]:
    """5분 메모리 캐시를 끼고 현재가를 돌려준다. 없는 종목은 None.

    `fetcher`는 테스트·대체 경로용 주입점이며 기본은 `_fetch_prices_batch`다
    (호출 시점에 해석하므로 monkeypatch로 바꿔도 반영된다).
    """
    fetcher = fetcher or _fetch_prices_batch
    want = []
    for t in tickers:
        sym = _clean(t).upper()
        if sym and sym not in want:
            want.append(sym)
    if not want:
        return {}

    now = time.time()
    out: dict[str, float | None] = {}
    missing: list[str] = []
    with _PRICE_LOCK:
        for sym in want:
            cached = _PRICE_CACHE.get(sym)
            if cached and now - cached[0] < ttl:
                out[sym] = cached[1]
            else:
                missing.append(sym)

    if missing:
        fresh = fetcher(missing) or {}
        with _PRICE_LOCK:
            for sym in missing:
                price = _num(fresh.get(sym))
                # 조회 실패(None)도 캐시해 장 마감·일시 장애 때 매번 재시도하지 않는다.
                _PRICE_CACHE[sym] = (now, price)
                out[sym] = price

    # 캐시에만 있던 값도 최신 상태로 채운다.
    return {sym: out.get(sym) for sym in want}


def clear_price_cache() -> None:
    with _PRICE_LOCK:
        _PRICE_CACHE.clear()


# --- 성적표 ------------------------------------------------------------------


def empty_performance(limit: int = DEFAULT_LIMIT, error: str | None = None) -> dict:
    """이력이 없거나 계산이 실패했을 때의 응답(키 모양은 항상 같다)."""
    summary = {
        "total_picks": 0,
        "evaluated_picks": 0,
        "win_count": 0,
        "win_rate_pct": None,
        "avg_return_pct": None,
        "best_pick": None,
        "worst_pick": None,
    }
    body = {"summary": summary, "picks": [], "as_of": kst_now().isoformat(timespec="seconds"),
            "limit": limit}
    if error:
        body["error"] = error
    return body


def _evaluate(pick: dict, current_price: float | None, today: date) -> dict:
    entry = _num(pick.get("entry_price"))
    price = _num(current_price)
    return_pct = None
    if entry and entry > 0 and price is not None and price > 0:
        return_pct = round((price - entry) / entry * 100, 2)

    days_held = None
    try:
        rec_date = datetime.strptime(str(pick.get("recommended_date")), "%Y-%m-%d").date()
        days_held = max(0, (today - rec_date).days)
    except (TypeError, ValueError):
        days_held = None

    return {
        "ticker": pick.get("ticker"),
        "recommended_date": pick.get("recommended_date"),
        "recommended_at": pick.get("recommended_at"),
        "entry_price": None if entry is None else round(entry, 4),
        "current_price": None if price is None else round(price, 4),
        "return_pct": return_pct,
        "is_win": None if return_pct is None else return_pct > 0,
        "days_held": days_held,
        "strategy": pick.get("strategy") or "",
        "mode": pick.get("mode") or "",
        "quant_score": _num(pick.get("quant_score")),
    }


def _summary(rows: list[dict]) -> dict:
    evaluated = [r for r in rows if r["return_pct"] is not None]
    win_count = sum(1 for r in evaluated if r["is_win"])
    win_rate = round(win_count / len(evaluated) * 100, 1) if evaluated else None
    avg = round(sum(r["return_pct"] for r in evaluated) / len(evaluated), 2) if evaluated else None

    def _brief(row):
        return {
            "ticker": row["ticker"],
            "recommended_date": row["recommended_date"],
            "entry_price": row["entry_price"],
            "current_price": row["current_price"],
            "return_pct": row["return_pct"],
        }

    best = _brief(max(evaluated, key=lambda r: r["return_pct"])) if evaluated else None
    worst = _brief(min(evaluated, key=lambda r: r["return_pct"])) if evaluated else None
    return {
        "total_picks": len(rows),
        "evaluated_picks": len(evaluated),
        "win_count": win_count,
        "win_rate_pct": win_rate,
        "avg_return_pct": avg,
        "best_pick": best,
        "worst_pick": worst,
    }


def compute_performance(picks: list[dict], prices: dict[str, float | None],
                        today: date | None = None) -> dict:
    """추천 원본 행 + 현재가 → 성적표(픽별 수익률·승패 + 요약). 네트워크 없음."""
    day = today or kst_today()
    rows = [_evaluate(p, (prices or {}).get(_clean(p.get("ticker")).upper()), day)
            for p in picks or []]
    return {"summary": _summary(rows), "picks": rows}


def get_performance(limit: int = DEFAULT_LIMIT, db_path: str | None = None,
                    price_fetcher=None) -> dict:
    """최근 추천을 읽어 현재가로 평가한 성적표를 만든다.

    DB가 비었거나 읽을 수 없으면 `empty_performance`(0/None)를 돌려준다.
    """
    n = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    try:
        picks = recent_picks(n, db_path=db_path)
    except Exception as exc:
        logger.warning("추천 이력 조회 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        return empty_performance(n, error="history_unavailable")

    if not picks:
        body = empty_performance(n)
        body["picks"] = []
        return body

    try:
        prices = latest_prices([p["ticker"] for p in picks],
                               fetcher=price_fetcher or _fetch_prices_batch)
    except Exception as exc:  # 가격 조회가 죽어도 이력은 보여준다(수익률만 null)
        logger.warning("현재가 조회 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        prices = {}
        body = compute_performance(picks, prices)
        body["error"] = "price_unavailable"
        body["as_of"] = kst_now().isoformat(timespec="seconds")
        body["limit"] = n
        return body

    body = compute_performance(picks, prices)
    body["as_of"] = kst_now().isoformat(timespec="seconds")
    body["limit"] = n
    return body
