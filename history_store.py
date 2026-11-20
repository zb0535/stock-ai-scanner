"""AI 추천 성적표 — 추천 이력 저장(SQLite 또는 외부 PostgreSQL)과 수익률·적중률 계산.

설계 원칙
- **사용자가 고른 종목만 기록한다**: `/api/screen`은 이력을 남기지 않고, 클라이언트가
  `POST /api/performance/track`으로 픽 하나를 지정해 추적을 시작한다. `DELETE
  /api/performance/{id}`로 행 하나만 지운다. 성적표 조회(`GET /api/performance`)는 픽마다
  `id`(정수 PK)를 포함해 개별 삭제가 가능하다.
- 선택적 외부 DB: `DATABASE_URL`(postgres:// 또는 postgresql://)이 설정돼 있으면 PostgreSQL에
  기록한다(Neon·Supabase·Render Postgres 호환). 설정이 없으면 예전처럼 SQLite를 쓴다.
- 가볍게(로컬 폴백): SQLite 경로는 `HISTORY_DB_PATH` 환경변수로 바꿀 수 있고,
  기본값은 `data/recommendations.db`(저장소 기준)이며, 그 디렉터리를 만들 수 없으면
  `/tmp/stock_scanner_history.db`로 떨어진다. 부모 디렉터리는 안전하게 만든다.
- 스크리닝을 망치지 않게: 모든 DB 함수는 예외를 그대로 올리되, `api.py`는 호출부에서
  감싸 경고만 남긴다. 저장이 실패해도 `/api/screen` 응답은 그대로 나간다.
- 같은 날 중복 금지: `(recommended_date, ticker, strategy)` UNIQUE. 같은 조건으로 하루에
  여러 번 추적해도 행은 하나이며, 처음 기록한 `entry_price`·`recommended_at`을 유지한다
  (진입가는 최초 추천 시점의 값이어야 수익률이 왜곡되지 않는다). 점수·mode·criteria만 갱신.
- 평가는 배치: 현재가는 `yf.download` 한 번(최대 50종목)으로 받아 5분 메모리 캐시한다.
  `t.info`는 절대 부르지 않는다(Yahoo 429/401 차단 회피, 응답 속도).
- 장중에는 일봉 종가 대신 **실시간 현재가**(한국 네이버·미국 Finnhub)를 우선 쓴다.
  실시간 조회에 실패한 종목만 일봉 종가로 채우고, 픽마다 `price_source`를 남긴다.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import yfinance as yf

import naver_finance
import realtime_quotes

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


# --- 저장 백엔드 선택: PostgreSQL(DATABASE_URL) / SQLite(기본) -------------------

DATABASE_URL_ENV = "DATABASE_URL"

# 외부 DB 연결에만 쓰는 PostgreSQL 스키마. 컬럼 이름·의미·제약은 SQLite 스키마와 같다.
# recommended_date/recommended_at은 두 백엔드 모두 TEXT(ISO)로 저장해 조회 결과가 달라지지 않는다.
PG_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id               SERIAL PRIMARY KEY,
    ticker           TEXT    NOT NULL,
    recommended_date TEXT    NOT NULL,          -- KST YYYY-MM-DD
    recommended_at   TEXT    NOT NULL,          -- ISO 8601 (KST 오프셋 포함)
    entry_price      DOUBLE PRECISION,
    strategy         TEXT    NOT NULL DEFAULT '',
    mode             TEXT    NOT NULL DEFAULT '',
    quant_score      DOUBLE PRECISION,
    criteria         TEXT    NOT NULL DEFAULT '',
    CONSTRAINT ux_{TABLE}_date_ticker_strategy UNIQUE (recommended_date, ticker, strategy)
)
"""

PG_DATE_INDEX = (
    f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_recommended_at ON {TABLE} (recommended_at DESC)"
)

# 같은 (날짜, 티커, 전략)은 한 행. DO UPDATE에서 entry_price·recommended_at을 건드리지 않으므로
# 최초 진입가·최초 추천 시각이 유지된다(SQLite 경로와 동일한 규칙).
# RETURNING (xmax = 0)은 이번 문장이 INSERT였는지(True) UPDATE였는지(False)를 알려준다.
PG_UPSERT = f"""
INSERT INTO {TABLE} (ticker, recommended_date, recommended_at, entry_price,
                    strategy, mode, quant_score, criteria)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (recommended_date, ticker, strategy) DO UPDATE
SET quant_score = EXCLUDED.quant_score,
    mode        = EXCLUDED.mode,
    criteria    = EXCLUDED.criteria
RETURNING (xmax = 0) AS inserted
"""

# 조회·삭제가 함께 쓰는 컬럼 목록. `id`(정수 PK)를 항상 포함해야 클라이언트가 행을 개별 삭제할 수 있다.
PICK_COLUMNS = ("id, ticker, recommended_date, recommended_at, entry_price, strategy, "
                "mode, quant_score, criteria")

PG_SELECT = (
    f"SELECT {PICK_COLUMNS} FROM {TABLE} "
    f"ORDER BY recommended_date DESC, id DESC LIMIT %s"
)

PG_SELECT_ONE = (
    f"SELECT {PICK_COLUMNS} FROM {TABLE} "
    f"WHERE recommended_date = %s AND ticker = %s AND strategy = %s LIMIT 1"
)

# RETURNING id — 지운 행이 없으면 결과가 비어 404 판단에 쓴다.
PG_DELETE = f"DELETE FROM {TABLE} WHERE id = %s RETURNING id"

# 외부 DB 연결 타임아웃(초). 무료 티어가 잠들었다 깨는 시간을 넉넉히 준다.
PG_CONNECT_TIMEOUT = 10

# 한 프로세스에서 스키마를 이미 보장한 접속 문자열(비밀번호 없는 정규화 URL은 아니고 원문).
_PG_SCHEMA_READY: set[str] = set()
_PG_SCHEMA_LOCK = threading.Lock()

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


def normalize_database_url(raw: Any = None) -> str:
    """`DATABASE_URL`을 libpq가 이해하는 접속 문자열로 정리한다(순수 함수).

    - 앞뒤 공백 제거. 비어 있으면 ""(→ SQLite 폴백).
    - `postgres://` → `postgresql://` (Heroku·일부 대시보드가 주는 옛 스킴).
    - 두 스킴이 아니면 "" — 실수로 다른 URL이 들어와도 SQLite로 계속 동작한다.
    - sslmode가 없고 원격 호스트면 `sslmode=require`를 붙인다(Neon·Supabase는 SSL 필수,
      Render Postgres도 SSL을 받는다). 로컬(localhost·127.0.0.1·::1)에는 붙이지 않는다.
    - 질의 문자열은 문자열로만 다뤄 비밀번호의 % 인코딩을 건드리지 않는다.
    """
    url = str(raw).strip() if raw is not None else (os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not url:
        return ""

    lowered = url.lower()
    if lowered.startswith("postgresql://"):
        pass
    elif lowered.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    else:
        return ""

    url = url.rstrip("?&")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    names = [p.split("=", 1)[0].strip().lower() for p in parts.query.split("&") if p.strip()]
    if "sslmode" in names or host in _LOCAL_HOSTS:
        return url
    return f"{url}{'&' if parts.query else '?'}sslmode=require"


def database_url() -> str:
    """현재 설정된 PostgreSQL 접속 문자열(정규화본). 없으면 ""."""
    return normalize_database_url()


def storage_backend(db_path: str | None = None) -> str:
    """이번 호출이 쓰는 저장소: "postgres" 또는 "sqlite".

    `db_path`를 명시하면(테스트·로컬 파일 지정) DATABASE_URL과 무관하게 SQLite다.
    접속하지 않고 환경변수만 읽으므로 헬스 체크에서 불러도 I/O가 없다.
    """
    if db_path:
        return "sqlite"
    return "postgres" if database_url() else "sqlite"


def redact_database_url(raw: Any = None) -> str:
    """비밀번호를 지운 접속 문자열(로그·응답용). 자격 증명은 밖으로 나가지 않는다."""
    url = normalize_database_url(raw)
    if not url:
        return ""
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostpart = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{hostpart}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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


def _pg_connect(url: str | None = None):
    """PostgreSQL 연결(짧게 열고 닫는다). 첫 연결에서 스키마를 멱등하게 만든다.

    psycopg는 여기서 늦게 import한다 — psycopg가 없는 환경에서도 SQLite 경로는 그대로 돈다.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - requirements.txt에 포함
        raise RuntimeError("psycopg가 설치되지 않아 PostgreSQL 백엔드를 쓸 수 없습니다") from exc

    conninfo = normalize_database_url(url) if url else database_url()
    if not conninfo:
        raise RuntimeError("DATABASE_URL이 설정되지 않았습니다")

    conn = psycopg.connect(conninfo, connect_timeout=PG_CONNECT_TIMEOUT, row_factory=dict_row,
                           prepare_threshold=None)  # PgBouncer(Neon pooler) 연결 모드에서도 안전하게
    try:
        _ensure_pg_schema(conn, conninfo)
    except BaseException:
        conn.close()
        raise
    return conn


def _ensure_pg_schema(conn, key: str) -> None:
    """PostgreSQL 테이블·인덱스를 없으면 만든다(프로세스당 한 번)."""
    if key in _PG_SCHEMA_READY:
        return
    with _PG_SCHEMA_LOCK:
        if key in _PG_SCHEMA_READY:
            return
        conn.execute(PG_SCHEMA)
        conn.execute(PG_DATE_INDEX)
        conn.commit()
        _PG_SCHEMA_READY.add(key)


def init_db(db_path: str | None = None) -> str:
    """스키마를 만들거나 확인하고 실제 사용 경로를 돌려준다."""
    if storage_backend(db_path) == "postgres":
        with _WRITE_LOCK:
            conn = _pg_connect()
            try:
                conn.commit()
            finally:
                conn.close()
        return redact_database_url()
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


def _build_rows(
    picks: Iterable[dict],
    strategy: str = "",
    mode: str = "",
    criteria: str = "",
    recommended_date: str | None = None,
    recommended_at: str | None = None,
) -> tuple[list[tuple], str, int]:
    """픽 목록을 저장용 행 튜플로 바꿉니다(백엔드 공통).

    행 튜플 순서는 `ticker, recommended_date, recommended_at, entry_price, strategy,
    mode, quant_score, criteria` — SQLite INSERT와 PostgreSQL INSERT가 같은 순서를 쓴다.
    반환: (행 목록, 기준 날짜(YYYY-MM-DD), 제외한 픽 수)
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
    return rows, day, skipped


def _record_picks_pg(rows: list[tuple], day: str, skipped: int) -> dict:
    """PostgreSQL upsert. `(recommended_date, ticker, strategy)` 충돌 시 점수·mode·criteria만 갱신."""
    inserted = updated = 0
    with _WRITE_LOCK:
        conn = _pg_connect()
        try:
            for row in rows:
                cur = conn.execute(PG_UPSERT, row)
                res = cur.fetchone()
                if res and res.get("inserted"):
                    inserted += 1
                else:
                    updated += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    logger.info("추천 이력(postgres) %s: 신규 %d · 갱신 %d · 제외 %d", day, inserted, updated, skipped)
    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "db_path": redact_database_url(), "backend": "postgres", "date": day}


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
    `DATABASE_URL`이 있으면 PostgreSQL, 없으면 SQLite(`db_path` 지정 시 강제 SQLite).

    반환: {"inserted": n, "updated": n, "skipped": n, "db_path": path, "backend": ...}
    """
    rows, day, skipped = _build_rows(picks, strategy=strategy, mode=mode, criteria=criteria,
                                     recommended_date=recommended_date,
                                     recommended_at=recommended_at)

    if not rows:
        return {"inserted": 0, "updated": 0, "skipped": skipped, "db_path": None,
                "backend": storage_backend(db_path), "date": day}

    if storage_backend(db_path) == "postgres":
        return _record_picks_pg(rows, day, skipped)

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
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "db_path": path,
            "backend": "sqlite", "date": day}


def recent_picks(limit: int = DEFAULT_LIMIT, db_path: str | None = None) -> list[dict]:
    """최신 추천부터 최대 limit개를 돌려준다(저장된 원본 필드 그대로)."""
    n = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    if storage_backend(db_path) == "postgres":
        conn = _pg_connect()
        try:
            return [dict(r) for r in conn.execute(PG_SELECT, (n,)).fetchall()]
        finally:
            conn.close()
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"SELECT {PICK_COLUMNS} FROM {TABLE} "
            f"ORDER BY recommended_date DESC, id DESC LIMIT ?",
            (n,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --- 수동 추적(개별 추가)·개별 삭제 -------------------------------------------


def _lookup_pick(ticker: str, day: str, strategy: str,
                 db_path: str | None = None) -> dict | None:
    """`(날짜, 티커, 전략)` 한 행을 `id`까지 붙여 돌려준다(없으면 None)."""
    if storage_backend(db_path) == "postgres":
        conn = _pg_connect()
        try:
            row = conn.execute(PG_SELECT_ONE, (day, ticker, strategy)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"SELECT {PICK_COLUMNS} FROM {TABLE} "
            "WHERE recommended_date = ? AND ticker = ? AND strategy = ? LIMIT 1",
            (day, ticker, strategy),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record_pick(
    ticker: str,
    entry_price: Any = None,
    strategy: str = "",
    mode: str = "",
    criteria: str = "",
    quant_score: Any = None,
    recommended_date: str | None = None,
    recommended_at: str | None = None,
    db_path: str | None = None,
) -> dict:
    """종목 한 개를 **사용자가 직접** 추적 목록에 넣는다(`POST /api/performance/track`).

    `record_picks`와 같은 테이블·같은 중복 규칙을 쓰므로, 같은 날 같은 `(티커, 전략)`을 다시
    추적하면 행이 늘지 않고 기존 행(첫 진입가·첫 시각)이 그대로 돌아온다.
    반환: `{"ok": True, "created": bool, "pick": {id, ticker, ...}, "inserted", "updated"}`
    """
    sym = _clean(ticker).upper()
    if not sym:
        raise ValueError("ticker_required")
    out = record_picks(
        [{"ticker": sym, "entry_price": entry_price, "quant_score": quant_score}],
        strategy=strategy, mode=mode, criteria=criteria,
        recommended_date=recommended_date, recommended_at=recommended_at, db_path=db_path,
    )
    pick = _lookup_pick(sym, out["date"], _clean(strategy), db_path) if out["inserted"] or out["updated"] else None
    return {"ok": pick is not None, "created": out["inserted"] > 0, "pick": pick,
            "inserted": out["inserted"], "updated": out["updated"],
            "date": out["date"], "backend": out["backend"]}


def delete_pick(pick_id: Any, db_path: str | None = None) -> bool:
    """`id`가 일치하는 행 **하나만** 지운다. 지운 행이 없으면 False(→ 404).

    정수로 해석되지 않거나 1 미만인 값은 DB를 건드리지 않고 False다.
    """
    try:
        pid = int(pick_id)
    except (TypeError, ValueError):
        return False
    if isinstance(pick_id, bool) or pid <= 0:
        return False

    if storage_backend(db_path) == "postgres":
        with _WRITE_LOCK:
            conn = _pg_connect()
            try:
                found = conn.execute(PG_DELETE, (pid,)).fetchone() is not None
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        if found:
            logger.info("추천 이력 삭제(postgres): id=%s", pid)
        return found

    with _WRITE_LOCK:
        conn = _connect(db_path)
        try:
            with conn:
                cur = conn.execute(f"DELETE FROM {TABLE} WHERE id = ?", (pid,))
                found = cur.rowcount > 0
        finally:
            conn.close()
    if found:
        logger.info("추천 이력 삭제: id=%s", pid)
    return found


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


def _overlay_realtime(prices: dict[str, float | None], tickers: list[str]
                      ) -> tuple[dict[str, float | None], dict[str, str]]:
    """일봉 종가 위에 실시간 현재가를 덮어쓴다(실패한 종목은 종가 유지).

    반환: (가격 dict, 티커별 price_source dict). 예외는 모두 흡수해 성적표를 죽이지 않는다.
    """
    merged = dict(prices or {})
    sources: dict[str, str] = {}
    try:
        quotes = realtime_quotes.get_prices(tickers)
    except Exception as exc:  # 실시간 계층이 죽어도 종가는 그대로 보여준다
        logger.warning("성적표 실시간 시세 실패: %s: %s", type(exc).__name__, str(exc)[:160])
        quotes = {}
    for sym, quote in (quotes or {}).items():
        price = _num(quote.get("price"))
        if price is None or price <= 0:
            continue
        merged[sym] = price
        sources[sym] = realtime_quotes.source_of(quote)
    for sym, price in merged.items():
        if sym not in sources and _num(price) is not None:
            sources[sym] = realtime_quotes.SOURCE_DAILY
    return merged, sources


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
            "limit": limit, "storage_backend": storage_backend()}
    if error:
        body["error"] = error
    return body


def _evaluate(pick: dict, current_price: float | None, today: date,
              price_source: str | None = None) -> dict:
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
        "id": pick.get("id"),
        "ticker": pick.get("ticker"),
        "name": naver_finance.static_display_name(pick.get("ticker")),
        "currency": naver_finance.currency_for(pick.get("ticker")),
        "recommended_date": pick.get("recommended_date"),
        "recommended_at": pick.get("recommended_at"),
        "entry_price": None if entry is None else round(entry, 4),
        "current_price": None if price is None else round(price, 4),
        "price_source": price_source if price is not None else None,
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
            "name": row.get("name"),
            "currency": row.get("currency"),
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
                        today: date | None = None,
                        sources: dict[str, str] | None = None) -> dict:
    """추천 원본 행 + 현재가 → 성적표(픽별 수익률·승패 + 요약). 네트워크 없음.

    `sources`는 티커별 현재가 출처(`naver_realtime`/`finnhub_quote`/`daily_close`)다.
    없으면 `daily_close`로 본다(기존 호출부 호환).
    """
    day = today or kst_today()
    by_source = {_clean(k).upper(): v for k, v in (sources or {}).items()}
    rows = [_evaluate(p, (prices or {}).get(_clean(p.get("ticker")).upper()), day,
                      by_source.get(_clean(p.get("ticker")).upper()))
            for p in picks or []]
    return {"summary": _summary(rows), "picks": rows}


def _tag_backend(body: dict, db_path: str | None = None) -> dict:
    """응답에 이번 조회가 실제로 쓴 저장소를 붙인다(자격 증명은 넣지 않는다)."""
    body["storage_backend"] = storage_backend(db_path)
    return body


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
        return _tag_backend(empty_performance(n, error="history_unavailable"), db_path)

    if not picks:
        body = empty_performance(n)
        body["picks"] = []
        return _tag_backend(body, db_path)

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
        return _tag_backend(body, db_path)

    # 장중 현재가: 최신 일봉 종가 위에 실시간 시세(네이버·Finnhub)를 덮어쓴다.
    # `price_fetcher`를 주입한 호출(테스트·대체 경로)에는 적용하지 않는다.
    sources: dict[str, str] = {}
    if price_fetcher is None:
        prices, sources = _overlay_realtime(prices, [p["ticker"] for p in picks])

    body = compute_performance(picks, prices, sources=sources)
    body["as_of"] = kst_now().isoformat(timespec="seconds")
    body["limit"] = n
    return _tag_backend(body, db_path)
