"""닉네임 + 4자리 PIN 멀티 유저 & 지인 수익률 랭킹 테스트 (v2.11).

다루는 것
1. 기존 계좌·체결은 기본 관리자(user 1) 소유로 **그대로** 남는다(마이그레이션).
2. `POST /api/paper/auth`: 새 닉네임이면 유저 + 전용 미국·한국 계좌(각 100만 원),
   기존 닉네임은 PIN 검증(불일치 401), PIN은 해시로만 저장된다.
3. 토큰(`X-Paper-User-Token`)으로 계좌가 분리된다 — 다른 유저의 계좌·체결이 보이지 않고,
   설정·매매도 자기 계좌에만 적용된다. 토큰이 틀리면 401.
4. `GET /api/paper/leaderboard`: 모든 유저의 합산 수익률 순위, PIN·토큰은 응답에 없다.
5. 실제 계좌(브로커) API는 지인(비관리자) 토큰에 403, 관리자·무토큰(구 클라이언트)은 허용.
6. FCM 기기 토큰은 유저별로 분리된다.
7. 자동 실행이 지인 계좌까지 돌고, 같은 전략의 후보는 한 번만 수집한다.

실행: python -m pytest tests/test_paper_multiuser.py -q
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paper_auth  # noqa: E402
import paper_store  # noqa: E402
import paper_trading  # noqa: E402

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - httpx 미설치
    TestClient = None

SEED = 1_000_000.0


class MultiUserCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "paper.db")
        patcher = patch.dict(os.environ, {"HISTORY_DB_PATH": self.db_path, "PAPER_AUTO": "0",
                                          "PAPER_STATUS_PREWARM": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        paper_trading.clear_caches()
        paper_auth.reset_attempts()
        paper_auth._SECRET_CACHE.clear()
        patcher2 = patch.dict(os.environ, {"PAPER_TOKEN_SECRET": "test-secret"})
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def tearDown(self):
        paper_trading.clear_caches()
        paper_auth.reset_attempts()
        paper_auth._SECRET_CACHE.clear()

    def login(self, nickname, pin="1234"):
        user, created = paper_auth.authenticate(nickname, pin, self.db_path)
        token, _ = paper_auth.sign_token(user, self.db_path)
        return user, created, token


# ---------------------------------------------------------------------------
# 1. 기존 계좌 보존·관리자 이어받기
# ---------------------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE paper_account (
    id               INTEGER PRIMARY KEY,
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
CREATE TABLE paper_positions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
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
    UNIQUE(ticker)
);
CREATE TABLE paper_trades (
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
    realized_pnl_krw  REAL,
    reason            TEXT    NOT NULL DEFAULT '',
    ai_source         TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE paper_equity_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL UNIQUE,
    total_value_krw REAL    NOT NULL,
    cash_krw        REAL    NOT NULL,
    stock_value_krw REAL    NOT NULL,
    return_pct      REAL,
    recorded_at     TEXT
);
CREATE TABLE paper_fcm_tokens (
    token      TEXT PRIMARY KEY,
    platform   TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
"""


class MigrationAndAdminTests(MultiUserCase):
    def _legacy_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute("INSERT INTO paper_account (id, initial_cash_krw, cash_krw, markets,"
                         " strategy, auto_enabled, last_run_us_date, created_at, updated_at)"
                         " VALUES (1, 1000000, 700000, 'ALL', 'balanced', 1, '2026-11-27', 'x', 'y')")
            conn.execute("INSERT INTO paper_account (id, initial_cash_krw, cash_krw, markets,"
                         " strategy, auto_enabled, created_at, updated_at)"
                         " VALUES (2, 1000000, 950000, 'KR', 'momentum', 1, 'x', 'y')")
            conn.execute("INSERT INTO paper_positions (ticker, market, name, currency, shares,"
                         " avg_price, fx_rate, cost_krw, reason, opened_at)"
                         " VALUES ('AAPL','US','Apple','USD',1.5,200.0,1300.0,390000.0,'기존','x')")
            conn.execute("INSERT INTO paper_trades (run_id, trade_date, traded_at, ticker, market,"
                         " name, side, shares, price, currency, fx_rate, amount_krw,"
                         " realized_pnl_krw, reason, ai_source)"
                         " VALUES ('r1','2026-11-26','2026-11-26T12:00:00+09:00','AAPL','US',"
                         "'Apple','BUY',1.5,200.0,'USD',1300.0,390000.0,NULL,'기존 매수','gemini')")
            conn.execute("INSERT INTO paper_equity_history (date, total_value_krw, cash_krw,"
                         " stock_value_krw, return_pct, recorded_at)"
                         " VALUES ('2026-11-26', 1000000, 700000, 300000, 0.0, 'z')")

    def test_existing_accounts_move_to_admin_user_untouched(self):
        self._legacy_db()
        admin = paper_auth.ensure_admin(self.db_path)
        self.assertEqual(admin["id"], 1)
        self.assertTrue(admin["is_admin"])
        us = paper_store.get_account(1, self.db_path)
        self.assertEqual(us["user_id"], 1)
        self.assertEqual(us["cash_krw"], 700_000)
        kr = paper_store.get_account(2, self.db_path)
        self.assertEqual(kr["market_scope"], "KR")
        self.assertEqual(kr["strategy"], "momentum")
        self.assertEqual([p["ticker"] for p in paper_store.list_positions(self.db_path, 1)],
                         ["AAPL"])
        self.assertEqual(len(paper_store.recent_trades(10, self.db_path, 1)), 1)

    def test_admin_first_pin_claims_existing_accounts(self):
        self._legacy_db()
        paper_auth.ensure_admin(self.db_path)
        user, created = paper_auth.authenticate("현우", "0420", self.db_path)
        self.assertFalse(created)
        self.assertEqual(user["id"], 1)
        self.assertTrue(user["is_admin"])
        with self.assertRaises(paper_auth.AuthError) as ctx:
            paper_auth.authenticate("현우", "9999", self.db_path)
        self.assertEqual(ctx.exception.code, "pin_mismatch")

    def test_admin_pin_env_locks_the_account_immediately(self):
        with patch.dict(os.environ, {"ADMIN_PIN": "2468"}):
            admin = paper_auth.ensure_admin(self.db_path)
        self.assertTrue(admin["has_pin"])
        user, _ = paper_auth.authenticate("현우", "2468", self.db_path)
        self.assertEqual(user["id"], 1)
        with self.assertRaises(paper_auth.AuthError):
            paper_auth.authenticate("현우", "1357", self.db_path)


# ---------------------------------------------------------------------------
# 2. 가입·PIN
# ---------------------------------------------------------------------------


class SignupTests(MultiUserCase):
    def test_new_nickname_creates_user_and_two_accounts(self):
        user, created, token = self.login("민수")
        self.assertTrue(created)
        self.assertFalse(user["is_admin"])
        self.assertTrue(token.startswith("v1."))
        accounts = paper_store.accounts_for_user(self.db_path, user["id"])
        self.assertEqual(set(accounts), {"US", "KR"})
        self.assertEqual(accounts["US"]["initial_cash_krw"], SEED)
        self.assertEqual(accounts["KR"]["initial_cash_krw"], SEED)
        self.assertNotIn(accounts["US"]["id"], (1, 2))  # 관리자 계좌를 재사용하지 않는다

    def test_existing_nickname_requires_the_same_pin(self):
        first, _, _ = self.login("민수", "1234")
        again, created = paper_auth.authenticate("민수", "1234", self.db_path)
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        with self.assertRaises(paper_auth.AuthError) as ctx:
            paper_auth.authenticate("민수", "0000", self.db_path)
        self.assertEqual(ctx.exception.code, "pin_mismatch")

    def test_nickname_is_case_insensitive(self):
        first, _, _ = self.login("Minsu")
        again, created = paper_auth.authenticate("minsu", "1234", self.db_path)
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])

    def test_pin_must_be_four_digits(self):
        for bad in ("123", "12345", "abcd", "12a4", ""):
            with self.assertRaises(paper_auth.AuthError) as ctx:
                paper_auth.authenticate("민수", bad, self.db_path)
            self.assertIn(ctx.exception.code, ("pin_invalid", "nickname_invalid"))
        self.assertIsNone(paper_auth.get_user_by_nickname("민수", self.db_path))

    def test_pin_is_stored_as_a_salted_hash(self):
        user, _, _ = self.login("민수", "1234")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT pin_hash FROM paper_users WHERE id = ?",
                               (user["id"],)).fetchone()
        stored = row[0]
        self.assertNotIn("1234", stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(paper_auth.hash_pin("1234").split("$")[2]
                        != paper_auth.hash_pin("1234").split("$")[2])  # salt는 매번 다르다

    def test_brute_force_lock_after_ten_failures(self):
        self.login("민수", "1234")
        for _ in range(paper_auth.MAX_FAILS):
            with self.assertRaises(paper_auth.AuthError):
                paper_auth.authenticate("민수", "0000", self.db_path)
        with self.assertRaises(paper_auth.AuthError) as ctx:
            paper_auth.authenticate("민수", "1234", self.db_path)
        self.assertEqual(ctx.exception.code, "too_many_attempts")


# ---------------------------------------------------------------------------
# 3. 토큰 계좌 분리
# ---------------------------------------------------------------------------


class TokenScopeTests(MultiUserCase):
    def test_token_roundtrip_and_rejects_tampering(self):
        user, _, token = self.login("민수")
        payload = paper_auth.verify_token(token, self.db_path)
        self.assertEqual(payload["user_id"], user["id"])
        self.assertIsNone(paper_auth.verify_token(token + "x", self.db_path))
        expiring, exp = paper_auth.sign_token(user, self.db_path, ttl_sec=60)
        with patch("paper_auth.time.time", return_value=exp + 1):
            self.assertIsNone(paper_auth.verify_token(expiring, self.db_path))

    def test_two_users_have_separate_accounts_and_positions(self):
        a, _, _ = self.login("민수")
        b, _, _ = self.login("서연", "5678")
        accounts_a = paper_store.accounts_for_user(self.db_path, a["id"])
        accounts_b = paper_store.accounts_for_user(self.db_path, b["id"])
        self.assertFalse({acc["id"] for acc in accounts_a.values()} &
                         {acc["id"] for acc in accounts_b.values()})
        paper_store.add_position("AAPL", "US", "USD", 1.0, 200.0, 1300.0, 260_000.0,
                                 name="Apple", account_id=accounts_a["US"]["id"],
                                 db_path=self.db_path)
        self.assertEqual(paper_store.list_positions(self.db_path, accounts_a["US"]["id"])[0]
                         ["ticker"], "AAPL")
        self.assertEqual(paper_store.list_positions(self.db_path, accounts_b["US"]["id"]), [])

    def test_status_is_scoped_per_token(self):
        a, _, _ = self.login("민수")
        paper_store.add_position("AAPL", "US", "USD", 1.0, 200.0, 1300.0, 260_000.0,
                                 name="Apple",
                                 account_id=paper_store.accounts_for_user(
                                     self.db_path, a["id"])["US"]["id"], db_path=self.db_path)
        body = paper_trading.status(db_path=self.db_path, user_id=a["id"],
                                    price_fn=lambda symbols: ({}, {}),
                                    fx_fn=lambda: {"rate": 1300.0, "source": "test"})
        self.assertEqual([p["ticker"] for p in body["positions"]], ["AAPL"])
        self.assertEqual(body["user"]["nickname"], "민수")
        self.assertIsNone(body["broker"])  # 지인 계정에는 실제 계좌 카드 정보가 없다

    def test_configure_touches_only_that_users_account(self):
        a, _, _ = self.login("민수")
        b, _, _ = self.login("서연", "5678")
        result = paper_trading.configure(account="KR", initial_cash_krw=500_000,
                                         strategy="value", user_id=a["id"], db_path=self.db_path)
        self.assertEqual(result["account"]["initial_cash_krw"], 500_000)
        b_accounts = paper_store.accounts_for_user(self.db_path, b["id"])
        self.assertEqual(b_accounts["KR"]["initial_cash_krw"], SEED)
        self.assertEqual(paper_store.get_account(2, self.db_path)["initial_cash_krw"], SEED)

    def test_run_on_friend_account_leaves_admin_untouched(self):
        import json

        a, _, _ = self.login("민수")
        accounts = paper_store.accounts_for_user(self.db_path, a["id"])
        admin_cash = paper_store.get_account(1, self.db_path)["cash_krw"]
        candidates = [{"ticker": "AAPL", "name": "Apple", "market": "US",
                       "currency": "USD", "price": 200.0, "quant_score": 90, "signals": []}]
        result = paper_trading.run(
            market="US", force=True, user_id=a["id"], db_path=self.db_path,
            ai_call=lambda prompt: json.dumps({"buys": [
                {"ticker": "AAPL", "amount_krw": 100_000, "reason": "테스트"}]}),
            candidate_fn=lambda market, criteria: candidates,
            price_fn=lambda symbols: ({sym: 200.0 for sym in symbols}, {}),
            fx_fn=lambda: {"rate": 1300.0, "source": "test"})
        self.assertEqual(len(result["buys"]), 1)
        self.assertEqual([p["ticker"] for p in
                          paper_store.list_positions(self.db_path, accounts["US"]["id"])],
                         ["AAPL"])
        self.assertEqual(paper_store.list_positions(self.db_path, 1), [])
        self.assertEqual(paper_store.get_account(1, self.db_path)["cash_krw"], admin_cash)
        self.assertEqual(len(paper_store.recent_trades(10, self.db_path, 1)), 0)


# ---------------------------------------------------------------------------
# 4. 랭킹
# ---------------------------------------------------------------------------


class LeaderboardTests(MultiUserCase):
    def test_ranking_sorted_by_combined_return_without_secrets(self):
        a, _, _ = self.login("민수")            # 관리자 계좌(user 1)는 0%
        b, _, _ = self.login("서연", "5678")
        # 민수: 미국 계좌에 수익 +10% 포지션
        accounts = paper_store.accounts_for_user(self.db_path, a["id"])
        paper_store.add_position("AAPL", "US", "USD", 1.0, 200.0, 1300.0, 200_000.0,
                                 name="Apple", account_id=accounts["US"]["id"], db_path=self.db_path)
        paper_store.update_account({"cash_krw": 300_000.0, "initial_cash_krw": 500_000.0},
                                   self.db_path, accounts["US"]["id"])
        body = paper_trading.leaderboard(
            db_path=self.db_path,
            price_fn=lambda symbols: ({sym: 260.0 for sym in symbols}, {}),
            fx_fn=lambda: {"rate": 1300.0, "source": "test"})
        rows = body["rows"]
        self.assertEqual(rows[0]["nickname"], "민수")
        self.assertEqual(rows[0]["rank"], 1)
        self.assertGreater(rows[0]["total_return_pct"], 0)
        self.assertTrue(all("pin" not in key.lower() and "token" not in key.lower()
                            for row in rows for key in row))
        self.assertTrue(all("pin_hash" not in row for row in rows))
        # 관리자(현우)도 0%로 들어야 한다 — 가입 전에도 계좌가 있다
        admin_rows = [row for row in rows if row["is_admin"]]
        self.assertEqual(len(admin_rows), 1)
        self.assertEqual(admin_rows[0]["nickname"], "현우")
        self.assertEqual(admin_rows[0]["total_value_krw"], SEED * 2)

    def test_leaderboard_has_per_market_returns(self):
        a, _, _ = self.login("민수")
        body = paper_trading.leaderboard(db_path=self.db_path,
                                         price_fn=lambda symbols: ({}, {}),
                                         fx_fn=lambda: {"rate": 1300.0, "source": "test"})
        row = next(row for row in body["rows"] if row["user_id"] == a["id"])
        self.assertIn("us", row)
        self.assertIn("kr", row)
        self.assertEqual(row["us"]["total_value_krw"], SEED)
        self.assertEqual(row["kr"]["total_value_krw"], SEED)


# ---------------------------------------------------------------------------
# 5. FCM 유저 분리·자동 실행
# ---------------------------------------------------------------------------


class PushAndAutoTests(MultiUserCase):
    def test_fcm_tokens_are_scoped_by_user(self):
        a, _, _ = self.login("민수")
        paper_store.register_fcm_token("device-a", user_id=a["id"], db_path=self.db_path)
        paper_store.register_fcm_token("device-admin", db_path=self.db_path)
        self.assertEqual([row["token"] for row in
                          paper_store.list_fcm_tokens(self.db_path, a["id"])], ["device-a"])
        self.assertEqual(paper_store.count_fcm_tokens(self.db_path), 2)
        self.assertEqual(paper_store.count_fcm_tokens(self.db_path, a["id"]), 1)

    def test_auto_due_accounts_includes_other_users(self):
        a, _, _ = self.login("민수")
        due = paper_trading.auto_due_accounts(
            now=datetime(2026, 11, 28, 16, 0, tzinfo=paper_trading.KST))
        uid_markets = {(uid, market) for uid, market in due}
        self.assertIn((a["id"], "KR"), uid_markets)
        self.assertIn((a["id"], "US"), uid_markets)
        self.assertNotIn((paper_store.DEFAULT_USER_ID, "KR"), uid_markets)

    def test_auto_tick_runs_every_due_user_and_collects_candidates_once(self):
        self.login("민수")
        self.login("서연", "5678")
        calls = []
        candidate_calls = []
        collected = []

        def fake_run(market=None, force=False, trigger="manual", user_id=None,
                     candidate_fn=None, **kwargs):
            calls.append((user_id, market))
            if candidate_fn:
                candidate_calls.append(candidate_fn(market, "균형 잡힌 종목"))
            return {"ok": True, "summary": {}, "buys": [], "sells": []}

        def fake_candidates(market, criteria):
            collected.append((market, criteria))
            return ["cand"]

        with patch.object(paper_trading, "auto_due_markets", return_value=[]), \
                patch.object(paper_trading, "auto_due_accounts",
                             return_value=[(2, "KR"), (3, "KR")]), \
                patch.object(paper_trading, "run", side_effect=fake_run), \
                patch.object(paper_trading, "default_candidates",
                             side_effect=fake_candidates):
            paper_trading._auto_tick()
        self.assertEqual(calls, [(2, "KR"), (3, "KR")])
        self.assertEqual(len(candidate_calls), 2)
        # 같은 (시장, 전략) 후보는 메모 함수가 한 번만 수집한다
        self.assertEqual(len(collected), 1)
        self.assertTrue(all(item == ["cand"] for item in candidate_calls))


# ---------------------------------------------------------------------------
# 6. HTTP 표면
# ---------------------------------------------------------------------------


@unittest.skipIf(TestClient is None, "httpx 미설치")
class ApiSurfaceTests(MultiUserCase):
    def setUp(self):
        super().setUp()
        import api
        self.api = api
        self.client = TestClient(api.app)

    def auth(self, nickname, pin="1234"):
        response = self.client.post("/api/paper/auth", json={"nickname": nickname, "pin": pin})
        return response

    def test_auth_endpoint_returns_token_and_user(self):
        response = self.auth("민수")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["created"])
        self.assertEqual(body["user"]["nickname"], "민수")
        self.assertFalse(body["user"]["is_admin"])
        self.assertNotIn("pin", body)
        self.assertNotIn("pin_hash", body["user"])

    def test_wrong_pin_is_401(self):
        self.auth("민수", "1234")
        response = self.auth("민수", "0000")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "pin_mismatch")

    def test_invalid_pin_format_is_400(self):
        self.assertEqual(self.auth("민수", "12").status_code, 400)

    def test_status_requires_valid_token(self):
        response = self.client.get("/api/paper/status",
                                   headers={"X-Paper-User-Token": "v1.bad.sig"})
        self.assertEqual(response.status_code, 401)

    def test_status_with_token_shows_only_that_users_account(self):
        token = self.auth("민수").json()["token"]
        response = self.client.get("/api/paper/status",
                                   headers={"X-Paper-User-Token": token})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user"]["nickname"], "민수")
        self.assertEqual(body["account"]["initial_cash_krw"], SEED)
        self.assertNotEqual(body["account"]["id"], 1)
        self.assertIsNone(body["broker"])

    def test_admin_token_sees_broker_card(self):
        paper_auth.ensure_admin(self.db_path)
        user, _ = paper_auth.authenticate("현우", "1234", self.db_path)
        token, _ = paper_auth.sign_token(user, self.db_path)
        body = self.client.get("/api/paper/status",
                               headers={"X-Paper-User-Token": token}).json()
        self.assertIsNotNone(body["broker"])
        self.assertTrue(body["user"]["is_admin"])

    def test_broker_endpoints_are_admin_only(self):
        token = self.auth("민수").json()["token"]
        headers = {"X-Paper-User-Token": token}
        self.assertEqual(self.client.get("/api/broker/status", headers=headers).status_code, 403)
        response = self.client.post("/api/broker/credentials",
                                    headers=headers,
                                    json={"adapter": "kis", "app_key": "k", "app_secret": "s",
                                          "account_no": "1", "confirm_risk": True})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.delete("/api/broker/credentials/kis",
                                            headers=headers).status_code, 403)
        # 토큰이 없는 구 클라이언트는 예전처럼 허용(하위 호환)
        self.assertEqual(self.client.get("/api/broker/status").status_code, 200)

    def test_leaderboard_endpoint(self):
        self.auth("민수")
        response = self.client.get("/api/paper/leaderboard")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["count"], 2)
        self.assertTrue(all("rank" in row for row in body["rows"]))

    def test_fcm_register_is_per_user(self):
        token = self.auth("민수").json()["token"]
        headers = {"X-Paper-User-Token": token}
        response = self.client.post("/api/paper/fcm/register", headers=headers,
                                    json={"token": "device-min"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tokens"], 1)
        admin = self.client.get("/api/paper/fcm/status").json()
        self.assertEqual(admin["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
