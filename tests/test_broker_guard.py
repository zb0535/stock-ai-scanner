"""브로커 추상화·실주문 안전장치 테스트.

다루는 것
1. 어댑터 선택: `BROKER_MODE` 기본값은 paper, 모르는 값은 paper로 내려간다
2. 자리 어댑터(한투·토스·미래에셋): 키가 없으면 `broker_not_configured`이고 어떤 주문도 안 나간다
3. 모의투자 호환: `PaperBroker`가 `paper_trading` 체결 엔진과 **같은 금액·수수료**로 체결한다
4. 킬 스위치: `REAL_TRADING_ENABLED=false`면 실주문이 차단된다
5. 1회/1일 한도: `REAL_ORDER_MAX_KRW`·`REAL_DAILY_MAX_KRW`를 넘으면 차단된다
6. 중복 주문: 같은 (run_id, 시장, 방향, 종목)은 한 번만 나간다
7. 드라이런: 주문을 보내지 않고 "막혔을 사유"만 돌려준다
8. 재기동: 1일 누적·중복 키가 JSON 상태 파일에서 복원된다
9. 실계좌 모드에서 모의투자 실행기는 실주문을 만들지 않는다(가짜 잔고로 실주문 방지)
10. `GET /`가 `broker` 요약을 노출하고 기존 필드는 그대로다

실행: python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import broker  # noqa: E402
import paper_store  # noqa: E402
import paper_trading  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

SEED = 1_000_000.0


class FakeLiveBroker(broker.BrokerAdapter):
    """실계좌 어댑터의 테스트 대역 — 실제 주문 대신 받은 주문을 기록한다."""

    name = "fake"
    label = "테스트 실계좌"
    is_live = True
    api_key_envs = ("FAKE_BROKER_KEY",)

    placed: list = []

    def __init__(self):
        FakeLiveBroker.placed = []

    def place_buy(self, order):
        FakeLiveBroker.placed.append(order)
        return broker.OrderResult(ok=True, status=broker.STATUS_FILLED, adapter=self.name,
                                  live=True, order_id="fake-buy")

    def place_sell(self, order):
        FakeLiveBroker.placed.append(order)
        return broker.OrderResult(ok=True, status=broker.STATUS_FILLED, adapter=self.name,
                                  live=True, order_id="fake-sell")

    def get_balance(self, db_path=None, account_id=None):
        return broker.BrokerAccountInfo(adapter=self.name, live=True, ok=True,
                                        cash_krw=SEED, positions=[])


class BrokerCase(unittest.TestCase):
    """테스트마다 빈 SQLite 파일 + 빈 가드 상태 파일 + 모의투자 기본 모드."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "paper.db")
        self.guard_state = os.path.join(self._tmp.name, "broker_guard.json")
        patcher = patch.dict(os.environ, {
            "HISTORY_DB_PATH": self.db_path,
            "PAPER_AUTO": "0",
            "BROKER_MODE": "",
            "BROKER_GUARD_STATE": self.guard_state,
            "REAL_TRADING_ENABLED": "0",
            "REAL_DRY_RUN": "1",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeLiveBroker.placed = []
        paper_trading.clear_caches()

    # --- 도우미 ---
    def order(self, **overrides) -> broker.OrderRequest:
        base = dict(side="BUY", ticker="AAPL", market="US", price=100.0, currency="USD",
                    amount_krw=50_000.0, fx_rate=1300.0, run_id="run1", name="애플",
                    db_path=self.db_path, account_id=paper_store.ACCOUNT_US)
        base.update(overrides)
        return broker.OrderRequest(**base)

    def guard(self) -> broker.OrderGuard:
        return broker.OrderGuard(broker.OrderLedger(self.guard_state))

    def wrapped(self, adapter=None, guard=None) -> broker.GuardedBroker:
        return broker.GuardedBroker(adapter or FakeLiveBroker(), guard or self.guard())

    def live_env(self, **extra):
        env = {"REAL_TRADING_ENABLED": "1", "REAL_DRY_RUN": "0", "FAKE_BROKER_KEY": "test-key"}
        env.update(extra)
        return patch.dict(os.environ, env)

    def candidates(self, rows_by_market):
        def _fn(market, criteria, top_n=paper_trading.CANDIDATES_PER_MARKET):
            return list(rows_by_market.get(market) or [])
        return _fn

    def prices(self, mapping):
        def _fn(tickers):
            prices = {sym: mapping[sym] for sym in tickers if sym in mapping}
            return prices, {sym: "test" for sym in prices}
        return _fn

    def fx(self, rate=1300.0):
        return lambda: {"rate": rate, "source": "test"}

    def us(self, ticker, price=100.0, score=80.0, name=""):
        return {"ticker": ticker, "name": name or ticker, "currency": "USD", "price": price,
                "quant_score": score, "signals": ["정배열일치"], "rsi14": 55.0,
                "trend": "정배열", "price_source": "test"}


# ---------------------------------------------------------------------------


class ModeSelectionTests(BrokerCase):
    def test_default_mode_is_paper(self):
        self.assertEqual(broker.broker_mode(), "paper")
        self.assertEqual(broker.requested_mode(), "paper")

    def test_unknown_mode_falls_back_to_paper(self):
        with patch.dict(os.environ, {"BROKER_MODE": "upbit"}):
            self.assertEqual(broker.requested_mode(), "upbit")
            self.assertEqual(broker.broker_mode(), "paper")

    def test_paper_adapter_is_not_live_and_is_configured(self):
        adapter = broker.get_adapter(market="US", db_path=self.db_path)
        self.assertEqual(adapter.name, "paper")
        self.assertFalse(adapter.is_live)
        self.assertTrue(adapter.configured)
        self.assertEqual(adapter.health().status, "ok")

    def test_live_stubs_report_missing_keys(self):
        for mode, expected in (("kis", "KIS_APP_KEY"), ("toss", "TOSS_CLIENT_ID"),
                               ("mirae", "MIRAE_API_KEY")):
            with patch.dict(os.environ, {"BROKER_MODE": mode}):
                broker.clear_cache()
                adapter = broker.get_adapter(market="US", db_path=self.db_path)
                health = adapter.health()
            self.assertTrue(adapter.is_live, mode)
            self.assertFalse(adapter.configured, mode)
            self.assertEqual(health.status, broker.STATUS_NOT_CONFIGURED, mode)
            self.assertIn(expected, health.missing_env, mode)
            self.assertFalse(health.implemented, mode)

    def test_summary_lists_every_adapter(self):
        summary = broker.broker_summary()
        self.assertEqual(summary["mode"], "paper")
        self.assertFalse(summary["live"])
        self.assertFalse(summary["real_trading_enabled"])
        self.assertEqual(summary["supported_adapters"], ["paper", "kis", "toss", "mirae"])
        names = [row["name"] for row in summary["adapters"]]
        self.assertEqual(names, ["paper", "kis", "toss", "mirae"])
        self.assertIn("guard", summary)
        self.assertEqual(summary["guard"]["order_max_krw"], broker.DEFAULT_ORDER_MAX_KRW)

    def test_summary_never_raises_on_broken_env(self):
        with patch.dict(os.environ, {"BROKER_MODE": "kis", "REAL_ORDER_MAX_KRW": "무한"}):
            summary = broker.broker_summary()
        self.assertEqual(summary["mode"], "kis")
        self.assertTrue(summary["live"])
        self.assertFalse(summary["configured"])
        self.assertEqual(summary["guard"]["order_max_krw"], broker.DEFAULT_ORDER_MAX_KRW)


class PaperCompatTests(BrokerCase):
    """모의투자 경로가 어댑터를 거쳐도 예전 체결 엔진과 같은 금액을 낸다."""

    def test_buy_through_adapter_matches_fill_engine(self):
        paper_store.init_store(self.db_path, paper_store.ACCOUNT_US)
        order = self.order(amount_krw=300_000.0, reason="테스트", run_id="cmp-buy")
        via_adapter = broker.PaperBroker(market="US", db_path=self.db_path).place_buy(order)
        self.assertTrue(via_adapter.ok)
        self.assertEqual(via_adapter.status, broker.STATUS_FILLED)
        self.assertIsNotNone(via_adapter.trade)
        self.assertTrue(str(via_adapter.order_id).startswith("paper-"))

        direct_path = os.path.join(self._tmp.name, "direct.db")
        paper_store.init_store(direct_path, paper_store.ACCOUNT_US)
        direct = paper_trading._fill_buy("AAPL", "US", "애플", "USD", 100.0, 300_000.0, 1300.0,
                                         "테스트", "cmp-buy", "broker", direct_path,
                                         paper_store.ACCOUNT_US)
        for key in ("ticker", "side", "shares", "price", "currency", "fx_rate",
                    "amount_krw", "fee_krw", "settle_krw"):
            self.assertEqual(via_adapter.trade[key], direct[key], key)

    def test_sell_through_adapter_uses_the_same_net_amount(self):
        paper_store.init_store(self.db_path, paper_store.ACCOUNT_US)
        paper_trading._fill_buy("AAPL", "US", "애플", "USD", 100.0, 300_000.0, 1300.0,
                                "매수", "buy-1", "quant", self.db_path, paper_store.ACCOUNT_US)
        position = paper_store.list_positions(self.db_path, paper_store.ACCOUNT_US)[0]
        result = paper_trading._execute_sell(position, 90.0, 1300.0, 1.0, "손절", "sell-1",
                                            "rule", self.db_path, paper_store.ACCOUNT_US)
        self.assertTrue(result.ok)
        trade = result.trade
        self.assertEqual(trade["ticker"], "AAPL")
        self.assertAlmostEqual(trade["amount_krw"],
                               round(trade["shares"] * 90.0 * 1300.0, 2), places=2)
        self.assertAlmostEqual(trade["settle_krw"],
                               round(trade["amount_krw"] - trade["fee_krw"], 2), places=2)
        self.assertAlmostEqual(trade["realized_pnl_krw"],
                               round(trade["settle_krw"] - round(
                                   position["cost_krw"] * (trade["shares"] / position["shares"]), 2),
                                     2), places=2)
        # 전량 매도 뒤 남는 조각(≤0.0001주)은 소수점 4자리 내림의 기존 동작이다(값 손실이 아니라
        # 2.3042×10000 = 23041.999…처럼 실수 표기 때문에 생긴다) — "사실상 전량"만 확인한다.
        remaining = paper_store.list_positions(self.db_path, paper_store.ACCOUNT_US)
        self.assertLessEqual(sum(float(row["shares"]) for row in remaining), 0.0001)

    def test_paper_run_places_orders_even_with_the_kill_switch_off(self):
        result = paper_trading.run(market="US", force=True,
                                   candidate_fn=self.candidates({"US": [self.us("AAPL")]}),
                                   price_fn=self.prices({"AAPL": 100.0}),
                                   fx_fn=self.fx())
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["buys"]), 1)
        self.assertGreater(result["buys"][0]["fee_krw"], 0)
        self.assertLess(result["summary"]["cash_krw"], SEED)

    def test_paper_order_key_is_per_run(self):
        first = paper_trading.run(market="US", force=True,
                                  candidate_fn=self.candidates({"US": [self.us("AAPL")]}),
                                  price_fn=self.prices({"AAPL": 100.0}), fx_fn=self.fx())
        second = paper_trading.run(market="US", force=True,
                                   candidate_fn=self.candidates({"US": [self.us("MSFT")]}),
                                   price_fn=self.prices({"MSFT": 100.0}), fx_fn=self.fx())
        self.assertEqual(len(first["buys"]), 1)
        self.assertEqual(len(second["buys"]), 1)


class GuardTests(BrokerCase):
    def test_kill_switch_blocks_live_order(self):
        with patch.dict(os.environ, {"REAL_DRY_RUN": "0", "FAKE_BROKER_KEY": "test-key"}):
            result = self.wrapped().place_buy(self.order())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, broker.STATUS_BLOCKED)
        self.assertEqual(result.reason, broker.REASON_KILL_SWITCH)
        self.assertEqual(FakeLiveBroker.placed, [])

    def test_unconfigured_adapter_never_places(self):
        with patch.dict(os.environ, {"REAL_TRADING_ENABLED": "1", "REAL_DRY_RUN": "0"}):
            result = self.wrapped().place_buy(self.order())
        self.assertEqual(result.status, broker.STATUS_NOT_CONFIGURED)
        self.assertEqual(result.reason, broker.REASON_NOT_CONFIGURED)
        self.assertEqual(FakeLiveBroker.placed, [])

    def test_single_order_cap(self):
        with self.live_env(REAL_ORDER_MAX_KRW="100000"):
            result = self.wrapped().place_buy(self.order(amount_krw=200_000.0))
        self.assertEqual(result.status, broker.STATUS_BLOCKED)
        self.assertEqual(result.reason, broker.REASON_ORDER_LIMIT)
        self.assertEqual(result.guard["notional_krw"], 200_000.0)
        self.assertEqual(FakeLiveBroker.placed, [])

    def test_single_order_under_cap_is_placed(self):
        with self.live_env(REAL_ORDER_MAX_KRW="100000"):
            result = self.wrapped().place_buy(self.order(amount_krw=90_000.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, broker.STATUS_FILLED)
        self.assertEqual(len(FakeLiveBroker.placed), 1)

    def test_daily_cap_blocks_the_next_order(self):
        with self.live_env(REAL_DAILY_MAX_KRW="100000"):
            wrapper = self.wrapped()
            first = wrapper.place_buy(self.order(amount_krw=60_000.0, run_id="day-a"))
            second = wrapper.place_buy(self.order(amount_krw=60_000.0, run_id="day-b"))
        self.assertEqual(first.status, broker.STATUS_FILLED)
        self.assertEqual(second.status, broker.STATUS_BLOCKED)
        self.assertEqual(second.reason, broker.REASON_DAILY_LIMIT)
        self.assertAlmostEqual(wrapper.guard.ledger.daily_used(), 60_000.0, places=2)
        self.assertEqual(len(FakeLiveBroker.placed), 1)

    def test_duplicate_order_is_blocked(self):
        with self.live_env():
            wrapper = self.wrapped()
            first = wrapper.place_buy(self.order(run_id="dup"))
            second = wrapper.place_buy(self.order(run_id="dup"))
            other_side = wrapper.place_sell(self.order(run_id="dup", side="SELL", shares=1.0,
                                                       amount_krw=0.0))
        self.assertEqual(first.status, broker.STATUS_FILLED)
        self.assertEqual(second.status, broker.STATUS_BLOCKED)
        self.assertEqual(second.reason, broker.REASON_DUPLICATE)
        self.assertEqual(other_side.status, broker.STATUS_FILLED)  # 방향이 다르면 다른 주문
        self.assertEqual(len(FakeLiveBroker.placed), 2)

    def test_dry_run_never_places_and_keeps_the_counter(self):
        with self.live_env(REAL_DRY_RUN="1"):
            wrapper = self.wrapped()
            first = wrapper.place_buy(self.order(run_id="dry-a"))
            second = wrapper.place_buy(self.order(run_id="dry-b"))
        self.assertEqual(first.status, broker.STATUS_DRY_RUN)
        self.assertTrue(first.ok)
        self.assertEqual(first.guard["blocked_by"], "")
        self.assertEqual(second.status, broker.STATUS_DRY_RUN)
        self.assertEqual(FakeLiveBroker.placed, [])
        self.assertAlmostEqual(wrapper.guard.ledger.daily_used(), 0.0, places=2)

    def test_dry_run_reports_would_be_blocked(self):
        with self.live_env(REAL_DRY_RUN="1", REAL_ORDER_MAX_KRW="100000"):
            result = self.wrapped().place_buy(self.order(amount_krw=500_000.0))
        self.assertEqual(result.status, broker.STATUS_DRY_RUN)
        self.assertEqual(result.guard["blocked_by"], broker.REASON_ORDER_LIMIT)
        self.assertIn("1회 주문 상한", result.reason)
        self.assertEqual(FakeLiveBroker.placed, [])

    def test_state_file_survives_a_restart(self):
        with self.live_env(REAL_DAILY_MAX_KRW="100000"):
            first_wrapper = self.wrapped()
            first = first_wrapper.place_buy(self.order(amount_krw=60_000.0, run_id="boot-a"))
            self.assertEqual(first.status, broker.STATUS_FILLED)
            self.assertTrue(os.path.exists(self.guard_state))
            # 서버 재기동 — 메모리를 버리고 파일에서 다시 읽는다
            broker.clear_cache()
            second_wrapper = broker.GuardedBroker(FakeLiveBroker(), self.guard())
            self.assertAlmostEqual(second_wrapper.guard.ledger.daily_used(), 60_000.0, places=2)
            blocked = second_wrapper.place_buy(self.order(amount_krw=60_000.0, run_id="boot-b"))
            duplicate = second_wrapper.place_buy(self.order(amount_krw=60_000.0, run_id="boot-a"))
        self.assertEqual(blocked.reason, broker.REASON_DAILY_LIMIT)
        self.assertEqual(duplicate.reason, broker.REASON_DAILY_LIMIT)

    def test_memory_only_mode_when_state_path_is_disabled(self):
        with patch.dict(os.environ, {"BROKER_GUARD_STATE": ""}):
            guard = broker.guard_for(self.db_path)
            self.assertIsNone(guard.ledger.path)
            with self.live_env():
                result = broker.GuardedBroker(FakeLiveBroker(), guard).place_buy(self.order())
        self.assertEqual(result.status, broker.STATUS_FILLED)

    def test_invalid_order_is_blocked_even_in_paper(self):
        result = self.wrapped(broker.PaperBroker(market="US", db_path=self.db_path),
                             self.guard()).place_buy(self.order(ticker="", amount_krw=10_000.0))
        self.assertEqual(result.status, broker.STATUS_BLOCKED)
        self.assertEqual(result.reason, broker.REASON_INVALID)

    def test_order_key_normalizes_case(self):
        upper = broker.OrderRequest(side="buy", ticker="aapl", market="us", run_id="R1",
                                    price=100.0, amount_krw=10_000.0)
        lower = broker.OrderRequest(side="BUY", ticker="AAPL", market="US", run_id="R1",
                                    price=100.0, amount_krw=10_000.0)
        self.assertEqual(upper.side, "BUY")
        self.assertEqual(upper.ticker, "AAPL")
        self.assertEqual(upper.order_key(), lower.order_key())
        self.assertEqual(upper.client_order_id(), lower.client_order_id())
        self.assertNotIn("|", upper.client_order_id())

    def test_registered_live_adapter_is_guarded_through_get_adapter(self):
        broker.ADAPTERS["fake"] = FakeLiveBroker
        self.addCleanup(broker.ADAPTERS.pop, "fake", None)
        with patch.dict(os.environ, {"BROKER_MODE": "fake", "FAKE_BROKER_KEY": "test-key",
                                     "REAL_DRY_RUN": "0"}):
            broker.clear_cache()
            adapter = broker.get_adapter(market="US", db_path=self.db_path)
            self.assertTrue(adapter.is_live)
            self.assertEqual(adapter.name, "fake")
            result = adapter.place_buy(self.order(run_id="e2e"))
        self.assertEqual(result.status, broker.STATUS_BLOCKED)
        self.assertEqual(result.reason, broker.REASON_KILL_SWITCH)
        self.assertEqual(FakeLiveBroker.placed, [])


class LiveModeRunTests(BrokerCase):
    def test_paper_run_refuses_in_live_mode(self):
        with patch.dict(os.environ, {"BROKER_MODE": "kis"}):
            broker.clear_cache()
            result = paper_trading.run(market="US", force=True,
                                       candidate_fn=self.candidates({"US": [self.us("AAPL")]}),
                                       price_fn=self.prices({"AAPL": 100.0}), fx_fn=self.fx())
        self.assertEqual(result["skipped"], "live_broker_not_wired")
        self.assertEqual(paper_store.trade_count(self.db_path, paper_store.ACCOUNT_US), 0)
        account = paper_store.get_account(paper_store.ACCOUNT_US, self.db_path)
        self.assertEqual(account.get("last_run_us_date") or "", "")  # 오늘 몫을 먹지 않는다
        self.assertEqual(account["cash_krw"], SEED)

    def test_back_to_paper_mode_runs_after_a_refusal(self):
        with patch.dict(os.environ, {"BROKER_MODE": "kis"}):
            broker.clear_cache()
            refused = paper_trading.run(market="US", force=True,
                                        candidate_fn=self.candidates({"US": [self.us("AAPL")]}),
                                        price_fn=self.prices({"AAPL": 100.0}), fx_fn=self.fx())
        self.assertEqual(refused["skipped"], "live_broker_not_wired")
        broker.clear_cache()
        result = paper_trading.run(market="US", force=True,
                                   candidate_fn=self.candidates({"US": [self.us("AAPL")]}),
                                   price_fn=self.prices({"AAPL": 100.0}), fx_fn=self.fx())
        self.assertEqual(len(result["buys"]), 1)


@unittest.skipIf(TestClient is None, "httpx 미설치")
class HealthEndpointTests(BrokerCase):
    def setUp(self):
        super().setUp()
        import api
        self.client = TestClient(api.app)

    def test_root_exposes_the_broker_block(self):
        body = self.client.get("/").json()
        self.assertEqual(body["status"], "ok")
        block = body["broker"]
        self.assertEqual(block["mode"], "paper")
        self.assertFalse(block["live"])
        self.assertFalse(block["real_trading_enabled"])
        self.assertTrue(block["dry_run"])
        # 헬스 첵은 로그인 없이 누구나 본다 — 어댑터별 설정·주문 원장 경로는 담지 않는다.
        for hidden in ("supported_adapters", "adapters", "guard", "requested_mode", "label"):
            self.assertNotIn(hidden, block)
        self.assertNotIn("state_path", self.client.get("/").text)

    def test_root_keeps_the_existing_fields(self):
        body = self.client.get("/").json()
        for key in ("build", "finnhub", "storage_backend", "memory", "sp500_cache",
                    "kospi200_cache", "analysis_cache", "paper_auto", "fcm", "yahoo_guard"):
            self.assertIn(key, body)
        self.assertEqual(body["storage_backend"], "sqlite")

    def test_head_is_still_ok(self):
        response = self.client.head("/")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
