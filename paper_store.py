"""AI 모의투자(페이퍼 트레이딩) — 계좌·보유 포지션·체결·수익률 스냅샷 저장소.

설계 원칙
- **시장별로 완전히 분리된 두 계좌**: 미국(US) 계좌(`id=1`, `market_scope='US'`)와
  한국(KR) 계좌(`id=2`, `market_scope='KR'`). 현금·보유 종목·체결 내역·일별 수익률이
  `account_id`로 갈라져 **한쪽 매매가 다른 쪽 숫자를 절대 건드리지 않는다**. 두 계좌의
  수익률을 나란히 비교하는 것이 이 구조의 목적이다.
- **통화는 원화(KRW) 하나로 통일한다**: 각 계좌의 시드머니·현금·총 자산·수익률은 모두 `₩`다.
  미국 종목의 `avg_price`·`price`는 달러로 남기고(`currency="USD"`), 매수 시점의 USD/KRW
  환율(`fx_rate`)을 포지션에 함께 저장해 원화 원가(`cost_krw`)를 고정한다. 평가는 **현재
  환율**로 다시 계산한다 — 환율 변동도 수익률에 들어간다.
- **기존 성적표와 분리**: `history_store`의 `recommendations` 테이블은 그대로 두고,
  `paper_*` 테이블 네 개를 새로 만든다(`performance` 화면은 건드리지 않는다).
- **저장 백엔드는 성적표와 같은 규칙**: `DATABASE_URL`(PostgreSQL/Neon)이 있으면 그쪽에,
  없으면 SQLite(`HISTORY_DB_PATH`, 기본 `data/recommendations.db`)에 쓴다. 연결·정규화
  유틸은 `history_store`를 그대로 재사용해 두 저장소가 같은 DB 파일·같은 SSL 규칙을 쓴다.
- **현금은 절대 음수가 되지 않는다**: 매수 금액은 호출부(`paper_trading`)가 잔여 현금으로
  검증해 넘기지만, 저장 단계에서도 음수 현금을 막는다(`_guard_cash`).
- **기존 DB는 그대로 살린다**: 예전 단일 계좌 테이블에는 `account_id`가 없다. 기동 시
  `account_id`·`market_scope` 컬럼을 더하고, 이미 쌓인 계좌·체결·스냅샷은 **미국 계좌(1)**로
  옮긴다(지우지 않는다). 유니크 제약도 `(account_id, ticker)`·`(account_id, date)`로 바꾼다.
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
TABLE_USERS = "paper_users"          # 닉네임 + PIN 사용자(paper_auth가 만든다)
TABLE_SETTINGS = "paper_settings"    # 서버 비밀값 보관(토큰 서명 키)

# 시장별 계좌 — id가 곧 계좌이고, market_scope가 그 계좌의 시장이다.
# 기본 관리자 유저(user_id=1)의 계좌만 예전 id(1=US, 2=KR)를 그대로 쓴다.
# 지인 계정은 새 계좌 id(3, 4, …)를 받고 market_scope로 시장을 구분한다.
ACCOUNT_US = 1
ACCOUNT_KR = 2
DEFAULT_USER_ID = 1
DEFAULT_ADMIN_NICKNAME = "현우"
ACCOUNTS = (ACCOUNT_US, ACCOUNT_KR)
ACCOUNT_MARKETS = {ACCOUNT_US: "US", ACCOUNT_KR: "KR"}
MARKET_ACCOUNTS = {"US": ACCOUNT_US, "KR": ACCOUNT_KR}
ACCOUNT_LABELS = {"US": "🇺🇸 미국", "KR": "🇰🇷 한국"}
# 하위 호환: 계좌를 지정하지 않는 호출은 미국 계좌를 본다.
ACCOUNT_ID = ACCOUNT_US

# 기본 시드머니 — 계좌마다 원화 100만 원(미국·한국 각각 따로).
DEFAULT_INITIAL_CASH_KRW = 1_000_000.0
MIN_INITIAL_CASH_KRW = 100_000.0          # 10만 원
MAX_INITIAL_CASH_KRW = 1_000_000_000.0    # 10억 원

MARKETS = ("ALL", "US", "KR")
DEFAULT_MARKETS = "US"

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
    user_id          INTEGER NOT NULL DEFAULT 1,
    market_scope     TEXT    NOT NULL DEFAULT 'US',
    initial_cash_krw REAL    NOT NULL,
    cash_krw         REAL    NOT NULL,
    markets          TEXT    NOT NULL DEFAULT 'US',
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
    account_id INTEGER NOT NULL DEFAULT 1,
    ticker     TEXT    NOT NULL,
    market     TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    currency   TEXT    NOT NULL DEFAULT 'USD',
    shares     REAL    NOT NULL,
    avg_price  REAL    NOT NULL,
    fx_rate    REAL    NOT NULL DEFAULT 1,
    cost_krw   REAL    NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    opened_at  TEXT,
    UNIQUE(account_id, ticker)
);

CREATE TABLE IF NOT EXISTS {TABLE_TRADES} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL DEFAULT 1,
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
    account_id      INTEGER NOT NULL DEFAULT 1,
    date            TEXT    NOT NULL,
    total_value_krw REAL    NOT NULL,
    cash_krw        REAL    NOT NULL,
    stock_value_krw REAL    NOT NULL,
    return_pct      REAL,
    recorded_at     TEXT,
    UNIQUE(account_id, date)
);

CREATE TABLE IF NOT EXISTS {TABLE_FCM_TOKENS} (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL DEFAULT 1,
    platform   TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
"""

# PostgreSQL: AUTOINCREMENT 대신 SERIAL, REAL 대신 DOUBLE PRECISION.
_PG_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ACCOUNT} (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL DEFAULT 1,
    market_scope     TEXT    NOT NULL DEFAULT 'US',
    initial_cash_krw DOUBLE PRECISION NOT NULL,
    cash_krw         DOUBLE PRECISION NOT NULL,
    markets          TEXT    NOT NULL DEFAULT 'US',
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
    account_id INTEGER NOT NULL DEFAULT 1,
    ticker     TEXT    NOT NULL,
    market     TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    currency   TEXT    NOT NULL DEFAULT 'USD',
    shares     DOUBLE PRECISION NOT NULL,
    avg_price  DOUBLE PRECISION NOT NULL,
    fx_rate    DOUBLE PRECISION NOT NULL DEFAULT 1,
    cost_krw   DOUBLE PRECISION NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    opened_at  TEXT,
    UNIQUE(account_id, ticker)
);

CREATE TABLE IF NOT EXISTS {TABLE_TRADES} (
    id               SERIAL PRIMARY KEY,
    account_id       INTEGER NOT NULL DEFAULT 1,
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
    account_id      INTEGER NOT NULL DEFAULT 1,
    date            TEXT    NOT NULL,
    total_value_krw DOUBLE PRECISION NOT NULL,
    cash_krw        DOUBLE PRECISION NOT NULL,
    stock_value_krw DOUBLE PRECISION NOT NULL,
    return_pct      DOUBLE PRECISION,
    recorded_at     TEXT,
    UNIQUE(account_id, date)
);

CREATE TABLE IF NOT EXISTS {TABLE_FCM_TOKENS} (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL DEFAULT 1,
    platform   TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
"""

_WRITE_LOCK = threading.Lock()
_PG_READY: set[str] = set()
_PG_LOCK = threading.Lock()
_SQLITE_READY: set[str] = set()
_SQLITE_LOCK = threading.Lock()


# --- 계좌 식별 ----------------------------------------------------------------


def normalize_account_id(value: Any = None) -> int:
    """`"US"`/`"KR"`/`"1"`/`1` → 계좌 id. 빈 값은 미국 계좌(1). 모르는 값은 `ValueError`.

    계좌 id는 이제 유저마다 다르다(기본 관리자 1·2, 지인 3·4 …). 1 이상의 정수는 그대로 쓴다.
    """
    if value is None or value == "":
        return ACCOUNT_US
    if isinstance(value, bool):
        raise ValueError("account_invalid")
    if isinstance(value, int):
        if value in ACCOUNT_MARKETS or value > 0:
            return value
        raise ValueError("account_invalid")
    token = str(value).strip().upper()
    if token in ("", "US", "1", "USA"):
        return ACCOUNT_US
    if token in ("KR", "2", "KOR"):
        return ACCOUNT_KR
    if token.isdigit() and int(token) > 0:
        return int(token)
    raise ValueError("account_invalid")


def account_market(account_id: Any = None) -> str:
    """계좌 id → 시장 코드(`"US"`/`"KR"`). 알 수 없는 id는 미국으로 본다(행의 market_scope가 우선)."""
    aid = normalize_account_id(account_id)
    return ACCOUNT_MARKETS.get(aid, "US")


def account_label(account_id: Any = None) -> str:
    """화면·푸시용 이름 — `"🇺🇸 미국"` / `"🇰🇷 한국"`."""
    return ACCOUNT_LABELS[account_market(account_id)]


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
        # 기존 계좌·체결은 지우지 않고 컬럼만 덧붙인다(예전 행은 0 = 수수료 미기록, 계좌는 미국).
        for table in (TABLE_POSITIONS, TABLE_TRADES, TABLE_EQUITY):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS account_id "
                         "INTEGER NOT NULL DEFAULT 1")
        conn.execute(f"ALTER TABLE {TABLE_TRADES} ADD COLUMN IF NOT EXISTS fee_krw "
                     "DOUBLE PRECISION NOT NULL DEFAULT 0")
        conn.execute(f"ALTER TABLE {TABLE_ACCOUNT} ADD COLUMN IF NOT EXISTS market_scope TEXT")
        # 닉네임·PIN 멀티 유저 — 기존 계좌·기기 토큰은 모두 기본 관리자(user 1) 소유로 둔다.
        conn.execute(f"ALTER TABLE {TABLE_ACCOUNT} ADD COLUMN IF NOT EXISTS user_id "
                     "INTEGER NOT NULL DEFAULT 1")
        conn.execute(f"ALTER TABLE {TABLE_FCM_TOKENS} ADD COLUMN IF NOT EXISTS user_id "
                     "INTEGER NOT NULL DEFAULT 1")
        _pg_scope_accounts(conn)
        # 예전 단일 계좌 시절의 전역 유니크(ticker / date)를 계좌별 유니크로 바꾼다.
        _pg_drop_global_unique(conn, TABLE_POSITIONS, ("account_id", "ticker"))
        _pg_drop_global_unique(conn, TABLE_EQUITY, ("account_id", "date"))
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS paper_positions_account_ticker_key"
                     f" ON {TABLE_POSITIONS} (account_id, ticker)")
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS paper_equity_history_account_date_key"
                     f" ON {TABLE_EQUITY} (account_id, date)")
        conn.commit()
        _PG_READY.add(key)


def _pg_scope_accounts(conn) -> None:
    """계좌 행의 market_scope를 id로 채운다(1=US, 2=KR). `markets`도 그 계좌의 시장으로 맞춘다."""
    conn.execute(f"UPDATE {TABLE_ACCOUNT} SET market_scope = CASE WHEN id = 2 THEN 'KR' ELSE 'US' END"
                 " WHERE market_scope IS NULL OR market_scope = ''")
    conn.execute(f"UPDATE {TABLE_ACCOUNT} SET markets = market_scope"
                 " WHERE markets IS NULL OR markets = '' OR markets = 'ALL' OR markets <> market_scope")


def _pg_drop_global_unique(conn, table: str, keep: tuple[str, ...]) -> None:
    """`keep` 컬럼 조합이 아닌 UNIQUE 제약을 지운다(계좌가 섞이지 않게)."""
    try:
        rows = conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint"
            " WHERE conrelid = %s::regclass AND contype = 'u'", (table,)).fetchall() or []
    except Exception as exc:  # 권한·구버전 — 기본 이름으로만 시도한다
        logger.warning("유니크 제약 조회 실패(%s): %s", table, type(exc).__name__)
        rows = []
    for row in rows:
        try:
            data = dict(row)
        except (TypeError, ValueError):
            continue
        name = data.get("conname")
        definition = str(data.get("definition") or "")
        if not name or all(column in definition for column in keep):
            continue
        conn.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"')
    # 기본 이름(구 스키마)도 확실히 지운다.
    fallback = f"{table}_ticker_key" if table == TABLE_POSITIONS else f"{table}_date_key"
    conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fallback}")


def _ensure_sqlite_schema(conn, key: str) -> None:
    """SQLite 스키마 + 기존 파일용 컬럼/계좌 마이그레이션."""
    if key in _SQLITE_READY:
        return
    with _SQLITE_LOCK:
        if key in _SQLITE_READY:
            return
        conn.executescript(_SQLITE_SCHEMA)
        # 1) 수수료 컬럼(이미 있으면 그대로) — 계좌 테이블 재구성 전에 확인한다.
        columns = _sqlite_columns(conn, TABLE_TRADES)
        if "fee_krw" not in columns:
            conn.execute(f"ALTER TABLE {TABLE_TRADES} ADD COLUMN fee_krw REAL NOT NULL DEFAULT 0")
            conn.commit()
        # 2) 계좌 분리 마이그레이션 — 예전 행은 미국 계좌(1)로 옮긴다.
        if "account_id" not in _sqlite_columns(conn, TABLE_POSITIONS):
            _sqlite_split_accounts(conn)
            conn.commit()
        # 3) 계좌 테이블의 market_scope.
        account_columns = _sqlite_columns(conn, TABLE_ACCOUNT)
        if "market_scope" not in account_columns:
            conn.execute(f"ALTER TABLE {TABLE_ACCOUNT} ADD COLUMN market_scope TEXT")
        # 4) 멀티 유저 — 기존 계좌·기기 토큰은 기본 관리자(user 1) 소유.
        if "user_id" not in _sqlite_columns(conn, TABLE_ACCOUNT):
            conn.execute(f"ALTER TABLE {TABLE_ACCOUNT} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        if "user_id" not in _sqlite_columns(conn, TABLE_FCM_TOKENS):
            conn.execute(f"ALTER TABLE {TABLE_FCM_TOKENS} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        conn.execute(f"UPDATE {TABLE_ACCOUNT}"
                     " SET market_scope = CASE WHEN id = 2 THEN 'KR' ELSE 'US' END"
                     " WHERE market_scope IS NULL OR market_scope = ''")
        conn.execute(f"UPDATE {TABLE_ACCOUNT} SET markets = market_scope"
                     " WHERE markets IS NULL OR markets = '' OR markets = 'ALL'"
                     " OR markets <> market_scope")
        conn.commit()
        _SQLITE_READY.add(key)


def _sqlite_columns(conn, table: str) -> set[str]:
    columns: set[str] = set()
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall() or []
    except Exception:
        return columns
    for row in rows:
        try:
            columns.add(str(row[1]))
        except (IndexError, KeyError, TypeError):
            try:
                columns.add(str(dict(row).get("name")))
            except (TypeError, ValueError):
                pass
    return columns


# 계좌별 유니크로 다시 만드는 테이블(예전 스키마에는 account_id도, 계좌별 유니크도 없다).
_SQLITE_REBUILDS = {
    TABLE_POSITIONS: (
        f"""CREATE TABLE {{tmp}} (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL DEFAULT 1,
            ticker     TEXT    NOT NULL,
            market     TEXT    NOT NULL,
            name       TEXT    NOT NULL DEFAULT '',
            currency   TEXT    NOT NULL DEFAULT 'USD',
            shares     REAL    NOT NULL,
            avg_price  REAL    NOT NULL,
            fx_rate    REAL    NOT NULL DEFAULT 1,
            cost_krw   REAL    NOT NULL,
            reason     TEXT    NOT NULL DEFAULT '',
            opened_at  TEXT,
            UNIQUE(account_id, ticker)
        )""",
        "id, ticker, market, name, currency, shares, avg_price, fx_rate, cost_krw, reason, opened_at",
    ),
    TABLE_TRADES: (
        f"""CREATE TABLE {{tmp}} (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id        INTEGER NOT NULL DEFAULT 1,
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
        )""",
        "id, run_id, trade_date, traded_at, ticker, market, name, side, shares, price, currency,"
        " fx_rate, amount_krw, fee_krw, realized_pnl_krw, reason, ai_source",
    ),
    TABLE_EQUITY: (
        f"""CREATE TABLE {{tmp}} (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id      INTEGER NOT NULL DEFAULT 1,
            date            TEXT    NOT NULL,
            total_value_krw REAL    NOT NULL,
            cash_krw        REAL    NOT NULL,
            stock_value_krw REAL    NOT NULL,
            return_pct      REAL,
            recorded_at     TEXT,
            UNIQUE(account_id, date)
        )""",
        "id, date, total_value_krw, cash_krw, stock_value_krw, return_pct, recorded_at",
    ),
}


def _sqlite_split_accounts(conn) -> None:
    """`account_id`가 없던 테이블을 계좌별 유니크 스키마로 다시 만들고 기존 행은 계좌 1로."""
    for table, (create_sql, columns) in _SQLITE_REBUILDS.items():
        tmp = f"{table}_account_migration"
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
        conn.execute(create_sql.format(tmp=tmp))
        conn.execute(f"INSERT INTO {tmp} (account_id, {columns})"
                     f" SELECT 1, {columns} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
    logger.info("모의투자 DB를 시장별 계좌(US=1 · KR=2) 구조로 마이그레이션했습니다.")


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


def _default_account(account_id: int = ACCOUNT_US, seed: float | None = None,
                     market: str | None = None, user_id: int = DEFAULT_USER_ID) -> dict:
    stamp = _now()
    aid = normalize_account_id(account_id)
    scope = (str(market or "").strip().upper()
             or ACCOUNT_MARKETS.get(aid) or "US")
    if scope not in ACCOUNT_MARKETS.values():
        scope = "US"
    cash = float(DEFAULT_INITIAL_CASH_KRW if seed is None else seed)
    return {
        "id": aid,
        "user_id": int(user_id or DEFAULT_USER_ID),
        "market_scope": scope,
        "initial_cash_krw": cash,
        "cash_krw": cash,
        "markets": scope,
        "strategy": DEFAULT_STRATEGY,
        "auto_enabled": 1,
        "last_run_date": None,
        "last_run_us_date": None,
        "last_run_kr_date": None,
        "created_at": stamp,
        "updated_at": stamp,
    }


def get_account(account_id: Any = ACCOUNT_US, db_path: str | None = None,
                user_id: int | None = None, market: str | None = None) -> dict:
    """계좌 한 행(KST 기준). 없으면 그 계좌의 기본값(₩100만)으로 만들어 돌려준다."""
    aid = normalize_account_id(account_id)
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (aid,))
        account = _row(cur)
    if account:
        return _normalize_account(account)

    account = _default_account(aid, market=market, user_id=user_id or DEFAULT_USER_ID)
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT id FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (aid,))
        if _row(cur) is None:
            conn.execute(
                _sql(f"INSERT INTO {TABLE_ACCOUNT} (id, user_id, market_scope, initial_cash_krw,"
                     " cash_krw, markets, strategy, auto_enabled, last_run_date, last_run_us_date,"
                     " last_run_kr_date, created_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", backend),
                (aid, account["user_id"], account["market_scope"], account["initial_cash_krw"],
                 account["cash_krw"], account["markets"], account["strategy"],
                 account["auto_enabled"], None, None, None, account["created_at"],
                 account["updated_at"]),
            )
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (aid,))
        stored = _row(cur)
    logger.info("모의투자 계좌 생성: %s · 시드 ₩%.0f", account["market_scope"],
                account["initial_cash_krw"])
    return _normalize_account(stored or account)


def _normalize_account(row: dict) -> dict:
    out = dict(row)
    aid = normalize_account_id(out.get("id"))
    scope = str(out.get("market_scope") or out.get("markets") or "").strip().upper()
    if scope not in ACCOUNT_MARKETS.values():
        scope = ACCOUNT_MARKETS.get(aid, "US")
    out["id"] = aid
    out["user_id"] = int(out.get("user_id") or DEFAULT_USER_ID)
    out["market_scope"] = scope
    out["markets"] = scope          # 계좌의 시장은 계좌가 정한다(설정으로 바뀌지 않는다).
    out["initial_cash_krw"] = float(out.get("initial_cash_krw") or DEFAULT_INITIAL_CASH_KRW)
    out["cash_krw"] = float(out.get("cash_krw") or 0.0)
    out["strategy"] = str(out.get("strategy") or DEFAULT_STRATEGY)
    if out["strategy"] not in STRATEGIES:
        out["strategy"] = DEFAULT_STRATEGY
    out["auto_enabled"] = bool(out.get("auto_enabled", 1))
    return out


def update_account(fields: dict, db_path: str | None = None,
                   account_id: Any = ACCOUNT_US) -> dict:
    """계좌의 지정 필드만 갱신하고 갱신된 행을 돌려준다(계좌가 없으면 먼저 만든다)."""
    aid = normalize_account_id(account_id)
    get_account(aid, db_path)  # 행이 없으면 기본값으로 만들어 둔다(UPDATE 0건 방지)
    allowed = {"initial_cash_krw", "cash_krw", "markets", "strategy", "auto_enabled",
               "last_run_date", "last_run_us_date", "last_run_kr_date"}
    payload = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not payload:
        return get_account(aid, db_path)
    payload.pop("markets", None)  # 계좌의 시장은 계좌 id가 정한다
    payload["updated_at"] = _now()
    assignments = ", ".join(f"{name} = ?" for name in payload)
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(_sql(f"UPDATE {TABLE_ACCOUNT} SET {assignments} WHERE id = ?", backend),
                     (*payload.values(), aid))
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (aid,))
        stored = _row(cur)
    return _normalize_account(stored or get_account(aid, db_path))


# --- 포지션 -------------------------------------------------------------------


def list_positions(db_path: str | None = None, account_id: Any = ACCOUNT_US) -> list[dict]:
    aid = normalize_account_id(account_id)
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE account_id = ?"
                                " ORDER BY id", backend), (aid,))
        rows = _rows(cur)
    return [_normalize_position(row) for row in rows]


def _normalize_position(row: dict) -> dict:
    out = dict(row)
    out["shares"] = float(out.get("shares") or 0.0)
    out["avg_price"] = float(out.get("avg_price") or 0.0)
    out["fx_rate"] = float(out.get("fx_rate") or 1.0)
    out["cost_krw"] = float(out.get("cost_krw") or 0.0)
    return out


def get_position(ticker: str, db_path: str | None = None,
                 account_id: Any = ACCOUNT_US) -> dict | None:
    aid = normalize_account_id(account_id)
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE account_id = ?"
                                " AND ticker = ?", backend), (aid, sym))
        row = _row(cur)
    return _normalize_position(row) if row else None


def add_position(ticker: str, market: str, currency: str, shares: float, price: float,
                 fx_rate: float, cost_krw: float, name: str = "", reason: str = "",
                 db_path: str | None = None, account_id: Any = ACCOUNT_US) -> dict:
    """포지션을 더한다. 이미 있으면 평단가·원가를 가중평균으로 합친다(추가 매수)."""
    aid = normalize_account_id(account_id)
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    price = float(price)
    cost_krw = float(cost_krw)
    if not sym or shares <= 0 or price <= 0 or cost_krw <= 0:
        raise ValueError("invalid_position")

    existing = get_position(sym, db_path, aid)
    stamp = _now()
    with _connection(db_path, write=True) as (conn, backend):
        if existing:
            total_shares = existing["shares"] + shares
            total_cost = existing["cost_krw"] + cost_krw
            avg_price = (existing["avg_price"] * existing["shares"] + price * shares) / total_shares
            fx = (existing["fx_rate"] * existing["cost_krw"] + float(fx_rate) * cost_krw) / total_cost
            conn.execute(
                _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, avg_price = ?, fx_rate = ?,"
                     " cost_krw = ?, reason = ? WHERE account_id = ? AND ticker = ?", backend),
                (total_shares, avg_price, fx, total_cost,
                 (reason or existing.get("reason") or "")[:400], aid, sym),
            )
        else:
            conn.execute(
                _sql(f"INSERT INTO {TABLE_POSITIONS} (account_id, ticker, market, name, currency,"
                     " shares, avg_price, fx_rate, cost_krw, reason, opened_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", backend),
                (aid, sym, market, name or "", currency, shares, price, float(fx_rate), cost_krw,
                 (reason or "")[:400], stamp),
            )
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE account_id = ?"
                                " AND ticker = ?", backend), (aid, sym))
        stored = _row(cur)
    return _normalize_position(stored or {})


def reduce_position(ticker: str, shares: float, db_path: str | None = None,
                    account_id: Any = ACCOUNT_US) -> dict | None:
    """보유 수량을 줄인다. 0 이하가 되면 행을 지운다(전량 매도).

    반환은 남은 포지션(전량 매도면 None). 원가는 수량 비율로 줄어 평단가·매수환율이 유지된다.
    """
    aid = normalize_account_id(account_id)
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    position = get_position(sym, db_path, aid)
    if not position:
        return None
    remaining = round(position["shares"] - shares, 8)
    with _connection(db_path, write=True) as (conn, backend):
        if remaining <= 0.0000001:
            conn.execute(_sql(f"DELETE FROM {TABLE_POSITIONS} WHERE account_id = ? AND ticker = ?",
                              backend), (aid, sym))
            return None
        ratio = remaining / position["shares"]
        conn.execute(
            _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, cost_krw = ?"
                 " WHERE account_id = ? AND ticker = ?", backend),
            (remaining, round(position["cost_krw"] * ratio, 2), aid, sym),
        )
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE account_id = ?"
                                " AND ticker = ?", backend), (aid, sym))
        stored = _row(cur)
    return _normalize_position(stored or {})


def clear_positions(db_path: str | None = None, account_id: Any = ACCOUNT_US) -> int:
    aid = normalize_account_id(account_id)
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"DELETE FROM {TABLE_POSITIONS} WHERE account_id = ?", backend),
                           (aid,))
        return int(getattr(cur, "rowcount", 0) or 0)


# --- 체결 ---------------------------------------------------------------------


def _trade_payload(trade: dict, account_id: Any = ACCOUNT_US) -> dict:
    """체결 dict → DB 컬럼 순서가 고정된 payload."""
    return {
        "account_id": normalize_account_id(account_id),
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


def record_trade(trade: dict, db_path: str | None = None,
                 account_id: Any = ACCOUNT_US) -> dict:
    """체결 한 건을 남긴다(BUY/SELL). `amount_krw`는 원화 환산 금액(양수)."""
    payload = _trade_payload(trade, account_id)
    with _connection(db_path, write=True) as (conn, backend):
        payload["id"] = _insert_trade(conn, backend, payload)
    return payload


def _apply_cash_delta(conn, backend: str, delta: float, stamp: str,
                      account_id: Any = ACCOUNT_US) -> float | None:
    """현금을 **원자적으로** 더한다(음수면 잔고 부족 시 실패). 새 잔고 또는 None.

    절대값을 덮어쓰지 않고 `cash_krw + delta`로 계산하므로, 자동 실행과 수동 실행이 겹쳐도
    한쪽의 차감이 다른 쪽에 의해 지워지지 않는다(현금 초과 매수 방지).
    """
    aid = normalize_account_id(account_id)
    amount = round(float(delta), 2)
    if amount < 0:
        cur = conn.execute(
            _sql(f"UPDATE {TABLE_ACCOUNT} SET cash_krw = cash_krw + ?, updated_at = ?"
                 " WHERE id = ? AND cash_krw + ? >= 0", backend),
            (amount, stamp, aid, amount))
    else:
        cur = conn.execute(
            _sql(f"UPDATE {TABLE_ACCOUNT} SET cash_krw = cash_krw + ?, updated_at = ? WHERE id = ?",
                 backend),
            (amount, stamp, aid))
    if int(getattr(cur, "rowcount", 0) or 0) == 0:
        return None
    row = _row(conn.execute(
        _sql(f"SELECT cash_krw FROM {TABLE_ACCOUNT} WHERE id = ?", backend), (aid,)))
    return round(float((row or {}).get("cash_krw") or 0.0), 2)


def _upsert_position(conn, backend: str, aid: int, sym: str, market: str, name: str,
                     currency: str, shares: float, price: float, fx_rate: float,
                     cost_krw: float, reason: str, stamp: str) -> None:
    cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE account_id = ?"
                            " AND ticker = ?", backend), (aid, sym))
    existing = _row(cur)
    if existing:
        total_shares = float(existing["shares"]) + shares
        total_cost = float(existing["cost_krw"]) + cost_krw
        avg_price = (float(existing["avg_price"]) * float(existing["shares"]) + price * shares) / total_shares
        fx = (float(existing["fx_rate"]) * float(existing["cost_krw"]) + float(fx_rate) * cost_krw) / total_cost
        conn.execute(
            _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, avg_price = ?, fx_rate = ?,"
                 " cost_krw = ?, reason = ? WHERE account_id = ? AND ticker = ?", backend),
            (total_shares, avg_price, fx, total_cost,
             (reason or existing.get("reason") or "")[:400], aid, sym),
        )
        return
    conn.execute(
        _sql(f"INSERT INTO {TABLE_POSITIONS} (account_id, ticker, market, name, currency, shares,"
             " avg_price, fx_rate, cost_krw, reason, opened_at)"
             " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", backend),
        (aid, sym, market, name or "", currency, shares, price, float(fx_rate), cost_krw,
         (reason or "")[:400], stamp),
    )


def apply_buy(ticker: str, market: str, name: str, currency: str, shares: float, price: float,
              fx_rate: float, cost_krw: float, reason: str, cash_delta: float,
              trade: dict, db_path: str | None = None,
              account_id: Any = ACCOUNT_US) -> dict:
    """매수 체결을 **한 트랜잭션으로** 반영한다(포지션 + 계좌 현금 + 체결 기록).

    `cash_delta`는 보통 `-cost_krw`다. 잔고보다 많이 차감하려 하면 `cash_below_zero`로
    실패하고 포지션도 남기지 않는다.
    """
    aid = normalize_account_id(account_id)
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    price = float(price)
    cost_krw = float(cost_krw)
    if not sym or shares <= 0 or price <= 0 or cost_krw <= 0:
        raise ValueError("invalid_position")
    get_account(aid, db_path)  # 계좌 행이 없으면 먼저 만든다(현금 UPDATE가 0건이 되지 않게)

    payload = _trade_payload(trade, aid)
    stamp = _now()
    with _connection(db_path, write=True) as (conn, backend):
        _upsert_position(conn, backend, aid, sym, market, name, currency, shares, price,
                         fx_rate, cost_krw, reason, stamp)
        cash = _apply_cash_delta(conn, backend, cash_delta, stamp, aid)
        if cash is None:  # 잔고 부족 → 트랜잭션 전체 취소(포지션도 남지 않는다)
            raise ValueError("cash_below_zero")
        payload["id"] = _insert_trade(conn, backend, payload)
        payload["cash_krw"] = cash
        payload["settle_krw"] = round(abs(float(cash_delta)), 2)
    return payload


def apply_sell(ticker: str, shares: float, cost_krw_sold: float, cash_delta: float,
               trade: dict, db_path: str | None = None,
               account_id: Any = ACCOUNT_US) -> dict:
    """매도 체결을 한 트랜잭션으로 반영한다(포지션 축소/삭제 + 현금 증가 + 체결 기록)."""
    aid = normalize_account_id(account_id)
    sym = str(ticker or "").strip().upper()
    shares = float(shares)
    if not sym or shares <= 0:
        raise ValueError("invalid_position")
    get_account(aid, db_path)

    payload = _trade_payload(trade, aid)
    stamp = _now()
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_POSITIONS} WHERE account_id = ?"
                                " AND ticker = ?", backend), (aid, sym))
        position = _row(cur)
        if not position:
            raise ValueError("position_not_found")
        remaining = round(float(position["shares"]) - shares, 8)
        if remaining <= 0.0000001:
            conn.execute(_sql(f"DELETE FROM {TABLE_POSITIONS} WHERE account_id = ? AND ticker = ?",
                              backend), (aid, sym))
        else:
            kept_cost = max(0.0, round(float(position["cost_krw"]) - float(cost_krw_sold), 2))
            conn.execute(
                _sql(f"UPDATE {TABLE_POSITIONS} SET shares = ?, cost_krw = ?"
                     " WHERE account_id = ? AND ticker = ?", backend),
                (remaining, kept_cost, aid, sym),
            )
        cash = _apply_cash_delta(conn, backend, cash_delta, stamp, aid)
        if cash is None:  # 계좌 행이 없을 수 없다 — 있으면 실패로 본다
            raise ValueError("cash_update_failed")
        payload["id"] = _insert_trade(conn, backend, payload)
        payload["cash_krw"] = cash
        payload["settle_krw"] = round(float(cash_delta), 2)
        payload["remaining_shares"] = 0.0 if remaining <= 0.0000001 else remaining
    return payload


def _normalize_trade(row: dict) -> dict:
    """체결 한 행 — 수량·금액을 float로, 정산 금액(`settle_krw`)을 계산해 붙인다."""
    out = dict(row)
    out["shares"] = float(out.get("shares") or 0.0)
    out["price"] = float(out.get("price") or 0.0)
    out["fx_rate"] = float(out.get("fx_rate") or 1.0)
    out["amount_krw"] = float(out.get("amount_krw") or 0.0)
    # 수수료 컬럼이 없던 시절 체결은 0이다(마이그레이션 기본값).
    out["fee_krw"] = float(out.get("fee_krw") or 0.0)
    sign = 1.0 if str(out.get("side") or "").upper() == "BUY" else -1.0
    out["settle_krw"] = round(out["amount_krw"] + sign * out["fee_krw"], 2)
    if out.get("realized_pnl_krw") is not None:
        out["realized_pnl_krw"] = float(out["realized_pnl_krw"])
    return out


def _normalize_equity_row(row: dict) -> dict:
    """수익률 스냅샷 한 행 — 금액을 float로."""
    out = dict(row)
    out["total_value_krw"] = float(out.get("total_value_krw") or 0.0)
    out["cash_krw"] = float(out.get("cash_krw") or 0.0)
    out["stock_value_krw"] = float(out.get("stock_value_krw") or 0.0)
    if out.get("return_pct") is not None:
        out["return_pct"] = float(out["return_pct"])
    return out


def recent_trades(limit: int = 40, db_path: str | None = None,
                  account_id: Any = ACCOUNT_US) -> list[dict]:
    aid = normalize_account_id(account_id)
    n = max(1, min(int(limit or 40), 500))
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(f"SELECT * FROM {TABLE_TRADES} WHERE account_id = ?"
                                " ORDER BY id DESC LIMIT ?", backend), (aid, n))
        rows = _rows(cur)
    return [_normalize_trade(row) for row in rows]


def realized_pnl(db_path: str | None = None, account_id: Any = ACCOUNT_US) -> float:
    """계좌의 누적 실현손익(매도 체결의 `realized_pnl_krw` 합)."""
    aid = normalize_account_id(account_id)
    with _connection(db_path) as (conn, backend):
        row = _row(conn.execute(_sql(
            f"SELECT COALESCE(SUM(realized_pnl_krw), 0) AS total FROM {TABLE_TRADES}"
            " WHERE account_id = ? AND side = 'SELL'", backend), (aid,)))
    return round(float((row or {}).get("total") or 0.0), 2)


def trade_count(db_path: str | None = None, account_id: Any = ACCOUNT_US) -> int:
    aid = normalize_account_id(account_id)
    with _connection(db_path) as (conn, backend):
        row = _row(conn.execute(_sql(f"SELECT COUNT(*) AS n FROM {TABLE_TRADES}"
                                     " WHERE account_id = ?", backend), (aid,)))
    return int((row or {}).get("n") or 0)


def clear_trades(db_path: str | None = None, account_id: Any = ACCOUNT_US) -> int:
    aid = normalize_account_id(account_id)
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"DELETE FROM {TABLE_TRADES} WHERE account_id = ?", backend),
                           (aid,))
        return int(getattr(cur, "rowcount", 0) or 0)


# --- 수익률 스냅샷 ------------------------------------------------------------


def record_equity(day: str, total_value_krw: float, cash_krw: float, stock_value_krw: float,
                  return_pct: float | None, db_path: str | None = None,
                  account_id: Any = ACCOUNT_US) -> dict:
    """계좌별 하루 한 행(KST 날짜 UNIQUE). 같은 날 다시 실행하면 최신 값으로 덮어쓴다."""
    aid = normalize_account_id(account_id)
    payload = {
        "account_id": aid,
        "date": day or kst_today_str(),
        "total_value_krw": round(float(total_value_krw), 2),
        "cash_krw": round(float(cash_krw), 2),
        "stock_value_krw": round(float(stock_value_krw), 2),
        "return_pct": None if return_pct is None else round(float(return_pct), 4),
        "recorded_at": _now(),
    }
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(
            _sql(f"INSERT INTO {TABLE_EQUITY} (account_id, date, total_value_krw, cash_krw,"
                 " stock_value_krw, return_pct, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                 " ON CONFLICT (account_id, date) DO UPDATE SET"
                 " total_value_krw = excluded.total_value_krw, cash_krw = excluded.cash_krw,"
                 " stock_value_krw = excluded.stock_value_krw, return_pct = excluded.return_pct,"
                 " recorded_at = excluded.recorded_at", backend),
            tuple(payload.values()),
        )
    return payload


def equity_history(limit: int = 180, db_path: str | None = None,
                   account_id: Any = ACCOUNT_US) -> list[dict]:
    aid = normalize_account_id(account_id)
    n = max(1, min(int(limit or 180), 1000))
    with _connection(db_path) as (conn, backend):
        cur = conn.execute(_sql(
            f"SELECT * FROM {TABLE_EQUITY} WHERE account_id = ? ORDER BY date DESC LIMIT ?",
            backend), (aid, n))
        rows = _rows(cur)
    rows.reverse()  # 오래된 날짜부터(차트 x축)
    return [_normalize_equity_row(row) for row in rows]


def clear_equity(db_path: str | None = None, account_id: Any = ACCOUNT_US) -> int:
    aid = normalize_account_id(account_id)
    with _connection(db_path, write=True) as (conn, backend):
        cur = conn.execute(_sql(f"DELETE FROM {TABLE_EQUITY} WHERE account_id = ?", backend),
                           (aid,))
        return int(getattr(cur, "rowcount", 0) or 0)


# --- FCM 토큰(체결 푸시 알림 기기) ---------------------------------------------


def register_fcm_token(token: str, platform: str = "android",
                       db_path: str | None = None, user_id: int | None = None) -> dict:
    """앱이 받은 FCM 토큰을 저장하거나 갱신한다(토큰 하나 = 기기 하나, 그 유저 소유).

    같은 토큰이 다시 오면 `updated_at`·`user_id`만 새로 쓴다(재설치·앱 실행마다 동기화해도 행이
    늘지 않음). 다른 닉네임으로 로그인한 기기는 그 유저의 알림을 받도록 소유자가 바뀐다.
    """
    clean = str(token or "").strip()
    if not clean:
        raise ValueError("empty_token")
    if len(clean) > 4096:
        raise ValueError("token_too_long")
    uid = int(user_id or DEFAULT_USER_ID)
    stamp = _now()
    payload = {"token": clean, "platform": str(platform or "").strip()[:32],
               "user_id": uid, "updated_at": stamp}
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(
            _sql(f"INSERT INTO {TABLE_FCM_TOKENS} (token, user_id, platform, updated_at)"
                 " VALUES (?, ?, ?, ?)"
                 " ON CONFLICT (token) DO UPDATE SET user_id = excluded.user_id,"
                 " platform = excluded.platform, updated_at = excluded.updated_at", backend),
            (payload["token"], payload["user_id"], payload["platform"], payload["updated_at"]),
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


def list_fcm_tokens(db_path: str | None = None, user_id: int | None = None) -> list[dict]:
    """등록된 기기 토큰(최신 순). `user_id`를 주면 그 유저의 기기만."""
    with _connection(db_path) as (conn, backend):
        if user_id is None:
            cur = conn.execute(_sql(
                f"SELECT token, user_id, platform, updated_at FROM {TABLE_FCM_TOKENS}"
                " ORDER BY updated_at DESC", backend))
        else:
            cur = conn.execute(_sql(
                f"SELECT token, user_id, platform, updated_at FROM {TABLE_FCM_TOKENS}"
                " WHERE user_id = ? ORDER BY updated_at DESC", backend), (int(user_id),))
        rows = _rows(cur)
    return [{"token": str(row.get("token") or ""),
             "user_id": int(row.get("user_id") or DEFAULT_USER_ID),
             "platform": str(row.get("platform") or ""),
             "updated_at": row.get("updated_at")} for row in rows]


def count_fcm_tokens(db_path: str | None = None, user_id: int | None = None) -> int:
    with _connection(db_path) as (conn, backend):
        if user_id is None:
            row = _row(conn.execute(_sql(f"SELECT COUNT(*) AS n FROM {TABLE_FCM_TOKENS}", backend)))
        else:
            row = _row(conn.execute(_sql(
                f"SELECT COUNT(*) AS n FROM {TABLE_FCM_TOKENS} WHERE user_id = ?", backend),
                (int(user_id),)))
    return int((row or {}).get("n") or 0)


# --- 초기화 -------------------------------------------------------------------


def reset(db_path: str | None = None, initial_cash_krw: float | None = None,
          keep_settings: bool = True, account_id: Any = ACCOUNT_US) -> dict:
    """한 계좌의 포지션·체결·스냅샷을 지우고 새 시드머니로 다시 시작한다.

    다른 계좌는 손대지 않는다. 설정(전략·자동 실행)은 기본으로 유지하고,
    `initial_cash_krw`를 주면 그 값으로 시드머니를 바꾼다. `last_run_*` 날짜는 지워
    오늘 자동 실행이 다시 가능하게 한다.
    """
    aid = normalize_account_id(account_id)
    stamp = _now()
    # 지우기·계좌 갱신·시드 스냅샷을 **연결 1회**로 처리한다(예전엔 연결을 8번 열었다).
    with _connection(db_path, write=True) as (conn, backend):
        _ensure_accounts(conn, backend)
        account = _normalize_account(
            _row(conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend),
                              (aid,))) or _default_account(aid))
        seed = (float(initial_cash_krw) if initial_cash_krw is not None
                else account["initial_cash_krw"])
        for table in (TABLE_POSITIONS, TABLE_TRADES, TABLE_EQUITY):
            conn.execute(_sql(f"DELETE FROM {table} WHERE account_id = ?", backend), (aid,))
        conn.execute(
            _sql(f"UPDATE {TABLE_ACCOUNT} SET initial_cash_krw = ?, cash_krw = ?,"
                 " last_run_date = NULL, last_run_us_date = NULL, last_run_kr_date = NULL,"
                 " updated_at = ? WHERE id = ?", backend),
            (seed, seed, stamp, aid),
        )
        conn.execute(
            _sql(f"INSERT INTO {TABLE_EQUITY} (account_id, date, total_value_krw, cash_krw,"
                 " stock_value_krw, return_pct, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                 " ON CONFLICT (account_id, date) DO UPDATE SET"
                 " total_value_krw = excluded.total_value_krw, cash_krw = excluded.cash_krw,"
                 " stock_value_krw = excluded.stock_value_krw, return_pct = excluded.return_pct,"
                 " recorded_at = excluded.recorded_at", backend),
            (aid, kst_today_str(), seed, seed, 0.0, 0.0, stamp),
        )
        stored = _row(conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} WHERE id = ?", backend),
                                   (aid,)))
    logger.info("모의투자 %s 계좌 초기화: 시드 ₩%.0f", account["market_scope"], seed)
    return _normalize_account(stored or account)


def init_store(db_path: str | None = None, account_id: Any = None,
               user_id: int | None = None) -> dict:
    """유저의 두 계좌를 준비하고(스키마·시드 스냅샷), 지정 계좌(기본 미국)를 돌려준다."""
    uid = int(user_id or DEFAULT_USER_ID)
    with _connection(db_path, write=True) as (conn, backend):
        accounts = _ensure_accounts(conn, backend, uid)
        for aid, account in accounts.items():
            has_equity = _row(conn.execute(_sql(
                f"SELECT id FROM {TABLE_EQUITY} WHERE account_id = ? LIMIT 1", backend), (aid,)))
            if not has_equity:
                conn.execute(
                    _sql(f"INSERT INTO {TABLE_EQUITY} (account_id, date, total_value_krw, cash_krw,"
                         " stock_value_krw, return_pct, recorded_at)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)"
                         " ON CONFLICT (account_id, date) DO NOTHING", backend),
                    (aid, kst_today_str(), account["cash_krw"], account["cash_krw"], 0.0, 0.0,
                     _now()),
                )
    target = normalize_account_id(account_id) if account_id is not None else None
    if target is None or target not in accounts:
        target = next((aid for aid, acc in accounts.items()
                       if acc["market_scope"] == "US"), next(iter(accounts)))
    return accounts[target]


def _next_account_id(conn, backend: str) -> int:
    """새 계좌 행에 쓸 id — id는 AUTOINCREMENT가 아니라 직접 정한다(SQLite·Postgres 공통)."""
    row = _row(conn.execute(_sql(f"SELECT COALESCE(MAX(id), 0) AS m FROM {TABLE_ACCOUNT}", backend)))
    return int((row or {}).get("m") or 0) + 1


def _ensure_accounts(conn, backend: str, user_id: int | None = None) -> dict[int, dict]:
    """한 유저의 미국·한국 계좌 행을 보장하고 `{account_id: 계좌}`를 돌려준다(연결 안에서 한 번만).

    기본 관리자(user 1)는 예전 그대로 id 1(US)·2(KR)를 쓴다. 다른 유저는 비어 있는 다음 id를
    받아 두 계좌를 새로 만든다(기존 행은 절대 건드리지 않는다).
    """
    uid = int(user_id or DEFAULT_USER_ID)
    columns = ("id", "user_id", "market_scope", "initial_cash_krw", "cash_krw", "markets",
               "strategy", "auto_enabled", "last_run_date", "last_run_us_date",
               "last_run_kr_date", "created_at", "updated_at")
    if uid == DEFAULT_USER_ID:
        # 하위 호환: 예전 계좌 두 행을 그대로(user 1 소유로) 보장한다.
        defaults = [_default_account(aid) for aid in ACCOUNTS]
        row_sql = "(" + ", ".join("?" for _ in columns) + ")"
        conn.execute(
            _sql(f"INSERT INTO {TABLE_ACCOUNT} ({', '.join(columns)})"
                 f" VALUES {', '.join(row_sql for _ in defaults)}"
                 " ON CONFLICT (id) DO NOTHING", backend),
            tuple(default[column] for default in defaults for column in columns),
        )
        conn.execute(_sql(f"UPDATE {TABLE_ACCOUNT} SET user_id = ? WHERE id IN (?, ?)"
                          " AND (user_id IS NULL OR user_id = 0)", backend),
                     (DEFAULT_USER_ID, ACCOUNT_US, ACCOUNT_KR))

    rows = _rows(conn.execute(_sql(
        f"SELECT * FROM {TABLE_ACCOUNT} WHERE user_id = ? ORDER BY id", backend), (uid,)))
    found = [_normalize_account(row) for row in rows]
    by_market = {acc["market_scope"]: acc for acc in found}
    for scope in ("US", "KR"):
        if scope in by_market:
            continue
        aid = _next_account_id(conn, backend)
        account = _default_account(aid, market=scope, user_id=uid)
        conn.execute(
            _sql(f"INSERT INTO {TABLE_ACCOUNT} ({', '.join(columns)})"
                 f" VALUES ({', '.join('?' for _ in columns)})", backend),
            tuple(account[column] for column in columns),
        )
        by_market[scope] = account
    ordered = {by_market["US"]["id"]: by_market["US"], by_market["KR"]["id"]: by_market["KR"]}
    return ordered


def ensure_accounts(db_path: str | None = None,
                    user_id: int | None = None) -> dict[int, dict]:
    """유저의 두 계좌 행을 **연결 1회**로 준비해 돌려준다(`init_store`의 계좌 조회를 대신).

    `/api/paper/configure`처럼 계좌 한 행만 필요한 호출이 예전 `init_store`(연결 5번)를
    거치면 원격 PostgreSQL에서 10초 넘게 걸렸다. 행만 필요하면 이 함수를 쓴다.
    """
    with _connection(db_path, write=True) as (conn, backend):
        return _ensure_accounts(conn, backend, user_id)


def account_id_for(user_id: int, market: str, db_path: str | None = None) -> int:
    """유저 + 시장(`"US"`/`"KR"`) → 계좌 id(그 유저의 계좌가 없으면 만든다)."""
    scope = str(market or "").strip().upper()
    if scope not in ACCOUNT_MARKETS.values():
        raise ValueError("market_invalid")
    accounts = ensure_accounts(db_path, user_id)
    for aid, account in accounts.items():
        if account["market_scope"] == scope:
            return aid
    raise ValueError("market_invalid")


def accounts_for_user(db_path: str | None = None, user_id: int | None = None) -> dict[str, dict]:
    """유저의 계좌 두 개를 `{"US": 계좌, "KR": 계좌}`로 돌려준다."""
    return {acc["market_scope"]: acc for acc in ensure_accounts(db_path, user_id).values()}


def accounts_bulk(db_path: str | None = None) -> dict:
    """모든 유저의 계좌·보유 종목을 **연결 1회**로 읽는다(랭킹용).

    반환: `{"accounts": {user_id: {account_id: 계좌}}, "positions": {account_id: [포지션]}}`.
    사용자 행(닉네임)은 `paper_auth.list_users`가 따로 준다.
    """
    with _connection(db_path) as (conn, backend):
        rows = _rows(conn.execute(_sql(f"SELECT * FROM {TABLE_ACCOUNT} ORDER BY user_id, id",
                                      backend)))
        accounts: dict[int, dict[int, dict]] = {}
        for row in rows:
            account = _normalize_account(row)
            accounts.setdefault(account["user_id"], {})[account["id"]] = account
        positions: dict[int, list[dict]] = {}
        for row in _rows(conn.execute(_sql(
                f"SELECT * FROM {TABLE_POSITIONS} ORDER BY account_id, id", backend))):
            aid = normalize_account_id(row.get("account_id"))
            positions.setdefault(aid, []).append(_normalize_position(row))
    return {"accounts": accounts, "positions": positions, "storage_backend": backend,
            "queried_at": _now()}


def _recent_per_account(conn, backend: str, table: str, order_column: str, limit: int,
                        normalize=None, account_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """지정 계좌들의 최근 N행 — 윈도 함수 한 번으로 계좌별 LIMIT을 맞춘다.

    계좌마다 따로 읽으면 원격 PostgreSQL 왕복이 두 배(0.5초×2)가 되므로 한 번에 읽는다.
    반환은 `account_id` → 행 목록(최신이 앞). 윈도 함수가 없는 아주 오래된 SQLite에서는
    계좌별 조회로 물러난다(결과는 같다).
    """
    ids = [int(aid) for aid in (account_ids or ACCOUNTS)]
    sql = (f"SELECT * FROM (SELECT {table}.*, ROW_NUMBER() OVER"
           f" (PARTITION BY account_id ORDER BY {order_column} DESC) AS rn"
           f" FROM {table}) AS t WHERE rn <= ? ORDER BY account_id, rn")
    try:
        rows = _rows(conn.execute(_sql(sql, backend), (limit,)))
    except Exception as exc:  # pragma: no cover - 표준 sqlite3는 윈도 함수를 지원
        logger.warning("%s 최근 행 조회 실패 → 계좌별 조회로 대체: %s: %s",
                       table, type(exc).__name__, str(exc)[:120])
        rows = []
        for aid in ids:
            rows.extend(_rows(conn.execute(_sql(
                f"SELECT * FROM {table} WHERE account_id = ?"
                f" ORDER BY {order_column} DESC LIMIT ?", backend), (aid, limit))))
    out: dict[int, list[dict]] = {aid: [] for aid in ids}
    for row in rows:
        data = dict(row)
        data.pop("rn", None)  # 윈도 함수가 붙인 순번은 응답에 넣지 않는다
        aid = normalize_account_id(data.get("account_id"))
        if aid not in out:
            continue
        out.setdefault(aid, []).append(normalize(data) if normalize else data)
    return out


def snapshot(limit_trades: int = 40, limit_equity: int = 180,
             db_path: str | None = None, user_id: int | None = None) -> dict:
    """두 계좌의 화면 데이터를 **DB 연결 1회**로 모아 온다(`/api/paper/status`용).

    왜 필요한가
    -----------
    상태 화면 하나가 계좌별 조회 함수(`get_account`·`list_positions`·`realized_pnl`·
    `recent_trades`·`equity_history`)를 5번씩 부르고 `init_store`까지 거치면 연결을
    **14번** 열었다. 렌더 무료 인스턴스에서 Neon PostgreSQL까지 연결 한 번이 1초 안팎이라
    탭을 누를 때마다 **약 19초**를 기다렸다. 화면이 한 번에 그리는 것을 같은 연결·같은
    시점에서 읽어 연결을 1번으로 줄인다.

    반환: `accounts`·`positions`·`realized`·`trades`·`equity`를 `account_id` 키로 나눈 dict와
    `storage_backend`·`queried_at`. 값의 모양은 개별 조회 함수와 같다(정규화 동일).
    계좌 행·시드 스냅샷이 없으면 이 연결 안에서 만들어 둔다(기존 `init_store`와 같은 결과).
    `user_id`를 주면 **그 유저의 두 계좌만** 읽는다(기본 관리자 1).
    """
    trades_n = max(1, min(int(limit_trades or 40), 500))
    equity_n = max(1, min(int(limit_equity or 180), 1000))
    with _connection(db_path, write=True) as (conn, backend):
        # 1) 그 유저의 두 계좌 행을 보장한다 — 한 문장으로 넣어 왕복을 한 번만 쓴다.
        accounts = _ensure_accounts(conn, backend, user_id)
        account_ids = list(accounts)
        accounts_raw = {aid: account for aid, account in accounts.items()}

        # 2) 보유 종목 — 그 유저의 두 계좌만(account_id로 나뉜다).
        positions: dict[int, list[dict]] = {aid: [] for aid in account_ids}
        placeholders = ", ".join("?" for _ in account_ids)
        for row in _rows(conn.execute(_sql(
                f"SELECT * FROM {TABLE_POSITIONS} WHERE account_id IN ({placeholders})"
                " ORDER BY account_id, id", backend), tuple(account_ids))):
            aid = normalize_account_id(row.get("account_id"))
            positions.setdefault(aid, []).append(_normalize_position(row))

        # 3) 계좌별 실현손익 합계 — 한 요청으로 묶는다.
        realized: dict[int, float] = {aid: 0.0 for aid in account_ids}
        for row in _rows(conn.execute(_sql(
                f"SELECT account_id, COALESCE(SUM(realized_pnl_krw), 0) AS total"
                f" FROM {TABLE_TRADES} WHERE side = 'SELL' AND account_id IN ({placeholders})"
                " GROUP BY account_id", backend), tuple(account_ids))):
            aid = normalize_account_id(row.get("account_id"))
            realized[aid] = round(float(row.get("total") or 0.0), 2)

        # 4) 최근 체결·수익률 스냅샷 — 계좌별 LIMIT을 윈도 함수 한 번으로 맞춘다.
        #    (계좌마다 따로 읽으면 원격 DB 왕복이 두 배가 된다)
        trades = _recent_per_account(conn, backend, TABLE_TRADES, "id", trades_n,
                                     _normalize_trade, account_ids)
        equity = _recent_per_account(conn, backend, TABLE_EQUITY, "date", equity_n,
                                     _normalize_equity_row, account_ids)
        for aid in account_ids:
            equity.setdefault(aid, []).reverse()  # 오래된 날짜부터(차트 x축)
            trades.setdefault(aid, [])
        for aid in account_ids:
            if equity[aid]:
                continue
            # 스냅샷이 하나도 없는 계좌는 시드 시점을 남긴다(기존 init_store 동작).
            account = accounts[aid]
            conn.execute(
                _sql(f"INSERT INTO {TABLE_EQUITY} (account_id, date, total_value_krw, cash_krw,"
                     " stock_value_krw, return_pct, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                     " ON CONFLICT (account_id, date) DO UPDATE SET"
                     " total_value_krw = excluded.total_value_krw, cash_krw = excluded.cash_krw,"
                     " stock_value_krw = excluded.stock_value_krw, return_pct = excluded.return_pct,"
                     " recorded_at = excluded.recorded_at", backend),
                (aid, kst_today_str(), account["cash_krw"], account["cash_krw"], 0.0, 0.0,
                 _now()),
            )
            seeded = _rows(conn.execute(_sql(
                f"SELECT * FROM {TABLE_EQUITY} WHERE account_id = ?"
                " ORDER BY date DESC LIMIT ?", backend), (aid, equity_n)))
            seeded.reverse()
            equity[aid] = [_normalize_equity_row(row) for row in seeded]

    return {
        "accounts": accounts,
        "positions": {aid: positions.get(aid, []) for aid in account_ids},
        "realized": {aid: realized.get(aid, 0.0) for aid in account_ids},
        "trades": {aid: trades.get(aid, []) for aid in account_ids},
        "equity": {aid: equity.get(aid, []) for aid in account_ids},
        "storage_backend": backend,
        "queried_at": _now(),
    }


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
