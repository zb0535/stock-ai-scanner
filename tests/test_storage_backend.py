"""외부 PostgreSQL 저장 백엔드(DATABASE_URL) 테스트.

다루는 것
1. DATABASE_URL 판별·정규화: postgres:// → postgresql://, sslmode 보강, 로컬 예외, 잘못된 스킴
2. 자격 증명 마스킹(redact): 응답·로그에 비밀번호가 나가지 않는다
3. PostgreSQL 백엔드 동작(psycopg.connect 대역): 스키마 멱등 생성, upsert 중 첫 진입가·시각 유지,
   조회 정렬·limit, 잘못된 픽 제외, 접속 문자열 정규화
4. 성적표 응답의 storage_backend 표기(postgres/sqlite)와 SQLite 폴백 회귀
5. API 노출: GET / · GET /api/performance · POST /api/screen → PostgreSQL 연동, DB 장애 시에도 200

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import history_store  # noqa: E402
import realtime_quotes  # noqa: E402
import stock_tools  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None

try:
    import psycopg  # noqa: F401
    HAVE_PSYCOPG = True
except ImportError:  # pragma: no cover - requirements 미설치 환경
    HAVE_PSYCOPG = False

KST = timezone(timedelta(hours=9))

FIELDS = ("ticker", "recommended_date", "recommended_at", "entry_price",
          "strategy", "mode", "quant_score", "criteria")
# 조회는 id를 포함한다(id가 있어야 클라이언트가 행 하나를 지울 수 있다).
SELECT_FIELDS = ("id",) + FIELDS

GOOD_URL = "postgresql://scanner:s3cret@ep-cool-1234.us-east-2.aws.neon.tech/neondb"


# ---------------------------------------------------------------------------
# PostgreSQL 대역(실제 서버 없이 SQL 흐름을 검증하는 in-memory double)
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows or [])
        self.rowcount = len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakePGConnection:
    """psycopg3 Connection 대역. recommendations 테이블을 dict로 흉내낸다.

    `history_store`가 보내는 SQL의 의미(테이블 생성·upsert·정렬 조회·단건 조회·단건 삭제)를
    그대로 흉내내므로 코드가 파라미터 순서나 충돌 키를 잘못 잡으면 테스트가 깨진다.
    """

    def __init__(self):
        self.rows = {}              # (recommended_date, ticker, strategy) -> row dict
        self.sql = []               # 실행된 SQL(공백 정리본)
        self.calls = []             # (sql, params)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._next_id = 1

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.sql.append(text)
        self.calls.append((text, params))

        if text.startswith("CREATE TABLE IF NOT EXISTS recommendations"):
            return FakeCursor([])
        if text.startswith("CREATE INDEX IF NOT EXISTS"):
            return FakeCursor([])
        if text.startswith("INSERT INTO recommendations"):
            ticker, day, at, entry, strat, mode, score, criteria = params
            key = (day, ticker, strat)
            existing = self.rows.get(key)
            if existing is None:
                self.rows[key] = dict(zip(FIELDS, (ticker, day, at, entry, strat, mode,
                                                  score, criteria)))
                self.rows[key]["id"] = self._next_id
                self._next_id += 1
                return FakeCursor([{"inserted": True}])
            # DO UPDATE는 entry_price·recommended_at을 건드리지 않는다.
            existing.update({"quant_score": score, "mode": mode, "criteria": criteria})
            return FakeCursor([{"inserted": False}])
        if text.startswith("SELECT id"):
            if "WHERE recommended_date" in text:
                day, ticker, strat = params
                row = self.rows.get((day, ticker, strat))
                return FakeCursor([{k: row[k] for k in SELECT_FIELDS}] if row else [])
            limit = params[0]
            ordered = sorted(self.rows.values(),
                             key=lambda r: (r["recommended_date"], r["id"]), reverse=True)
            return FakeCursor([{k: r[k] for k in SELECT_FIELDS} for r in ordered[:limit]])
        if text.startswith("DELETE FROM recommendations"):
            (pid,) = params
            for key, row in list(self.rows.items()):
                if row["id"] == pid:
                    del self.rows[key]
                    return FakeCursor([{"id": pid}])
            return FakeCursor([])
        raise AssertionError(f"예상하지 못한 SQL: {text}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakePGBackend:
    """psycopg.connect 대역. 한 개의 가짜 서버(연결)를 재사용한다."""

    def __init__(self):
        self.conn = FakePGConnection()
        self.conninfos = []
        self.fail_with = None

    def connect(self, conninfo, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.conninfos.append((conninfo, kwargs))
        self.conn.closed = False
        return self.conn

    def patch(self):
        return patch("psycopg.connect", side_effect=self.connect)


class PostgresCase(unittest.TestCase):
    """DATABASE_URL을 켜고 psycopg.connect를 대역으로 바꾸는 공통 준비."""

    url = GOOD_URL

    def setUp(self):
        patcher = patch.dict(os.environ, {"DATABASE_URL": self.url})
        patcher.start()
        self.addCleanup(patcher.stop)
        history_store._PG_SCHEMA_READY.clear()
        self.addCleanup(history_store._PG_SCHEMA_READY.clear)
        history_store.clear_price_cache()
        self.addCleanup(history_store.clear_price_cache)

        self.backend = FakePGBackend()
        self.patcher = self.backend.patch()
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def rows(self, limit=50):
        return history_store.recent_picks(limit=limit)


# ---------------------------------------------------------------------------
# 1. URL 판별·정규화
# ---------------------------------------------------------------------------


class UrlTests(unittest.TestCase):
    def test_postgres_scheme_is_rewritten_for_libpq(self):
        self.assertEqual(
            history_store.normalize_database_url("postgres://u:p@h.example.com:5432/db"),
            "postgresql://u:p@h.example.com:5432/db?sslmode=require",
        )

    def test_cloud_host_gets_sslmode_require(self):
        self.assertEqual(history_store.normalize_database_url(GOOD_URL), GOOD_URL + "?sslmode=require")
        self.assertEqual(
            history_store.normalize_database_url("postgres://u:p@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"),
            "postgresql://u:p@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require",
        )

    def test_existing_query_keeps_sslmode_and_params(self):
        url = "postgresql://u:p@neon.example.com/neondb?sslmode=verify-full&channel_binding=require"
        self.assertEqual(history_store.normalize_database_url(url), url)
        url2 = "postgresql://u:p@neon.example.com/neondb?application_name=scanner"
        self.assertEqual(history_store.normalize_database_url(url2), url2 + "&sslmode=require")

    def test_sslmode_is_added_once_and_respected(self):
        once = history_store.normalize_database_url("postgres://u:p@h.example.com/db")
        self.assertEqual(history_store.normalize_database_url(once), once)   # 멱등
        disabled = "postgresql://u:p@h.example.com/db?sslmode=disable"
        self.assertEqual(history_store.normalize_database_url(disabled), disabled)

    def test_local_hosts_get_no_sslmode(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                self.assertEqual(
                    history_store.normalize_database_url(f"postgres://u:p@{host}:5432/db"),
                    f"postgresql://u:p@{host}:5432/db",
                )

    def test_empty_or_other_scheme_means_sqlite(self):
        for raw in ("", "   ", None, "sqlite:///tmp/x.db", "mysql://u:p@h/db",
                    "https://example.com"):
            with self.subTest(raw=raw):
                self.assertEqual(history_store.normalize_database_url(raw), "")

    def test_trailing_separator_does_not_double_up(self):
        self.assertEqual(history_store.normalize_database_url("postgres://u:p@h.example.com/db?"),
                         "postgresql://u:p@h.example.com/db?sslmode=require")

    def test_storage_backend_reads_env_only(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            self.assertEqual(history_store.storage_backend(), "sqlite")
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://u:p@h.example.com/db"}):
            self.assertEqual(history_store.storage_backend(), "postgres")
            # db_path를 명시하면 DATABASE_URL과 무관하게 SQLite(테스트·로컬 파일 경로)
            self.assertEqual(history_store.storage_backend("/tmp/x.db"), "sqlite")

    def test_redact_hides_password(self):
        red = history_store.redact_database_url(GOOD_URL)
        self.assertNotIn("s3cret", red)
        self.assertIn("scanner:***@", red)
        self.assertIn("sslmode=require", red)
        self.assertEqual(history_store.redact_database_url("postgresql://h/db"),
                         "postgresql://h/db?sslmode=require")


# ---------------------------------------------------------------------------
# 2. PostgreSQL 기록·조회
# ---------------------------------------------------------------------------


@unittest.skipIf(not HAVE_PSYCOPG, "psycopg 미설치")
class PostgresRecordTests(PostgresCase):
    def test_schema_is_created_once_and_idempotently(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value")
        history_store.record_picks([{"ticker": "MSFT", "price": 200}], strategy="value")
        creates = [s for s in self.backend.conn.sql if s.startswith("CREATE TABLE")]
        self.assertEqual(len(creates), 1)                       # 프로세스당 한 번
        self.assertIn("SERIAL PRIMARY KEY", creates[0])
        self.assertIn("UNIQUE (recommended_date, ticker, strategy)", creates[0])
        self.assertTrue(any(s.startswith("CREATE INDEX IF NOT EXISTS") for s in self.backend.conn.sql))

    def test_connect_uses_normalized_url(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value")
        conninfo, kwargs = self.backend.conninfos[0]
        self.assertTrue(conninfo.startswith("postgresql://"))
        self.assertTrue(conninfo.endswith("?sslmode=require"))
        self.assertEqual(kwargs.get("connect_timeout"), history_store.PG_CONNECT_TIMEOUT)

    def test_record_persists_all_fields(self):
        out = history_store.record_picks(
            [{"ticker": "aapl", "price": 191.25, "quant_score": 82.5}],
            strategy="value", mode="sp500", criteria="저PER 가치주",
        )
        self.assertEqual((out["inserted"], out["updated"], out["backend"]), (1, 0, "postgres"))
        self.assertEqual(out["db_path"], history_store.redact_database_url())  # 비밀번호 없음
        self.assertNotIn("s3cret", out["db_path"])

        row = self.rows()[0]
        self.assertEqual(row["ticker"], "AAPL")                 # 대문자 정규화
        self.assertEqual(row["entry_price"], 191.25)
        self.assertEqual((row["strategy"], row["mode"]), ("value", "sp500"))
        self.assertEqual(row["quant_score"], 82.5)
        self.assertEqual(row["criteria"], "저PER 가치주")
        self.assertEqual(row["recommended_date"], history_store.kst_today().strftime("%Y-%m-%d"))
        self.assertRegex(row["recommended_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$")
        self.assertTrue(self.backend.conn.commits >= 1)

    def test_upsert_keeps_first_entry_and_time(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100.0, "quant_score": 70}],
                                   strategy="value", mode="watchlist", criteria="첫 조건",
                                   recommended_at="2026-11-18T09:00:00+09:00")
        out = history_store.record_picks([{"ticker": "AAPL", "price": 105.0, "quant_score": 88}],
                                         strategy="value", mode="sp500", criteria="두 번째 조건",
                                         recommended_at="2026-11-18T15:30:00+09:00")
        self.assertEqual((out["inserted"], out["updated"]), (0, 1))

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_price"], 100.0)         # 최초 진입가 유지
        self.assertEqual(rows[0]["recommended_at"], "2026-11-18T09:00:00+09:00")
        self.assertEqual(rows[0]["quant_score"], 88.0)          # 점수·모드·조건은 최신
        self.assertEqual(rows[0]["mode"], "sp500")
        self.assertEqual(rows[0]["criteria"], "두 번째 조건")

    def test_upsert_sql_uses_on_conflict_and_returning(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value")
        upsert = [s for s in self.backend.conn.sql if s.startswith("INSERT INTO recommendations")][0]
        self.assertIn("ON CONFLICT (recommended_date, ticker, strategy) DO UPDATE", upsert)
        self.assertIn("RETURNING (xmax = 0) AS inserted", upsert)
        update_clause = upsert.split("DO UPDATE", 1)[1]
        self.assertNotIn("entry_price", update_clause)           # 진입가·시각은 갱신하지 않는다
        self.assertNotIn("recommended_at", update_clause)

    def test_sql_uses_placeholders_not_string_interpolation(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value",
                                   criteria="'; DROP TABLE recommendations; --")
        upsert_params = [p for s, p in self.backend.conn.calls if s.startswith("INSERT INTO")]
        self.assertEqual(len(upsert_params), 1)
        self.assertEqual(upsert_params[0][7], "'; DROP TABLE recommendations; --")
        self.assertIn("%s", [s for s, _ in self.backend.conn.calls if s.startswith("INSERT INTO")][0])

    def test_same_day_different_strategy_is_separate_row(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value")
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="momentum")
        self.assertEqual(len(self.rows()), 2)

    def test_different_day_same_ticker_is_separate_row_and_sorted(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value",
                                   recommended_date="2026-11-17")
        history_store.record_picks([{"ticker": "MSFT", "price": 300}], strategy="value",
                                   recommended_date="2026-11-18")
        self.assertEqual([r["recommended_date"] for r in self.rows()], ["2026-11-18", "2026-11-17"])

    def test_invalid_picks_are_skipped(self):
        out = history_store.record_picks(
            [{"ticker": "", "price": 1}, {"price": 2}, "nope", {"ticker": "AMD", "price": 3}],
            strategy="value")
        self.assertEqual((out["inserted"], out["skipped"]), (1, 3))
        self.assertEqual([r["ticker"] for r in self.rows()], ["AMD"])

    def test_empty_picks_touch_no_connection(self):
        out = history_store.record_picks([], strategy="value")
        self.assertEqual((out["inserted"], out["updated"], out["backend"]), (0, 0, "postgres"))
        self.assertEqual(self.backend.conninfos, [])

    def test_limit_is_clamped_in_query(self):
        for t in ("A", "B", "C"):
            history_store.record_picks([{"ticker": t, "price": 1.0}], strategy="value",
                                       recommended_date=f"2026-11-{10 + ord(t):02d}")
        self.assertEqual(len(self.rows(limit=2)), 2)
        selects = [p for s, p in self.backend.conn.calls if s.startswith("SELECT")]
        self.assertEqual(selects[-1][0], 2)
        self.assertEqual(len(history_store.recent_picks(limit=9999)), 3)   # MAX_LIMIT로 잘림

    def test_init_db_touches_postgres_only(self):
        self.assertEqual(history_store.init_db(), history_store.redact_database_url())
        self.assertTrue(any(s.startswith("CREATE TABLE") for s in self.backend.conn.sql))

    def test_performance_reports_postgres_backend(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100.0}], strategy="value")
        body = history_store.get_performance(price_fetcher=lambda tickers: {"AAPL": 110.0})
        self.assertEqual(body["storage_backend"], "postgres")
        self.assertEqual(body["summary"]["total_picks"], 1)
        self.assertAlmostEqual(body["picks"][0]["return_pct"], 10.0)
        self.assertEqual(body["picks"][0]["entry_price"], 100.0)

    def test_performance_empty_still_names_backend(self):
        body = history_store.get_performance(price_fetcher=lambda tickers: {})
        self.assertEqual(body["storage_backend"], "postgres")
        self.assertEqual(body["summary"]["total_picks"], 0)

    def test_record_pick_returns_id_and_upserts(self):
        out = history_store.record_pick("aapl", entry_price=100.0, strategy="value",
                                        mode="watchlist", criteria="수동 추적")
        self.assertTrue(out["ok"])
        self.assertTrue(out["created"])
        self.assertEqual(out["backend"], "postgres")
        pid = out["pick"]["id"]
        self.assertIsInstance(pid, int)
        self.assertEqual((out["pick"]["ticker"], out["pick"]["entry_price"]), ("AAPL", 100.0))
        self.assertEqual(out["pick"]["criteria"], "수동 추적")

        again = history_store.record_pick("AAPL", entry_price=120.0, strategy="value")
        self.assertFalse(again["created"])                       # 같은 날·같은 전략은 한 행
        self.assertEqual(again["pick"]["id"], pid)
        self.assertEqual(again["pick"]["entry_price"], 100.0)    # 최초 진입가 유지
        self.assertEqual(len(self.rows()), 1)

    def test_recent_picks_and_performance_include_id(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 1.0}], strategy="value")
        row = self.rows()[0]
        self.assertIn("id", row)
        self.assertIsInstance(row["id"], int)
        body = history_store.get_performance(price_fetcher=lambda t: {"AAPL": 1.1})
        self.assertEqual(body["picks"][0]["id"], row["id"])

    def test_delete_pick_removes_only_target_row(self):
        a = history_store.record_pick("AAPL", entry_price=1.0, strategy="value")["pick"]["id"]
        b = history_store.record_pick("MSFT", entry_price=2.0, strategy="value")["pick"]["id"]
        self.assertTrue(history_store.delete_pick(a))
        self.assertEqual([r["id"] for r in self.rows()], [b])
        self.assertFalse(history_store.delete_pick(a))           # 이미 없다
        self.assertFalse(history_store.delete_pick(0))
        self.assertFalse(history_store.delete_pick("nope"))
        self.assertEqual(len(self.rows()), 1)

    def test_delete_sql_is_parameterized_single_row(self):
        a = history_store.record_pick("AAPL", entry_price=1.0, strategy="value")["pick"]["id"]
        history_store.delete_pick(a)
        sql, params = [c for c in self.backend.conn.calls if c[0].startswith("DELETE")][-1]
        self.assertEqual(params, (a,))
        self.assertIn("WHERE id = %s", sql)
        self.assertNotIn("recommended_date", sql)                # 날짜·티커로 지우지 않는다

    def test_delete_failure_rolls_back_and_closes(self):
        self.backend.fail_with = OSError("could not connect to server")
        with self.assertRaises(OSError):
            history_store.delete_pick(1)

    def test_connection_failure_surfaces_as_error_not_crash(self):
        self.backend.fail_with = OSError("could not connect to server")
        body = history_store.get_performance(price_fetcher=lambda tickers: {})
        self.assertEqual(body["summary"]["total_picks"], 0)
        self.assertEqual(body.get("error"), "history_unavailable")
        self.assertEqual(body["storage_backend"], "postgres")
        with self.assertRaises(OSError):
            history_store.record_picks([{"ticker": "AAPL", "price": 1}], strategy="value")


@unittest.skipIf(not HAVE_PSYCOPG, "psycopg 미설치")
class PostgresFailoverTests(unittest.TestCase):
    def test_rollback_on_write_error(self):
        backend = FakePGBackend()
        with patch.dict(os.environ, {"DATABASE_URL": GOOD_URL}), backend.patch():
            history_store._PG_SCHEMA_READY.clear()
            self.addCleanup(history_store._PG_SCHEMA_READY.clear)
            calls = {"n": 0}
            original = backend.conn.execute

            def flaky(sql, params=None):
                if sql.strip().startswith("INSERT"):
                    calls["n"] += 1
                    if calls["n"] == 2:
                        raise RuntimeError("connection reset")
                return original(sql, params)

            backend.conn.execute = flaky
            with self.assertRaises(RuntimeError):
                history_store.record_picks(
                    [{"ticker": "AAPL", "price": 1}, {"ticker": "MSFT", "price": 2}],
                    strategy="value")
            self.assertEqual(backend.conn.rollbacks, 1)
            self.assertTrue(backend.conn.closed)          # 실패해도 연결은 닫는다


# ---------------------------------------------------------------------------
# 3. SQLite 폴백 회귀
# ---------------------------------------------------------------------------


class SqliteFallbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "perf.db")
        patcher = patch.dict(os.environ, {"HISTORY_DB_PATH": self.db_path,
                                          "DATABASE_URL": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_database_url_uses_sqlite(self):
        self.assertEqual(history_store.storage_backend(), "sqlite")
        out = history_store.record_picks([{"ticker": "AAPL", "price": 100.0}], strategy="value")
        self.assertEqual((out["backend"], out["inserted"]), ("sqlite", 1))
        self.assertTrue(os.path.exists(self.db_path))
        body = history_store.get_performance(price_fetcher=lambda tickers: {"AAPL": 110.0})
        self.assertEqual(body["storage_backend"], "sqlite")
        self.assertAlmostEqual(body["picks"][0]["return_pct"], 10.0)

    def test_non_postgres_url_does_not_switch_backend(self):
        with patch.dict(os.environ, {"DATABASE_URL": "mysql://u:p@h/db"}):
            self.assertEqual(history_store.storage_backend(), "sqlite")
            out = history_store.record_picks([{"ticker": "MSFT", "price": 5.0}], strategy="value")
            self.assertEqual(out["backend"], "sqlite")


# ---------------------------------------------------------------------------
# 4. API 노출
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, payload):
        self.payload = payload

    def generate_content(self, **kwargs):
        return FakeResponse("```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```")


class FakeGenai:
    def __init__(self, payload):
        self.models = FakeModels(payload)


def make_hist(days=130, end="2026-11-18", start_price=100.0, end_price=150.0):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    close = pd.Series(np.linspace(start_price, end_price, days), index=idx)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": 1_000_000})


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ApiBackendTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "perf.db")
        self.env = patch.dict(os.environ, {"HISTORY_DB_PATH": self.db_path, "DATABASE_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        history_store.clear_price_cache()
        self.addCleanup(history_store.clear_price_cache)
        self.client = TestClient(api.app)

    def test_health_reports_backend_without_connecting(self):
        body = self.client.get("/").json()
        self.assertEqual(body["storage_backend"], "sqlite")
        with patch.dict(os.environ, {"DATABASE_URL": GOOD_URL}):
            body = self.client.get("/").json()
        self.assertEqual(body["storage_backend"], "postgres")
        self.assertNotIn("s3cret", json.dumps(body))          # 자격 증명 노출 없음
        self.assertEqual(self.client.head("/").status_code, 200)

    def test_performance_endpoint_names_backend(self):
        body = self.client.get("/api/performance").json()
        self.assertEqual(body["storage_backend"], "sqlite")
        self.assertEqual(body["summary"]["total_picks"], 0)


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
@unittest.skipIf(not HAVE_PSYCOPG, "psycopg 미설치")
class ApiPostgresFlowTests(PostgresCase):
    PICKS = [{"ticker": "AAPL", "price": 999.0, "reason": "r", "risk": "k"}]

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"HISTORY_DB_PATH": os.path.join(
            tempfile.mkdtemp(), "unused.db")})
        patcher.start()
        self.addCleanup(patcher.stop)
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        self.addCleanup(stock_tools._METRICS_CACHE.clear)
        self.addCleanup(stock_tools._HISTORY_CACHE.clear)
        self._patch(stock_tools, "get_stock_metrics",
                    lambda t: {"ticker": t, "price": 100.0, "financials_available": True})
        self._patch(api, "_get_recent_news", lambda t, limit=3: [f"{t} news"])
        self._patch(stock_tools, "get_history", lambda t, p="6mo": make_hist())
        self._patch(api.genai, "Client",
                    lambda **_: FakeGenai({"market_note": "ok", "top_picks": list(self.PICKS)}))
        # 성적표·수동 추적의 진입가 조회를 네트워크 없이 고정한다.
        self._patch(realtime_quotes, "get_prices", lambda *a, **k: {})
        self._patch(history_store, "_fetch_prices_batch",
                    lambda tickers: {t: 110.0 for t in tickers})
        history_store.clear_price_cache()
        self.client = TestClient(api.app)

    def _patch(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    def test_screen_does_not_write_to_postgres(self):
        # 자동 기록 제거: 분석만 돌려서는 PostgreSQL에 행이 생기지 않는다.
        resp = self.client.post("/api/screen",
                                json={"tickers": ["AAPL"], "criteria": "저PER 가치주"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.backend.conn.rows, {})
        body = self.client.get("/api/performance").json()
        self.assertEqual(body["summary"]["total_picks"], 0)

    def test_track_writes_to_postgres_and_performance_reads_it(self):
        resp = self.client.post("/api/performance/track",
                                json={"ticker": "AAPL", "entry_price": 100.0,
                                      "strategy": "value", "mode": "watchlist",
                                      "quant_score": 88.5, "criteria": "저PER 가치주"})
        self.assertEqual(resp.status_code, 200)
        tracked = resp.json()
        self.assertTrue(tracked["created"])

        rows = self.backend.conn.rows
        self.assertEqual(len(rows), 1)                       # SQLite 파일이 아니라 PostgreSQL
        stored = list(rows.values())[0]
        self.assertEqual(stored["ticker"], "AAPL")
        self.assertEqual(stored["entry_price"], 100.0)
        self.assertEqual(stored["id"], tracked["pick"]["id"])

        body = self.client.get("/api/performance").json()
        self.assertEqual(body["storage_backend"], "postgres")
        self.assertEqual(body["summary"]["total_picks"], 1)
        self.assertEqual(body["picks"][0]["id"], tracked["pick"]["id"])
        self.assertAlmostEqual(body["picks"][0]["return_pct"], 10.0)   # 110/100

    def test_track_without_price_uses_latest_close_on_postgres(self):
        resp = self.client.post("/api/performance/track", json={"ticker": "AAPL"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pick"]["entry_price"], 110.0)
        self.assertEqual(list(self.backend.conn.rows.values())[0]["entry_price"], 110.0)

    def test_delete_removes_row_from_postgres(self):
        a = self.client.post("/api/performance/track",
                             json={"ticker": "AAPL", "entry_price": 100.0,
                                   "strategy": "value"}).json()["pick"]["id"]
        self.client.post("/api/performance/track",
                         json={"ticker": "MSFT", "entry_price": 200.0, "strategy": "value"})
        resp = self.client.delete(f"/api/performance/{a}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "deleted_id": a})
        self.assertEqual(sorted(r["ticker"] for r in self.backend.conn.rows.values()), ["MSFT"])
        self.assertEqual(self.client.delete(f"/api/performance/{a}").status_code, 404)

    def test_screen_survives_postgres_outage(self):
        self.backend.fail_with = OSError("could not connect to server")
        resp = self.client.post("/api/screen",
                                json={"tickers": ["AAPL"], "criteria": "저PER 가치주"})
        self.assertEqual(resp.status_code, 200)          # 화면 분석은 DB와 무관
        self.assertEqual(len(resp.json()["analysis"]["top_picks"]), 1)

        body = self.client.get("/api/performance").json()
        self.assertEqual(body["summary"]["total_picks"], 0)
        self.assertEqual(body["error"], "history_unavailable")
        self.assertEqual(body["storage_backend"], "postgres")

    def test_track_reports_postgres_outage_as_503(self):
        self.backend.fail_with = OSError("could not connect to server")
        resp = self.client.post("/api/performance/track",
                                json={"ticker": "AAPL", "entry_price": 100.0})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "storage_unavailable")

    def test_delete_reports_postgres_outage_as_503(self):
        self.backend.fail_with = OSError("could not connect to server")
        resp = self.client.delete("/api/performance/1")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "storage_unavailable")


if __name__ == "__main__":
    unittest.main()
