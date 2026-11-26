"""AI 모의투자 체결 FCM 푸시 알림 테스트.

다루는 것
1. 토큰 저장소: 등록·갱신(중복 없음)·삭제·목록·개수, 기존 계좌/체결은 그대로
2. 알림 문구: 단건 매수·매도(순손익·수익률), 다건 요약, data payload(screen=paper)
3. 체결 연동: 실제 체결이 있을 때만 발송기가 불리고, 없으면 불리지 않는다
4. 미설정 안전 동작: 키가 없으면 서버는 살아 있고 발송은 skipped
5. 만료 토큰 정리: UnregisteredError 토큰을 DB에서 지운다
6. HTTP 표면: POST/DELETE /api/paper/fcm/register, GET /status, POST /test

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fcm  # noqa: E402
import paper_store  # noqa: E402
import paper_trading  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


def us(ticker, price=100.0, score=80.0, name=""):
    return {"ticker": ticker, "name": name or ticker, "currency": "USD", "price": price,
            "quant_score": score, "signals": ["정배열일치"], "rsi14": 55.0, "trend": "정배열",
            "price_source": "finnhub_quote"}


def kr(ticker, price=70000.0, score=80.0, name=""):
    return {"ticker": ticker, "name": name or ticker, "currency": "KRW", "price": price,
            "quant_score": score, "signals": ["정배열일치"], "rsi14": 55.0, "trend": "정배열",
            "price_source": "naver_realtime"}


def buy_trade(ticker="086280.KS", name="현대글로비스", shares=1.0, amount=201_470.0,
              fee=30.0, market="KR"):
    return {"ticker": ticker, "name": name, "side": "BUY", "market": market, "shares": shares,
            "amount_krw": amount, "fee_krw": fee, "realized_pnl_krw": None}


def sell_trade(ticker="271560.KS", name="오리온", shares=3.0, amount=305_000.0, fee=595.0,
               realized=12_300.0, market="KR"):
    return {"ticker": ticker, "name": name, "side": "SELL", "market": market, "shares": shares,
            "amount_krw": amount, "fee_krw": fee, "realized_pnl_krw": realized}


class _FakeResponse:
    """`messaging.send_each_for_multicast` 응답 흉내 — 토큰별 예외만 있으면 된다."""

    def __init__(self, exceptions):
        self.responses = [type("R", (), {"exception": exc})() for exc in exceptions]
        self.success_count = sum(1 for exc in exceptions if exc is None)
        self.failure_count = len(exceptions) - self.success_count


class FcmStoreCase(unittest.TestCase):
    """테스트마다 빈 SQLite 파일 + FCM 키 없는 상태(발송은 건너뛴다)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "paper.db")
        env = {"HISTORY_DB_PATH": self.db_path, "PAPER_AUTO": "0"}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (fcm.ENV_JSON, fcm.ENV_PATH):
            os.environ.pop(name, None)
        paper_trading.clear_caches()
        fcm.reset_init()
        self.addCleanup(fcm.reset_init)

    # --- 토큰 저장소 ---

    def test_register_stores_token_once_and_updates_timestamp(self):
        first = paper_store.register_fcm_token("token-a", "android")
        again = paper_store.register_fcm_token("token-a", "android")
        self.assertEqual(paper_store.count_fcm_tokens(), 1)
        self.assertEqual(paper_store.list_fcm_tokens()[0]["token"], "token-a")
        self.assertTrue(first["updated_at"])
        self.assertTrue(again["updated_at"])

    def test_register_rejects_blank_token(self):
        with self.assertRaises(ValueError):
            paper_store.register_fcm_token("   ")

    def test_remove_token_and_remove_many(self):
        paper_store.register_fcm_token("token-a")
        paper_store.register_fcm_token("token-b")
        self.assertEqual(paper_store.remove_fcm_token("token-a"), 1)
        self.assertEqual(paper_store.remove_fcm_token("token-a"), 0)
        self.assertEqual(paper_store.count_fcm_tokens(), 1)
        self.assertEqual(paper_store.remove_fcm_tokens(["token-b", "token-b"]), 1)
        self.assertEqual(paper_store.count_fcm_tokens(), 0)

    def test_registering_tokens_keeps_account_positions_and_trades(self):
        account = paper_store.get_account()
        paper_store.register_fcm_token("token-a")
        self.assertEqual(paper_store.get_account()["cash_krw"], account["cash_krw"])
        self.assertEqual(paper_store.get_account()["initial_cash_krw"],
                         account["initial_cash_krw"])
        self.assertEqual(paper_store.list_positions(), [])
        self.assertEqual(paper_store.recent_trades(), [])

    # --- 알림 문구 ---

    def test_single_buy_message(self):
        message = fcm.build_fill_message([], [buy_trade()])
        self.assertEqual(message["title"], "🤖 [한국] AI 매수 체결 · 현대글로비스")
        self.assertEqual(message["body"], "한국 계좌 · 1주 · ₩201,470 (수수료 ₩30)")
        self.assertEqual(message["data"]["screen"], "paper")
        self.assertEqual(message["data"]["market"], "KR")

    def test_us_fill_message_is_marked_as_the_us_account(self):
        message = fcm.build_fill_message([], [buy_trade("AAPL", "애플", 3.6338, 998_502.0, 1_498.0,
                                                        market="US")])
        self.assertEqual(message["title"], "🤖 [미국] AI 매수 체결 · 애플")
        self.assertTrue(message["body"].startswith("미국 계좌 · "))
        self.assertEqual(message["data"]["market"], "US")

    def test_single_sell_message_shows_net_pnl_with_percent(self):
        message = fcm.build_fill_message([sell_trade()], [])
        self.assertEqual(message["title"], "🤖 [한국] AI 매도 체결 · 오리온")
        # 순수취 304,405 - 순손익 12,300 = 원가 292,105 → +4.2%
        self.assertEqual(message["body"], "한국 계좌 · 순손익 +₩12,300 (+4.2%)")

    def test_single_sell_message_without_realized_pnl(self):
        trade = sell_trade(realized=None)
        message = fcm.build_fill_message([trade], [])
        self.assertEqual(message["body"], "한국 계좌 · 3주 · 순수취 ₩304,405")

    def test_multiple_buys_message(self):
        rows = [buy_trade(), buy_trade("005930.KS", "삼성전자", 2.0, 400_000.0, 60.0),
                buy_trade("000660.KS", "SK하이닉스", 1.0, 80_000.0, 12.0)]
        message = fcm.build_fill_message([], rows)
        self.assertEqual(message["title"], "🤖 [한국] AI 모의투자 3건 체결")
        self.assertEqual(message["body"], "한국 계좌 · 현대글로비스 외 2건 매수 · 총 ₩681,572")
        self.assertEqual(message["data"]["count"], "3")

    def test_mixed_message_separates_buy_and_sell_totals(self):
        message = fcm.build_fill_message([sell_trade(fee=0.0)], [buy_trade(fee=0.0)])
        self.assertEqual(message["title"], "🤖 [한국] AI 모의투자 2건 체결")
        self.assertIn("매수 ₩201,470", message["body"])
        self.assertIn("매도 ₩305,000", message["body"])

    def test_message_uses_ticker_when_name_missing(self):
        message = fcm.build_fill_message([], [buy_trade(name="")])
        self.assertIn("086280.KS", message["title"])

    def test_no_trades_makes_no_message(self):
        self.assertEqual(fcm.build_fill_message([], []), {})
        self.assertEqual(fcm.build_fill_message(None, None), {})

    # --- 미설정 안전 동작 ---

    def test_unconfigured_server_skips_push_without_error(self):
        paper_store.register_fcm_token("token-a")
        self.assertFalse(fcm.is_configured())
        result = fcm.notify_fills([], [buy_trade()])
        self.assertFalse(result["configured"])
        self.assertEqual(result["sent"], 0)
        self.assertIn("reason", result)
        # 토큰은 남아 있다 — 설정을 넣으면 다음 체결부터 바로 발송된다.
        self.assertEqual(paper_store.count_fcm_tokens(), 1)

    def test_status_hides_credentials(self):
        paper_store.register_fcm_token("token-a")
        status = fcm.status()
        self.assertFalse(status["configured"])
        self.assertEqual(status["tokens"], 1)
        self.assertEqual(status["channel_id"], fcm.CHANNEL_ID)
        self.assertNotIn("private_key", json.dumps(status))

    def test_env_json_accepts_base64_and_broken_json_is_reported(self):
        payload = {"project_id": "p", "private_key": "-----BEGIN-----\\nKEY", "client_email": "a@b"}
        encoded = __import__("base64").b64encode(json.dumps(payload).encode()).decode()
        self.assertEqual(fcm._decode_service_account(encoded)["project_id"], "p")
        # 한 번 더 이스케이프된 줄바꿈(`\\n`)도 실행 가능한 키로 교정한다.
        self.assertIn("\n", fcm._decode_service_account(json.dumps(payload))["private_key"])
        self.assertIsNone(fcm._decode_service_account("not-json"))
        with patch.dict(os.environ, {fcm.ENV_JSON: "not-json"}):
            self.assertFalse(fcm.is_configured())
            self.assertIn("invalid", fcm.status()["reason"])

    # --- 체결 연동 ---

    def _run(self, candidates, ai_payload=None, push_fn=None, force=True, prices=None):
        mapping = {"AAPL": 100.0, "MSFT": 200.0}
        mapping.update(prices or {})

        def price_fn(tickers):
            found = {sym: mapping[sym] for sym in tickers if sym in mapping}
            return found, {sym: "test" for sym in found}

        return paper_trading.run(
            market="US", force=force, db_path=self.db_path,
            ai_call=(lambda prompt: json.dumps(ai_payload)) if ai_payload else None,
            price_fn=price_fn,
            candidate_fn=lambda market, criteria, top_n=8: list(candidates),
            fx_fn=lambda: {"rate": 1300.0, "source": "test"},
            push_fn=push_fn,
        )

    def test_trades_trigger_one_push_with_sells_and_buys(self):
        calls = []
        result = self._run(
            [us("AAPL", 100.0), us("MSFT", 200.0)],
            ai_payload={"buys": [{"ticker": "AAPL", "amount_krw": 300_000,
                                  "reason": "정배열 상승"}], "sells": []},
            push_fn=lambda sells, buys, db: calls.append((sells, buys)) or {"sent": 1},
        )
        self.assertEqual(len(result["buys"]), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1]), 1)
        self.assertEqual(result["push"], {"sent": 1})

    def test_sells_are_included_in_push(self):
        self._run([us("AAPL", 100.0)],
                  ai_payload={"buys": [{"ticker": "AAPL", "amount_krw": 300_000,
                                        "reason": "매수"}], "sells": []})
        calls = []
        # 다음 실행에서 주가가 두 배 → 익절 규칙(+25%)이 걸려 매도 1건이 체결된다.
        result = self._run([us("AAPL", 200.0)], ai_payload=None, prices={"AAPL": 200.0},
                           push_fn=lambda sells, buys, db: calls.append((sells, buys)) or {"sent": 1})
        self.assertEqual(result.get("buys"), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][0]), 1)
        self.assertEqual(calls[0][0][0]["side"], "SELL")

    def test_no_fill_means_no_push(self):
        calls = []
        result = self._run([], ai_payload=None,
                           push_fn=lambda sells, buys, db: calls.append((sells, buys)) or {"sent": 1})
        self.assertEqual(calls, [])
        self.assertIsNone(result["push"])

    def test_push_failure_does_not_break_the_run(self):
        def boom(sells, buys, db):
            raise RuntimeError("fcm down")

        result = self._run([us("AAPL", 100.0)],
                           ai_payload={"buys": [{"ticker": "AAPL", "amount_krw": 300_000,
                                                 "reason": "매수"}], "sells": []},
                           push_fn=boom)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["buys"]), 1)
        self.assertEqual(result["push"]["error"], "push_failed")

    def test_default_push_uses_registered_tokens(self):
        """push_fn을 주지 않으면 실제 fcm 경로 — 키가 없으므로 skipped로 조용히 끝난다."""
        paper_store.register_fcm_token("token-a")
        result = self._run([us("AAPL", 100.0)],
                           ai_payload={"buys": [{"ticker": "AAPL", "amount_krw": 300_000,
                                                 "reason": "매수"}], "sells": []})
        self.assertTrue(result["ok"])
        self.assertFalse(result["push"]["configured"])
        self.assertEqual(result["push"]["sent"], 0)

    # --- 만료 토큰 정리 ---

    def test_dead_tokens_are_deleted_after_send(self):
        paper_store.register_fcm_token("alive-token")
        paper_store.register_fcm_token("dead-token")

        with patch.object(fcm, "_send_multicast", return_value=fcm.SendOutcome(
                success=1, failure=1, dead_tokens=["dead-token"])) as sender, \
                patch.object(fcm, "is_configured", return_value=True):
            result = fcm.notify_fills([], [buy_trade()])
        sender.assert_called_once()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual([row["token"] for row in paper_store.list_fcm_tokens()], ["alive-token"])

    def test_send_outcome_marks_only_dead_tokens(self):
        dead = type("UnregisteredError", (Exception,), {})
        response = _FakeResponse([None, dead("Requested entity was not found.")])
        with patch("firebase_admin.messaging.send_each_for_multicast", return_value=response), \
                patch.object(fcm, "_get_app", return_value=object()):
            outcome = fcm._send_multicast(["a", "b"], "t", "b", {})
        self.assertEqual(outcome.success, 1)
        self.assertEqual(outcome.failure, 1)
        self.assertEqual(outcome.dead_tokens, ["b"])

    def test_dead_token_detection(self):
        unregistered = type("UnregisteredError", (Exception,), {})
        self.assertTrue(fcm._is_dead_token(unregistered("not-registered")))
        self.assertTrue(fcm._is_dead_token(Exception("Requested entity was not found.")))
        self.assertFalse(fcm._is_dead_token(Exception("Quota exceeded")))


@unittest.skipIf(TestClient is None, "fastapi.testclient 없음")
class FcmEndpointCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "paper.db")
        env = {"HISTORY_DB_PATH": self.db_path, "PAPER_AUTO": "0", "GEMINI_API_KEY": "test"}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (fcm.ENV_JSON, fcm.ENV_PATH):
            os.environ.pop(name, None)
        fcm.reset_init()
        self.addCleanup(fcm.reset_init)
        import api
        self.client = TestClient(api.app)

    def test_register_and_unregister_endpoints(self):
        response = self.client.post("/api/paper/fcm/register",
                                    json={"token": "device-token", "platform": "android"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["registered"])
        self.assertEqual(response.json()["tokens"], 1)

        removed = self.client.delete("/api/paper/fcm/register?token=device-token")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["removed"], 1)
        self.assertEqual(removed.json()["tokens"], 0)

    def test_register_with_enabled_false_deletes_token(self):
        self.client.post("/api/paper/fcm/register", json={"token": "device-token"})
        response = self.client.post("/api/paper/fcm/register",
                                    json={"token": "device-token", "enabled": False})
        self.assertFalse(response.json()["registered"])
        self.assertEqual(response.json()["tokens"], 0)

    def test_register_rejects_empty_token(self):
        response = self.client.post("/api/paper/fcm/register", json={"token": "   "})
        self.assertEqual(response.status_code, 400)

    def test_status_endpoint_reports_unconfigured(self):
        response = self.client.get("/api/paper/fcm/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["configured"])
        self.assertEqual(body["tokens"], 0)

    def test_test_endpoint_without_config_or_tokens_is_safe(self):
        response = self.client.post("/api/paper/fcm/test")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["configured"])
        self.assertEqual(body["sent"], 0)

    def test_health_reports_fcm(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("fcm", response.json())


if __name__ == "__main__":
    unittest.main()
