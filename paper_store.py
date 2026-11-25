"""AI 모의투자(페이퍼 트레이딩) — 계좌·보유 포지션·체결·수익률 스냅샷 저장소.

설계 원칙
- **통화는 원화(KRW) 하나로 통일한다**: 미국·한국 종목을 한 계좌에서 굴리므로, 시드머니·
  현금·총 자산·수익률은 모두 `₩`다. 미국 종목의 `avg_price`·`price`는 달러로 남기고
  (`currency="USD"`), 매수 시점의 USD/KRW 환율(`fx_rate`)을 포지션에 함께 저장해 원화 원가
  (`cost_krw`)를 고정한다. 평가는 **현재 환율**로 다시 계산한다 — 환율 변동도 수익률에 들어간다.
- **기존 성적표와 분리**: `history_store`의 `recommendations` 테이블은 그대로 두고,
  `paper_*` 테이블 네 개를 새로 만든다(`performance` 화면은 건드리지 않는다).
- **저장 백엔드는 성적표와 같은 규칙**: `DATABASE_URL`(PostgreSQL/Neon)이 있으면 그쪽에,
  없으면 SQLite(`HISTORY_DB_PATH`, 기본 `data/recommendations.db`)에 쓴다. 연결·정규화
  유틸은 `history_store`를 그대로 재사용해 두 저장소가 같은 DB 파일·같은 SSL 규칙을 쓴다.
- **현금은 절대 음수가 되지 않는다**: 매수 금액은 호출부(`paper_trading`)가 잔여 현금으로
  검증해 넘기지만, 저장 단계에서도 음수 현금을 막는다(`_guard_cash`).
- 금액은 float(원, 소수점 반올림)로 저장한다. 화면 표시는 정수 원 단위로 반올림한다.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import history_store

logger = logging.getLogger("paper_store")

TABLE_ACCOUNT = "paper_account"
TABLE_POSITIONS = "paper_positions"
TABLE_TRADES = "paper_trades"
TABLE_EQUITY = "paper_equity_history"
TABLE_FCM_TOKENS = "paper_fcm_tokens"

ACCOUNT_ID = 1

# 기본 시드머니 — 원화 1,000만 원(한국·미국 통합 계좌).
DEFAULT_INITIAL_CASH_KRW = 10_000_000.0
MIN_INITIAL_CASH_KRW = 100_000.0          # 10만 원
MAX_INITIAL_CASH_KRW = 1_000_000_000.0    # 10억 원

MARKETS = ("ALL", "US", "KR")
DEFAULT_MARKETS = "ALL"

STRATEGIES = ("momentum", "value", "oversold", "balanced")
DEFAULT_STRATEGY = "balanced"
STRATEGY_LABELS = {
    "momentum": "모멘텀(상승 추세)",
    "value": "가치(저PER)",
    "oversold": "과매도 반등",
    "balanced": "균형",
}

_SQLITE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ACCOUNT} (
    id               INTEGER PRIMARY KEY,
    initial_cash_krw REAL    NOT NULL,
    cash_krw         REAL    NOT NULL,
    markets          TEXT    NOT NULL DEFAULT 'ALL',
    strategy         TEXT    NOT NULL DEFAULT 'balanced',
    auto_enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_date    TEXT,
    last_run_us_date TEXT,
    last_run_kr_date TEXT,
    created_at       TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS {TABLE_POSITIONS} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT    NOT NULL UNIQUE,
    market     TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    currency   TEXT    NOT NULL DEFAULT 'USD',
    shares     REAL    NOT NULL,
    avg_price  REAL    NOT NULL,
    fx_rate    REAL    NOT NULL DEFAULT 1,
    cost_krw   REAL    NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    opened_at  TEXT
);

CREATE TABLE IF NOT EXISTS {TABLE_TRADES} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT,
    trade_date        TEXT    NOT NULL,
    traded_at         TEXT    NOT NULL,
    ticker            TEXT    NOT NULL,
    market            TEXT    NOT NULL,
    name              TEXT    NOT NULL DEFAULT '',
    side              TEXT    NOT NULL,
    shares            REAL    NOT NULL,
    price             REAL    NOT NULL,
    currency          TEXT    NOT NULL DEFAULT 'USD',
    fx_rate           REAL    NOT NULL DEFAULT 1,
    amount_krw        REAL    NOT NULL,
    fee_krw           REAL    NOT NULL DEFAULT 0,
    realized_pnl_krw  REAL,
    reason            TEXT    NOT NULL DEFAULT '',
    ai_source         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS {TABLE_EQUITY} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL UNIQUE,
    total_value_krw REAL    NOT NULL,
    cash_krw        REAL    NOT NULL,
    stock_value_krw REAL    NOT NULL,
    return_pct      REAL,
    recorded_at     TEXT
);

CREATE TABLE IF NOT EXISTS {TABLE_FCM_TOKENS} (
    token      TEXT PRIMARY KEY,
    platform   TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
"""

# PostgreSQL: AUTOINCREMENT 대신 SERIAL, REAL 대신 DOUBLE PRECISION.
_PG_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ACCOUNT} (
    id               INTEGER PRIMARY KEY,
    initial_cash_krw DOUBLE PRECISION NOT NULL,
    cash_krw         DOUBLE PRECISION NOT NULL,
    markets          TEXT    NOT NULL DEFAULT 'ALL',
    strategy         TEXT    NOT NULL DEFAULT 'balanced',
    auto_enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_date    TEXT,
    last_run_us_date TEXT,
    last_run_kr_date TEXT,
    created_at       TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS {TABLE_POSITIONS} (
    id         SERIAL PRIMARY KEY,
    ticker     TEXT    NOT NULL UNIQUE,
    market     TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    currency   TEXT    NOT NULL DEFAULT 'USD',
    shares     DOUBLE PRECISION NOT NULL,
    avg_price  DOUBLE PRECISION NOT NULL,
    fx_rate    DOUBLE PRECISION NOT NULL DEFAULT 1,
    cost_krw   DOUBLE PRECISION NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    opened_at  TEXT
);

CREATE TABLE IF NOT EXISTS {TABLE_TRADES} (
    id               SERIAL PRIMARY KEY,
    run_id           TEXT,
    trade_date       TEXT    NOT NULL,
    traded_at        TEXT    NOT NULL,
    ticker           TEXT    NOT NULL,
    market           TEXT    NOT NULL,
    name             TEXT    NOT NULL DEFAULT '',
    side             TEXT    NOT NULL,
    shares           DOUBLE PRECISION NOT NULL,
    price            DOUBLE PRECISION NOT NULL,
    currency         TEXT    NOT NULL DEFAULT 'USD',
    fx_rate          DOUBLE PRECISION NOT NULL DEFAULT 1,
    amount_krw       DOUBLE PRECISION NOT NULL,
    fee_krw          DOUBLE PRECISION NOT NULL DEFAULT 0,
    realized_pnl_krw DOUBLE PRECISION,
    reason           TEXT    NOT NULL DEFAULT '',
    ai_source        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS {TABLE_EQUITY} (
    id              SERIAL PRIMARY KEY,
    date            TEXT    NOT NULL UNIQUE,
    total_value_krw DOUBLE PRECISION NOT NULL,
    cash_krw        DOUBLE PRECISION NOT NULL,
    stock_value_krw DOUBLE PRECISION NOT NULL,
    return_pct      DOUBLE PRECISION,
    recorded_at     TEXT
);

CREATE TABLE IF NOT EXISTS {TABLE_FCM_TOKENS} (
    token      TEXT PRIMARY KEY,
    platform   TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
"""

_WRITE_LOCK = threading.Lock()
_PG_READY: set[str] = set()
_PG_LOCK = threading.Lock()
_SQLITE_READY: set[str] = set()
_SQLITE_LOCK = threading.Lock()


# --- 연결 ---------------------------------------------------------------------


def storage_backend(db_path: str | None = None) -> str:
    """이번 호출이 쓰는 저장소: "postgres" 또는 "sqlite"(성적표와 같은 규칙)."""
    return history_store.storage_backend(db_path)


def database_label(db_path: str | None = None) -> str:
    """응답·로그에 남길 저장소 이름(자격 증명은 지운다)."""
    if storage_backend(db_path) == "postgres":
        return history_store.redact_database_url()
    return db_path or history_store.get_db_path()


def _ensure_pg_schema(conn, key: str) -> None:
    if key in _PG_READY:
        return
    with _PG_LOCK:
        if key in _PG_READY:
            return
        for statement in _split_statements(_PG_SCHEMA):
            conn.execute(statement)
        # 이미 돌고 있는 실서버 테이블에는 `CREATE TABLE IF NOT EXISTS`가 컬럼을 더하지 않는다.
        # 기존 계좌·체결을 지우지 않고 수수료 컬럼만 덧붙인다(기본 0 = 예전 체결은 수수료 미기록).
        conn.execute(f"ALTER TABLE {TABLE_TRADES} ADD COLUMN IF NOT EXISTS fee_krw "
                     "DOUBLE PRECISION NOT NULL DEFAULT 0")
        conn.commit()
        _PG_READY.add(key)


def _ensure_sqlite_schema(conn, key: str) -> None:
    """SQLite 스키마 + 기존 파일용 컬럼 마이그레이션(`PRAGMA table_info`로 확인 후 ADD COLUMN)."""
    if key in _SQLITE_READY:
        return
    with _SQLITE_LOCK:
        if key in _SQLITE_READY:
            return
        conn.executescript(_SQLITE_SCHEMA)
        columns = set()
        for row in conn.execute(f"PRAGMA table_info({TABLE_TRADES})").fetchall() or []:
            try:
                columns.add(str(row[1]))
            except (IndexError, KeyError, TypeError):
                columns.add(str(dict(row).get("name")))
        if "fee_krw" not in columns:
            conn.execute(f"ALTER TABLE {TABLE_TRADES} ADD COLUMN fee_krw REAL NOT NULL DEFAULT 0")
            conn.commit()
        _SQLITE_READY.add(key)


def _split_statements(sql: str) -> list[str]:
    return [part.strip() for part in sql.split(";") if part.strip()]


@contextmanager
def _connection(db_path: str | None = None, write: bool = False):
    """(연결, 백엔드)을 내주는 짧은 연결. 쓰기는 프로세스 락으로 직렬화한다.

    `?"` 자리표시자를 PostgreSQL용 `%s`로 바꿔 쓰므로 호출부는 한 가지 SQL만 알면 된다.
    """
    if storage_backend(db_path) == "postgres":
        lock = _WRITE_LOCK if write else _NullLock()
        with lock:
            conn = history_store._pg_connect()
            try:
                _ensure_pg_schema(conn, history_store.database_url())
                yield conn, "postgres"
                if write:
                    conn.commit()
            except BaseException:
                if write:
                    try:
                        conn.rollback()
                    except Exception:  # 이미 끊긴 연결
                        pass
                raise
            finally:
                conn.close()
        return

    lock = _WRITE_LOCK if write else _NullLock()
    with lock:
        conn = history_store._connect(db_path)
        try:
            _ensure_sqlite_schema(conn, db_path or history_store.get_db_path())
            yield conn, "sqlite"
            if write:
                conn.commit()
        except BaseException:
            if write:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            conn.close()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sql(query: str, backend: str) -> str:
    return query.replace("?", "%s") if backend == "postgres" else query


def _rows(cursor) -> list[dict]:
    out = getattr(cursor, "fetchall", lambda: [])()
    return [dict(row) for row in out or []]


def _row(cursor) -> dict | None:
    getter = getattr(cursor, "fetchone", lambda: None)
    item = getter()
    return dict(item) if item else None


def _now() -> str:
    return history_store.kst_now().isoformat(timespec="seconds")


def kst_today_str() -> str:
    return history_store.kst_today().isoformat()


# --- 계좌 ---------------------------------------------------------------------


def _default_account() -> dict:
    stamp = _now()
    return {
        "id": ACCOUNT_ID,
        "initial_cash_krw": DEFAULT_INITIAL_CASH_KRW,
        "cash_krw": DEFAULT_INITIAL_CASH_KRW,
        "markets": DEFAULT_MARKETS,
        "strategy": DEFAULT_STRATEGY,
        "auto_enabled": 1,
        "last_run_date": None,
        "last_run_us_date": None,
        "last_run_kr_date": None,
        "created_at": stamp,
        "updated_at": stamp,
    }


def get_account(db_path: str | None = None) -> dict:
    """계좌 한 행(KST 기준). 없으면 기본값($1만 → ₩1,000만)으로 만들어 돌려준다."""
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (ACCOUNT_ID,))
        account = _row(cur)
    if account:
        return _normalize_account(account)

    account = _default_account()
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT id FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (ACCOUNT_ID,))
        if _row(cur) is None:
            conn.execute(
                _sql(f"INSERT INTO {TABLE_ACCOUNT} (id, initial_cash_krw, cash_krw, markets, strategy,"
                     " auto_enabled, last_run_date, last_run_us_date, last_run_kr_date,"
                     " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", backend),
                (ACCOUNT_ID, account["initial_cash_krw"], account["cash_krw"], account["markets"],
                 account["strategy"], account["auto_enabled"], None, None, None,
                 account["created_at"], account["updated_at"]),
            )
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (ACCOUNT_ID,))
        stored = _row(cur)
    logger.info("모의투자 계좌 생성: 시드 ₩%.0f · 시장 %s", account["initial_cash_krw"], account["markets"])
    return _normalize_account(stored or account)


def _normalize_account(row: dict) -> dict:
    out = dict(row)
    out["initial_cash_krw"] = float(out.get("initial_cash_krw") or DEFAULT_INITIAL_CASH_KRW)
    out["cash_krw"] = float(out.get("cash_krw") or 0.0)
    out["markets"] = str(out.get("markets") or DEFAULT_MARKETS).upper()
    if out["markets"] not in MARKETS:
        out["markets"] = DEFAULT_MARKETS
    out["strategy"] = str(out.get("strategy") or DEFAULT_STRATEGY)
    if out["strategy"] not in STRATEGIES:
        out["strategy"] = DEFAULT_STRATEGY
    out["auto_enabled"] = bool(out.get("auto_enabled", 1))
    return out


def update_account(fields: dict, db_path: str | None = None) -> dict:
    """계좌의 지정 필드만 갱신하고 갱신된 행을 돌려준다(계좌가 없으면 먼저 만든다)."""
    get_account(db_path)  # 행이 없으면 기본값으로 만들어 둔다(UPDATE 0건 방지)
    allowed = {"initial_cash_krw", "cash_krw", "markets", "strategy", "auto_enabled",
               "last_run_date", "last_run_us_date", "last_run_kr_date"}
    payload = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not payload:
        return get_account(db_path)
    payload["updated_at"] = _now()
    assignments = ", ".join(f"{name} = ?" for name in payload)
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(_sql(f"UPDATE {TABLE_ACCOUNT} SET {assignments} WHERE id = ?", backend),
                     (*payload.values(), ACCOUNT_ID))
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (ACCOUNT_ID,))
        stored = _row(cur)
    return _normalize_account(stored or get_account(db_path))


# --- 포지션 -------------------------------------------------------------------


def list_positions(db_path: str | None = None) -> list[dict]:
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} ORDER BY id", backend))
        rows = _rows(cur)
    return [_normalize_position(row) for row in rows]


def _normalize_position(row: dict) -> dict:
    out = dict(row)
    out["shares"] = float(out.get("shares") or 0.0)
    out["avg_price"] = float(out.get("avg_price") or 0.0)
    out["fx_rate"] = float(out.get("fx_rate") or 1.0)
    out["cost_krw"] = float(out.get("cost_krw") or 0.0)
    return out


def get_position(ticker: str, db_path: str | None = None) -> dict | None:
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE ticker = ?", backend), (sym,))
        row = _row(cur)
    return _normalize_position(row) if row else None


def add_position(ticker: str, market: str, currency: str, shares: float, price: float,
                 fx_rate: float, cost_krw: float, name: str = "", reason: str = "",
                 db_path: str | None = None) -> dict:
    """포지션을 더한다. 이미 있으면 평단가·원가를 가중평균으로 합친다(추가 매수)."""
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    price = float(price)
    cost_krw = float(cost_krw)
    if not sym or shares <= 0 or price <= 0 or cost_krw <= 0:
        raise ValueError("invalid_position")

    existing = get_position(sym, db_path)
    stamp = _now()
    with _connection(db_path, write=True) as (conn, backend):
        if existing:
            total_shares = existing["shares"] + shares
            total_cost = existing["cost_krw"] + cost_krw
            avg_price = (existing["avg_price"] * existing["shares"] + price * shares) / total_shares
            fx = (existing["fx_rate"] * existing["cost_krw"] + float(fx_rate) * cost_krw) / total_cost
            conn.execute(
                _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, avg_price = ?, fx_rate = ?,"
                     " cost_krw = ?, reason = ? WHERE ticker = ?", backend),
                (total_shares, avg_price, fx, total_cost,
                 (reason or existing.get("reason") or "")[:400], sym),
            )
        else:
            conn.execute(
                _sql(f"INSERT INTO {TABLE_POSITIONS} (ticker, market, name, currency, shares,"
                     " avg_price, fx_rate, cost_krw, reason, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     backend),
                (sym, market, name or "", currency, shares, price, float(fx_rate), cost_krw,
                 (reason or "")[:400], stamp),
            )
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE ticker = ?", backend), (sym,))
        stored = _row(cur)
    return _normalize_position(stored or {})


def reduce_position(ticker: str, shares: float, db_path: str | None = None) -> dict | None:
    """보유 수량을 줄인다. 0 이하가 되면 행을 지운다(전량 매도).

    반환은 남은 포지션(전량 매도면 None). 원가는 수량 비율로 줄어 평단가·매수환율이 유지된다.
    """
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    position = get_position(sym, db_path)
    if not position:
        return None
    remaining = round(position["shares"] - shares, 8)
    with _connection(db_path, write=True) as (conn, backend):
        if remaining <= 0.0000001:
            conn.execute(_sql(f"DELETE FROM {TABLE_POSITIONS} WHERE ticker = ?", backend), (sym,))
            return None
        ratio = remaining / position["shares"]
        conn.execute(
            _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, cost_krw = ? WHERE ticker = ?", backend),
            (remaining, round(position["cost_krw"] * ratio, 2), sym),
        )
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE ticker = ?", backend), (sym,))
        stored = _row(cur)
    return _normalize_position(stored or {})


def clear_positions(db_path: str | None = None) -> int:
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(f"DELETE FROM {TABLE_POSITIONS}")
        return int(getattr(cur, "rowcount", 0) or 0)


# --- 체결 ---------------------------------------------------------------------


def _trade_payload(trade: dict) -> dict:
    """체결 dict → DB 컬럼 순서가 고정된 payload."""
    return {
        "run_id": trade.get("run_id") or "",
        "trade_date": trade.get("trade_date") or kst_today_str(),
        "traded_at": trade.get("traded_at") or _now(),
        "ticker": str(trade.get("ticker") or "").strip().upper(),
        "market": str(trade.get("market") or "").upper(),
        "name": trade.get("name") or "",
        "side": str(trade.get("side") or "").upper(),
        "shares": float(trade.get("shares") or 0.0),
        "price": float(trade.get("price") or 0.0),
        "currency": str(trade.get("currency") or "USD").upper(),
        "fx_rate": float(trade.get("fx_rate") or 1.0),
        "amount_krw": abs(float(trade.get("amount_krw") or 0.0)),
        "fee_krw": abs(float(trade.get("fee_krw") or 0.0)),
        "realized_pnl_krw": (None if trade.get("realized_pnl_krw") is None
                             else float(trade.get("realized_pnl_krw"))),
        "reason": (trade.get("reason") or "")[:400],
        "ai_source": trade.get("ai_source") or "",
    }


def _insert_trade(conn, backend: str, payload: dict) -> int | None:
    """이미 열린 연결(트랜잭션) 안에서 체결 한 행을 넣고 id를 돌려준다."""
    columns = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    values = tuple(payload.values())
    if backend == "postgres":
        row = conn.execute(
            _sql(f"INSERT INTO {TABLE_TRADES} ({columns}) VALUES ({marks}) RETURNING id", backend),
            values).fetchone()
        return (row or {}).get("id") if isinstance(row, dict) else None
    cur = conn.execute(f"INSERT INTO {TABLE_TRADES} ({columns}) VALUES ({marks})", values)
    return getattr(cur, "lastrowid", None)


def record_trade(trade: dict, db_path: str | None = None) -> dict:
    """체결 한 건을 남긴다(BUY/SELL). `amount_krw`는 원화 환산 금액(양수)."""
    payload = _trade_payload(trade)
    with _connection(db_path, write=True) as (conn, backend):
        payload["id"] = _insert_trade(conn, backend, payload)
    return payload


def _apply_cash_delta(conn, backend: str, delta: float, stamp: str) -> float | None:
    """현금을 **원자적으로** 더한다(음수면 잔고 부족 시 실패). 새 잔고 또는 None.

    절대값을 덮어쓰지 않고 `cash_krw + delta`로 계산하므로, 자동 실행과 수동 실행이 겹쳐도
    한쪽의 차감이 다른 쪽에 의해 지워지지 않는다(현금 초과 매수 방지).
    """
    amount = round(float(delta), 2)
    if amount < 0:
        cur = conn.execute(
            _sql(f"UPDATE {TABLE_ACCOUNT} SET cash_krw = cash_krw + ?, updated_at = ?"
                 " WHERE id = ? AND cash_krw + ? >= 0", backend),
            (amount, stamp, ACCOUNT_ID, amount))
    else:
        cur = conn.execute(
            _sql(f"UPDATE {TABLE_ACCOUNT} SET cash_krw = cash_krw + ?, updated_at = ? WHERE id = ?",
                 backend),
            (amount, stamp, ACCOUNT_ID))
    if int(getattr(cur, "rowcount", 0) or 0) == 0:
        return None
    row = _row(conn.execute(
        _sql(f"SELECT cash_krw FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (ACCOUNT_ID,)))
    return round(float((row or {}).get("cash_krw") or 0.0), 2)


def apply_buy(ticker: str, market: str, name: str, currency: str, shares: float, price: float,
              fx_rate: float, cost_krw: float, reason: str, cash_delta: float,
              trade: dict, db_path: str | None = None) -> dict:
    """매수 체결을 **한 트랜잭션으로** 반영한다(포지션 + 계좌 현금 + 체결 기록).

    `cash_delta`는 보통 `-cost_krw`다. 잔고보다 많이 차감하려 하면 `cash_below_zero`로
    실패하고 포지션도 남기지 않는다.
    """
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    price = float(price)
    cost_krw = float(cost_krw)
    if not sym or shares <= 0 or price <= 0 or cost_krw <= 0:
        raise ValueError("invalid_position")
    get_account(db_path)  # 계좌 행이 없으면 먼저 만든다(현금 UPDATE가 0건이 되지 않게)

    payload = _trade_payload(trade)
    stamp = _now()
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE ticker = ?", backend), (sym,))
        existing = _row(cur)
        if existing:
            total_shares = float(existing["shares"]) + shares
            total_cost = float(existing["cost_krw"]) + cost_krw
            avg_price = (float(existing["avg_price"]) * float(existing["shares"])
                         + price * shares) / total_shares
            fx = (float(existing["fx_rate"]) * float(existing["cost_krw"])
                  + float(fx_rate) * cost_krw) / total_cost
            conn.execute(
                _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, avg_price = ?, fx_rate = ?,"
                     " cost_krw = ?, reason = ? WHERE ticker = ?", backend),
                (total_shares, avg_price, fx, total_cost,
                 (reason or existing.get("reason") or "")[:400], sym),
            )
        else:
            conn.execute(
                _sql(f"INSERT INTO {TABLE_POSITIONS} (ticker, market, name, currency, shares,"
                     " avg_price, fx_rate, cost_krw, reason, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     backend),
                (sym, market, name or "", currency, shares, price, float(fx_rate), cost_krw,
                 (reason or "")[:400], stamp),
            )
        cash = _apply_cash_delta(conn, backend, cash_delta, stamp)
        if cash is None:  # 잔고 부족 → 트랜잭션 전체 취소(포지션도 남지 않는다)
            raise ValueError("cash_below_zero")
        payload["id"] = _insert_trade(conn, backend, payload)
        payload["cash_krw"] = cash
        payload["settle_krw"] = round(abs(float(cash_delta)), 2)
    return payload


def apply_sell(ticker: str, shares: float, cost_krw_sold: float, cash_delta: float,
               trade: dict, db_path: str | None = None) -> dict:
    """매도 체결을 한 트랜잭션으로 반영한다(포지션 축소/삭제 + 현금 증가 + 체결 기록)."""
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    if not sym or shares <= 0:
        raise ValueError("invalid_position")
    get_account(db_path)

    payload = _trade_payload(trade)
    stamp = _now()
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE ticker = ?", backend), (sym,))
        position = _row(cur)
        if not position:
            raise ValueError("position_not_found")
        remaining = round(float(position["shares"]) - shares, 8)
        if remaining <= 0.0000001:
            conn.execute(_sql(f"DELETE FROM {TABLE_POSITIONS} WHERE ticker = ?", backend), (sym,))
        else:
            kept_cost = max(0.0, round(float(position["cost_krw"]) - float(cost_krw_sold), 2))
            conn.execute(
                _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, cost_krw = ? WHERE ticker = ?", backend),
                (remaining, kept_cost, sym),
            )
        cash = _apply_cash_delta(conn, backend, cash_delta, stamp)
        if cash is None:  # 계좌 행이 없을 수 없다 — 있으면 실패로 본다
            raise ValueError("cash_update_failed")
        payload["id"] = _insert_trade(conn, backend, payload)
        payload["cash_krw"] = cash
        payload["settle_krw"] = round(float(cash_delta), 2)
        payload["remaining_shares"] = 0.0 if remaining <= 0.0000001 else remaining
    return payload


def recent_trades(limit: int = 40, db_path: str | None = None) -> list[dict]:
    n = max(1, min(int(limit or 40), 500))
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_TRADES} ORDER BY id DESC LIMIT ?", backend), (n,))
        rows = _rows(cur)
    for row in rows:
        row["shares"] = float(row.get("shares") or 0.0)
        row["price"] = float(row.get("price") or 0.0)
        row["fx_rate"] = float(row.get("fx_rate") or 1.0)
        row["amount_krw"] = float(row.get("amount_krw") or 0.0)
        # 수수료 컬럼이 없던 시절 체결은 0이다(마이그레이션 기본값).
        row["fee_krw"] = float(row.get("fee_krw") or 0.0)
        sign = 1.0 if str(row.get("side") or "").upper() == "BUY" else -1.0
        row["settle_krw"] = round(row["amount_krw"] + sign * row["fee_krw"], 2)
        if row.get("realized_pnl_krw") is not None:
            row["realized_pnl_krw"] = float(row["realized_pnl_krw"])
    return rows


def clear_trades(db_path: str | None = None) -> int:
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(f"DELETE FROM {TABLE_TRADES}")
        return int(getattr(cur, "rowcount", 0) or 0)


# --- 수익률 스냅샷 ------------------------------------------------------------


def record_equity(day: str, total_value_krw: float, cash_krw: float, stock_value_krw: float,
                  return_pct: float | None, db_path: str | None = None) -> dict:
    """하루 한 행(KST 날짜 UNIQUE). 같은 날 다시 실행하면 최신 값으로 덮어쓴다."""
    payload = {
        "date": day or kst_today_str(),
        "total_value_krw": round(float(total_value_krw), 2),
        "cash_krw": round(float(cash_krw), 2),
        "stock_value_krw": round(float(stock_value_krw), 2),
        "return_pct": None if return_pct is None else round(float(return_pct), 4),
        "recorded_at": _now(),
    }
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(
            _sql(f"INSERT INTO {TABLE_EQUITY} (date, total_value_krw, cash_krw, stock_value_krw,"
                 " return_pct, recorded_at) VALUES (?, ?, ?, ?, ?, ?)"
                 " ON CONFLICT (date) DO UPDATE SET total_value_krw = excluded.total_value_krw,"
                 " cash_krw = excluded.cash_krw, stock_value_krw = excluded.stock_value_krw,"
                 " return_pct = excluded.return_pct, recorded_at = excluded.recorded_at", backend),
            tuple(payload.values()),
        )
    return payload


def equity_history(limit: int = 180, db_path: str | None = None) -> list[dict]:
    n = max(1, min(int(limit or 180), 1000))
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(
            f"SELECT * FROM {TABLE_EQUITY} ORDER BY date DESC LIMIT ?", backend), (n,))
        rows = _rows(cur)
    rows.reverse()  # 오래된 날짜부터(차트 x축)
    for row in rows:
        row["total_value_krw"] = float(row.get("total_value_krw") or 0.0)
        row["cash_krw"] = float(row.get("cash_krw") or 0.0)
        row["stock_value_krw"] = float(row.get("stock_value_krw") or 0.0)
        if row.get("return_pct") is not None:
            row["return_pct"] = float(row["return_pct"])
    return rows


def clear_equity(db_path: str | None = None) -> int:
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(f"DELETE FROM {TABLE_EQUITY}")
        return int(getattr(cur, "rowcount", 0) or 0)


# --- FCM 토큰(체결 푸시 알림 기기) ---------------------------------------------


def register_fcm_token(token: str, platform: str = "android",
                       db_path: str | None = None) -> dict:
    """앱이 받은 FCM 토큰을 저장하거나 갱신한다(토큰 하나 = 기기 하나).

    같은 토큰이 다시 오면 `updated_at`만 새로 쓴다(재설치·앱 실행마다 동기화해도 행이 늘지 않음).
    """
    clean = str(token or "").strip()
    if not clean:
        raise ValueError("empty_token")
    if len(clean) > 4096:
        raise ValueError("token_too_long")
    stamp = _now()
    payload = {"token": clean, "platform": str(platform or "").strip()[:32], "updated_at": stamp}
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(
            _sql(f"INSERT INTO {TABLE_FCM_TOKENS} (token, platform, updated_at)"
                 " VALUES (?, ?, ?)"
                 " ON CONFLICT (token) DO UPDATE SET platform = excluded.platform,"
                 " updated_at = excluded.updated_at", backend),
            (payload["token"], payload["platform"], payload["updated_at"]),
        )
    return payload


def remove_fcm_token(token: str, db_path: str | None = None) -> int:
    """토큰 하나를 지운다(알림 끄기·만료 정리). 지운 행 수를 돌려준다."""
    clean = str(token or "").strip()
    if not clean:
        return 0
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"DELETE FROM {TABLE_FCM_TOKENS} WHERE token = ?", backend), (clean,))
        return int(getattr(cur, "rowcount", 0) or 0)


def remove_fcm_tokens(tokens: list[str], db_path: str | None = None) -> int:
    """여러 토큰을 한 번에 지운다(만료 토큰 일괄 정리)."""
    removed = 0
    for token in list(dict.fromkeys(str(t or "").strip() for t in tokens or [])):
        if token:
            removed += remove_fcm_token(token, db_path)
    return removed


def list_fcm_tokens(db_path: str | None = None) -> list[dict]:
    """등록된 기기 토큰(최신 순)."""
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(
            f"SELECT token, platform, updated_at FROM {TABLE_FCM_TOKENS}"
            " ORDER BY updated_at DESC", backend))
        rows = _rows(cur)
    return [{"token": str(row.get("token") or ""),
             "platform": str(row.get("platform") or ""),
             "updated_at": row.get("updated_at")} for row in rows]


def count_fcm_tokens(db_path: str | None = None) -> int:
    with _connection(db_path) as (conn, backend):
        row = _row(conn.execute(_sql(f"SELECT COUNT(*) AS n FROM {TABLE_FCM_TOKENS}", backend)))
    return int((row or {}).get("n") or 0)


# --- 초기화 -------------------------------------------------------------------


def reset(db_path: str | None = None, initial_cash_krw: float | None = None,
          keep_settings: bool = True) -> dict:
    """포지션·체결·스냅샷을 지우고 새 시드머니로 계좌를 다시 시작한다.

    설정(시장·전략·자동 실행)은 기본으로 유지하고, `initial_cash_krw`를 주면 그 값으로
    시드머니를 바꾼다. `last_run_*` 날짜는 지워 오늘 자동 실행이 다시 가능하게 한다.
    """
    account = get_account(db_path)
    seed = float(initial_cash_krw) if initial_cash_krw is not None else account["initial_cash_krw"]
    clear_positions(db_path)
    clear_trades(db_path)
    clear_equity(db_path)
    updated = update_account({
        "initial_cash_krw": seed,
        "cash_krw": seed,
        "last_run_date": None,
        "last_run_us_date": None,
        "last_run_kr_date": None,
    }, db_path)
    record_equity(kst_today_str(), seed, seed, 0.0, 0.0, db_path)
    logger.info("모의투자 계좌 초기화: 시드 ₩%.0f", seed)
    return updated


def init_store(db_path: str | None = None) -> dict:
    """스키마를 확인하고 계좌·시드 스냅샷을 준비한다(기동 시 1회)."""
    account = get_account(db_path)
    if not equity_history(1, db_path):
        record_equity(kst_today_str(), account["cash_krw"], account["cash_krw"], 0.0, 0.0, db_path)
    return account


def as_int_krw(value: Any) -> int:
    """원화 금액을 화면용 정수로(반올림)."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def as_time(value: Any) -> datetime | None:
    """저장된 ISO 문자열 → datetime(KST). 해석 불가면 None."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=history_store.KST)
    return parsed


def env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")
