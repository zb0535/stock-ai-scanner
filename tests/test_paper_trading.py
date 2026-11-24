"""AI 모의투자(paper_store + paper_trading + /api/paper/*) 테스트.

다루는 것
1. 계좌 기본값: 원화 시드머니 ₩10,000,000 · 시장 ALL · 자동 실행 켬
2. 현금 한도: AI가 현금보다 큰 금액을 말해도 체결 금액 합계가 현금을 넘지 않는다
3. 수량 단위: 미국은 소수점 4자리, 한국은 1주 정수
4. 손익 계산: 미국 종목은 매수 환율(원가) 대비 현재 환율로 원화 손익이 계산된다(환차 포함)
5. 매도: 손절(-8%) 기준이면 자동으로 전량 매도되고 현금이 늘어난다
6. 리셋: 포지션·체결·스냅샷이 지워지고 새 시드머니로 시작한다
7. 중복 실행 방지: 같은 날 `force=False`면 건너뛰고, `force=True`면 다시 돈다
8. 폴백: AI가 실패하면 정량 점수 상위 종목으로 매수한다
9. 자동 실행 시각: 한국 15:40·미국 06:20 KST 이후 시장별 하루 1회
10. HTTP 표면: GET /api/paper/status, POST /api/paper/configure, POST /api/paper/run
11. 거래 비용: 매수 예산에 수수료 포함(현금 음수 금지)·매도 수수료+증권거래세·실현손익 순액
12. 순손익(net_pnl)·환차: 지금 팔 때의 순수취액으로 손절·익절을 판정하고 프롬프트에 전달
13. DB 마이그레이션: 기존 SQLite 파일에 fee_krw 컬럼을 더해도 계좌·체결이 남는다

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paper_store  # noqa: E402
import paper_trading  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

KST = timezone(timedelta(hours=9))


def us(ticker, price=100.0, score=80.0, name=""):
    return {"ticker": ticker, "name": name or ticker, "currency": "USD", "price": price,
            "quant_score": score, "signals": ["정배열일치"], "rsi14": 55.0, "trend": "정배열",
            "price_source": "finnhub_quote"}


def kr(ticker, price=70000.0, score=80.0, name=""):
    return {"ticker": ticker, "name": name or ticker, "currency": "KRW", "price": price,
            "quant_score": score, "signals": ["정배열일치"], "rsi14": 55.0, "trend": "정배열",
            "price_source": "naver_realtime"}


class PaperCase(unittest.TestCase):
    """테스트마다 빈 SQLite 파일 + 고정 환율(1,300원)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "paper.db")
        patcher = patch.dict(os.environ, {"HISTORY_DB_PATH": self.db_path, "PAPER_AUTO": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        paper_trading.clear_caches()

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

    def ai(self, payload):
        def _call(prompt):
            return json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
        return _call


# ---------------------------------------------------------------------------
# 1. 계좌·설정·리셋
# ---------------------------------------------------------------------------


class AccountTests(PaperCase):
    def test_default_account_is_krw_ten_million(self):
        account = paper_store.get_account()
        self.assertEqual(account["initial_cash_krw"], 10_000_000)
        self.assertEqual(account["cash_krw"], 10_000_000)
        self.assertEqual(account["markets"], "ALL")
        self.assertEqual(account["strategy"], "balanced")
        self.assertTrue(account["auto_enabled"])
        self.assertIsNone(account["last_run_us_date"])

    def test_init_store_seeds_equity_point(self):
        paper_store.init_store()
        rows = paper_store.equity_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_value_krw"], 10_000_000)
        self.assertEqual(rows[0]["return_pct"], 0.0)
        # 다시 불러도 스냅샷이 늘지 않는다(같은 날 UNIQUE).
        paper_store.init_store()
        self.assertEqual(len(paper_store.equity_history()), 1)

    def test_configure_changes_seed_and_markets(self):
        out = paper_trading.configure(initial_cash_krw=5_000_000, markets="KR",
                                      strategy="value", auto_enabled=False)
        self.assertTrue(out["ok"])
        account = out["account"]
        self.assertEqual(account["initial_cash_krw"], 5_000_000)
        self.assertEqual(account["cash_krw"], 5_000_000)
        self.assertEqual(account["markets"], "KR")
        self.assertEqual(account["strategy"], "value")
        self.assertFalse(account["auto_enabled"])

    def test_configure_validates_ranges(self):
        for kwargs in ({"initial_cash_krw": 1_000},          # 10만 원 미만
                       {"initial_cash_krw": 5_000_000_000},  # 10억 초과
                       {"markets": "JP"},
                       {"strategy": "scalp"}):
            with self.assertRaises(ValueError):
                paper_trading.configure(**kwargs)

    def test_configure_without_reset_moves_cash_by_delta(self):
        paper_trading.configure(initial_cash_krw=20_000_000)
        account = paper_store.get_account()
        self.assertEqual(account["initial_cash_krw"], 20_000_000)
        self.assertEqual(account["cash_krw"], 20_000_000)

    def test_configure_cannot_push_cash_negative(self):
        # 주식에 돈이 들어간 상태에서 시드머니를 현금보다 크게 줄이면 거절한다.
        paper_store.update_account({"cash_krw": 100_000})
        with self.assertRaises(ValueError) as ctx:
            paper_trading.configure(initial_cash_krw=paper_store.MIN_INITIAL_CASH_KRW)
        self.assertIn("cash_below_zero", str(ctx.exception))

    def test_reset_clears_everything(self):
        paper_store.add_position("AAPL", "US", "USD", 1.5, 100.0, 1300.0, 195_000.0)
        paper_store.record_trade({"ticker": "AAPL", "side": "BUY", "shares": 1.5, "price": 100.0,
                                  "currency": "USD", "fx_rate": 1300.0, "amount_krw": 195_000.0})
        paper_store.record_equity("2026-11-20", 10_000_000, 9_805_000, 195_000, 0.0)

        out = paper_trading.configure(initial_cash_krw=3_000_000, reset=True)
        self.assertTrue(out["reset"])
        self.assertEqual(paper_store.list_positions(), [])
        self.assertEqual(paper_store.recent_trades(), [])
        account = paper_store.get_account()
        self.assertEqual(account["cash_krw"], 3_000_000)
        self.assertEqual(account["initial_cash_krw"], 3_000_000)
        self.assertIsNone(account["last_run_date"])
        history = paper_store.equity_history()
        self.assertEqual(len(history), 1)                     # 새 출발점 한 줄만
        self.assertEqual(history[0]["total_value_krw"], 3_000_000)


# ---------------------------------------------------------------------------
# 2~5. 매매 실행
# ---------------------------------------------------------------------------


class RunTests(PaperCase):
    def setUp(self):
        super().setUp()
        paper_trading.configure(initial_cash_krw=10_000_000, markets="ALL")

    def test_buy_respects_cash_limit(self):
        """AI가 현금의 3배를 말해도 체결 합계(수수료 포함)는 현금을 넘지 않는다."""
        ai = self.ai({"market_note": "공격적", "sells": [], "buys": [
            {"ticker": "NVDA", "amount_krw": 20_000_000, "reason": "1위"},
            {"ticker": "AAPL", "amount_krw": 20_000_000, "reason": "2위"},
            {"ticker": "MSFT", "amount_krw": 20_000_000, "reason": "3위"},
        ]})
        result = paper_trading.run(
            markets="US", force=True, ai_call=ai,
            candidate_fn=self.candidates({"US": [us("NVDA", 1000), us("AAPL", 200), us("MSFT", 400)]}),
            price_fn=self.prices({"NVDA": 1000, "AAPL": 200, "MSFT": 400}), fx_fn=self.fx())
        spent = sum(trade["settle_krw"] for trade in result["buys"])   # 원금 + 수수료
        fees = sum(trade["fee_krw"] for trade in result["buys"])
        account = paper_store.get_account()
        self.assertEqual(len(result["buys"]), 3)
        self.assertGreater(fees, 0)
        self.assertLessEqual(spent, 10_000_000 + 0.01)
        self.assertGreaterEqual(account["cash_krw"], 0)
        self.assertAlmostEqual(account["cash_krw"], 10_000_000 - spent, places=2)
        # 종목 비중 상한(총자산 30%)도 지켜진다.
        for trade in result["buys"]:
            self.assertLessEqual(trade["settle_krw"], 3_000_000 + 1)

    def test_us_shares_rounded_to_four_decimals(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "AAPL", "amount_krw": 1_000_000, "reason": "x"}]})
        paper_trading.run(markets="US", force=True, ai_call=ai,
                          candidate_fn=self.candidates({"US": [us("AAPL", 211.37)]}),
                          price_fn=self.prices({"AAPL": 211.37}), fx_fn=self.fx(1300))
        position = paper_store.list_positions()[0]
        # 1,000,000은 수수료 포함 예산 → 원금 예산 1,000,000/1.0015 = 998,502.25
        # / (211.37 * 1300) = 3.63384... → 3.6338주(소수점 4자리 버림)
        self.assertEqual(position["shares"], 3.6338)
        self.assertEqual(position["currency"], "USD")
        principal = round(3.6338 * 211.37 * 1300, 2)
        self.assertAlmostEqual(position["cost_krw"], round(principal * 1.0015, 2), places=2)

    def test_kr_shares_are_integer(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "005930.KS", "amount_krw": 1_000_000, "reason": "x"}]})
        paper_trading.run(markets="KR", force=True, ai_call=ai,
                          candidate_fn=self.candidates({"KR": [kr("005930.KS", 71_300)]}),
                          price_fn=self.prices({"005930.KS": 71_300}), fx_fn=self.fx())
        position = paper_store.list_positions()[0]
        self.assertEqual(position["shares"], 14.0)          # 1,000,000 / 71,300 = 14.02 → 14주
        self.assertEqual(position["currency"], "KRW")
        self.assertEqual(position["fx_rate"], 1.0)
        # 원가 = 체결원금 998,200 + 매수 수수료 0.015%(149.73)
        self.assertAlmostEqual(position["cost_krw"], 998_349.73, places=2)

    def test_krw_pnl_includes_fx_move(self):
        """달러 가격이 그대로여도 원/달러가 오르면 원화 수익이 난다."""
        ai = self.ai({"sells": [], "buys": [{"ticker": "MSFT", "amount_krw": 2_604_000, "reason": "x"}]})
        paper_trading.run(markets="US", force=True, ai_call=ai,
                          candidate_fn=self.candidates({"US": [us("MSFT", 400)]}),
                          price_fn=self.prices({"MSFT": 400}), fx_fn=self.fx(1300))
        position = paper_store.list_positions()[0]
        # 수수료 포함 예산 2,604,000 → 원금 예산 2,600,099 → 약 5주(소수점 4자리)
        self.assertAlmostEqual(position["shares"], 5.0, places=3)
        cost = position["cost_krw"]
        self.assertAlmostEqual(cost, round(5.0001 * 400 * 1300 * 1.0015, 2), places=2)

        # 가격 그대로, 환율 1,300 → 1,430 (+10%)
        body = paper_trading.status(price_fn=self.prices({"MSFT": 400}), fx_fn=self.fx(1430))
        row = body["positions"][0]
        value = round(position["shares"] * 400 * 1430, 2)
        self.assertAlmostEqual(row["market_value_krw"], value, places=2)
        self.assertAlmostEqual(row["cost_krw"], cost, places=2)
        self.assertAlmostEqual(row["pnl_pct"], round((value - cost) / cost * 100, 2), places=2)
        self.assertAlmostEqual(row["fx_change_pct"], 10.0, places=2)   # 환차만으로 +10%
        total = value + (10_000_000 - cost)
        self.assertAlmostEqual(body["account"]["total_return_pct"],
                               round((total - 10_000_000) / 10_000_000 * 100, 2), places=2)

    def test_stop_loss_sells_position(self):
        paper_store.add_position("AAPL", "US", "USD", 10.0, 200.0, 1300.0, 2_600_000.0)
        paper_store.update_account({"cash_krw": 7_400_000})
        result = paper_trading.run(markets="US", force=True, ai_call=self.ai({"sells": [], "buys": []}),
                                   candidate_fn=self.candidates({"US": []}),
                                   price_fn=self.prices({"AAPL": 180}), fx_fn=self.fx(1300))
        self.assertEqual(len(result["sells"]), 1)
        sell = result["sells"][0]
        self.assertEqual(sell["side"], "SELL")
        self.assertEqual(sell["shares"], 10.0)
        # 총대금 2,340,000 - 미국 매도비용 0.15%(3,510) = 2,336,490 입금
        self.assertAlmostEqual(sell["amount_krw"], 2_340_000, places=2)
        self.assertAlmostEqual(sell["fee_krw"], 3_510, places=2)
        self.assertAlmostEqual(sell["settle_krw"], 2_336_490, places=2)
        self.assertAlmostEqual(sell["realized_pnl_krw"], -263_510, places=2)   # -10% + 수수료
        self.assertEqual(paper_store.list_positions(), [])
        self.assertAlmostEqual(paper_store.get_account()["cash_krw"],
                               7_400_000 + 2_336_490, places=2)

    def test_take_profit_rule_sells(self):
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0)
        paper_store.update_account({"cash_krw": 9_300_000})
        result = paper_trading.run(markets="KR", force=True, ai_call=self.ai({"sells": [], "buys": []}),
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 90_000}), fx_fn=self.fx())
        self.assertEqual(len(result["sells"]), 1)
        # 총대금 900,000 - 한국 매도비용 0.195%(1,755) = 898,245 → 실현 198,245
        self.assertAlmostEqual(result["sells"][0]["fee_krw"], 1_755, places=2)
        self.assertAlmostEqual(result["sells"][0]["realized_pnl_krw"], 198_245, places=2)

    def test_ai_partial_sell_keeps_fraction(self):
        paper_store.add_position("AAPL", "US", "USD", 10.0, 100.0, 1300.0, 1_300_000.0)
        paper_store.update_account({"cash_krw": 8_700_000})
        ai = self.ai({"sells": [{"ticker": "AAPL", "fraction": 0.5, "reason": "비중 축소"}],
                      "buys": []})
        result = paper_trading.run(markets="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": []}),
                                   price_fn=self.prices({"AAPL": 110}), fx_fn=self.fx())
        self.assertEqual(result["sells"][0]["shares"], 5.0)
        remaining = paper_store.list_positions()[0]
        self.assertEqual(remaining["shares"], 5.0)
        self.assertAlmostEqual(remaining["cost_krw"], 650_000, places=2)

    def test_unknown_tickers_are_ignored(self):
        ai = self.ai({"sells": [{"ticker": "TSLA", "reason": "없는 보유"}], "buys": [
            {"ticker": "ZZZZ", "amount_krw": 1_000_000, "reason": "후보 아님"},
            {"ticker": "AAPL", "amount_krw": 100, "reason": "너무 작음"},
        ]})
        result = paper_trading.run(markets="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": [us("AAPL", 100)]}),
                                   price_fn=self.prices({"AAPL": 100}), fx_fn=self.fx())
        self.assertEqual(result["buys"], [])
        self.assertEqual(result["sells"], [])
        reasons = " ".join(item["reason"] for item in result["skipped"])
        self.assertIn("후보 목록에 없는", reasons)
        self.assertIn("최소 주문", reasons)

    def test_quant_fallback_buys_without_ai(self):
        result = paper_trading.run(
            markets="ALL", force=True, ai_call=self.ai("not json at all"),
            candidate_fn=self.candidates({"US": [us("NVDA", 500, 91), us("AAPL", 200, 88)],
                                          "KR": [kr("005930.KS", 70_000, 86)]}),
            price_fn=self.prices({"NVDA": 500, "AAPL": 200, "005930.KS": 70_000}),
            fx_fn=self.fx())
        self.assertEqual(result["ai_source"], "quant")
        self.assertEqual(len(result["buys"]), 3)
        self.assertIn("정량 점수", result["buys"][0]["reason"])

    def test_run_records_equity_and_last_run_dates(self):
        paper_trading.run(markets="US", force=True, ai_call=self.ai({"sells": [], "buys": []}),
                          candidate_fn=self.candidates({"US": []}), price_fn=self.prices({}),
                          fx_fn=self.fx())
        account = paper_store.get_account()
        today = paper_store.kst_today_str()
        self.assertEqual(account["last_run_us_date"], today)
        self.assertIsNone(account["last_run_kr_date"])   # 미국만 돌았다
        history = paper_store.equity_history()
        self.assertEqual(history[-1]["date"], today)

    def test_duplicate_run_same_day_skipped_unless_forced(self):
        kwargs = dict(markets="US", ai_call=self.ai({"sells": [], "buys": []}),
                      candidate_fn=self.candidates({"US": []}), price_fn=self.prices({}),
                      fx_fn=self.fx())
        first = paper_trading.run(force=True, **kwargs)
        self.assertTrue(first["ok"])
        second = paper_trading.run(force=False, **kwargs)
        self.assertEqual(second.get("skipped"), "already_ran_today")
        third = paper_trading.run(force=True, **kwargs)
        self.assertTrue(third["ok"])
        self.assertNotEqual(third.get("skipped"), "already_ran_today")

    def test_markets_setting_limits_run_scope(self):
        paper_trading.configure(markets="KR")
        result = paper_trading.run(markets="US", force=True, ai_call=self.ai({"buys": []}),
                                   candidate_fn=self.candidates({}), price_fn=self.prices({}),
                                   fx_fn=self.fx())
        self.assertEqual(result["markets"], ["KR"])   # 설정 밖 시장은 무시

    def test_missing_price_marks_position_at_cost(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0)
        paper_store.update_account({"cash_krw": 9_870_000})
        body = paper_trading.status(price_fn=self.prices({}), fx_fn=self.fx())
        row = body["positions"][0]
        self.assertEqual(row["price_source"], "avg_price_fallback")
        self.assertAlmostEqual(row["market_value_krw"], 130_000, places=2)
        self.assertAlmostEqual(row["pnl_pct"], 0.0, places=2)


# ---------------------------------------------------------------------------
# 6. 자동 실행 시각
# ---------------------------------------------------------------------------


class AutoScheduleTests(PaperCase):
    def account(self, markets="ALL", auto=True, us=None, kr=None):
        paper_store.update_account({"markets": markets, "auto_enabled": 1 if auto else 0,
                                    "last_run_us_date": us, "last_run_kr_date": kr})
        return paper_store.get_account()

    def test_nothing_due_before_us_close(self):
        account = self.account()
        self.assertEqual(paper_trading.auto_due_markets(
            account, datetime(2026, 11, 24, 5, 0, tzinfo=KST)), [])

    def test_kr_due_after_1540(self):
        account = self.account(us="2026-11-24")      # 미국은 오늘 아침에 돌았다
        self.assertEqual(paper_trading.auto_due_markets(
            account, datetime(2026, 11, 24, 15, 41, tzinfo=KST)), ["KR"])

    def test_us_due_after_0620_next_morning(self):
        account = self.account()
        self.assertEqual(paper_trading.auto_due_markets(
            account, datetime(2026, 11, 25, 6, 21, tzinfo=KST)), ["US"])

    def test_already_ran_today_is_not_due(self):
        account = self.account(us="2026-11-25", kr="2026-11-25")
        self.assertEqual(paper_trading.auto_due_markets(
            account, datetime(2026, 11, 25, 16, 0, tzinfo=KST)), [])

    def test_market_filter_and_auto_off(self):
        kr_only = self.account(markets="KR")
        self.assertEqual(paper_trading.auto_due_markets(
            kr_only, datetime(2026, 11, 25, 6, 30, tzinfo=KST)), [])
        off = self.account(auto=False)
        self.assertEqual(paper_trading.auto_due_markets(
            off, datetime(2026, 11, 25, 16, 30, tzinfo=KST)), [])

    def test_us_due_wraps_to_next_day_when_done(self):
        account = self.account(us="2026-11-24")
        info = paper_trading.next_auto_info(account, datetime(2026, 11, 24, 7, 0, tzinfo=KST))
        self.assertEqual(info["market"], "KR")          # 오늘 15:40 한국이 다음
        self.assertTrue(info["at"].startswith("2026-11-24T15:40"))

    def test_next_auto_says_catch_up_when_due_now(self):
        account = self.account()          # 오늘 아직 아무 시장도 안 돌았다
        info = paper_trading.next_auto_info(account, datetime(2026, 11, 24, 19, 30, tzinfo=KST))
        self.assertEqual(info["market"], "KR")
        self.assertIn("곧 자동 실행", info["label"])

    def test_next_auto_after_all_ran_today(self):
        account = self.account(us="2026-11-24", kr="2026-11-24")
        info = paper_trading.next_auto_info(account, datetime(2026, 11, 24, 20, 0, tzinfo=KST))
        self.assertEqual(info["market"], "US")
        self.assertTrue(info["at"].startswith("2026-11-25T06:20"))

    def test_status_reports_schedule(self):
        body = paper_trading.status(price_fn=self.prices({}), fx_fn=self.fx())
        markets = {item["market"]: item for item in body["auto_schedule"]}
        self.assertEqual(markets["KR"]["at"], "15:40")
        self.assertEqual(markets["US"]["at"], "06:20")
        self.assertTrue(markets["KR"]["enabled"])
        self.assertEqual(body["account"]["markets"], "ALL")


# ---------------------------------------------------------------------------
# 5.5 동시 실행·현금 원자성 (실서버에서 자동·수동 실행이 겹쳐 초과 매수한 자리)
# ---------------------------------------------------------------------------


class ConcurrencyTests(PaperCase):
    def test_second_run_is_rejected_while_one_is_running(self):
        """자동 실행과 수동 실행이 겹치면 두 번째는 기다리지 않고 건너뛴다."""
        acquired = paper_trading._RUN_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            result = paper_trading.run(markets="KR", force=True,
                                       ai_call=self.ai({"buys": []}),
                                       candidate_fn=self.candidates({}), price_fn=self.prices({}),
                                       fx_fn=self.fx())
        finally:
            paper_trading._RUN_LOCK.release()
        self.assertEqual(result.get("skipped"), "already_running")
        self.assertEqual(paper_store.list_positions(), [])

    def test_store_refuses_overdraft_and_leaves_no_position(self):
        with self.assertRaises(ValueError) as ctx:
            paper_store.apply_buy(
                "AAPL", "US", "Apple", "USD", 100.0, 200.0, 1300.0, 26_000_000.0, "과다 매수",
                -26_000_000.0,
                {"ticker": "AAPL", "market": "US", "side": "BUY", "shares": 100.0,
                 "price": 200.0, "currency": "USD", "fx_rate": 1300.0, "amount_krw": 26_000_000.0},
            )
        self.assertIn("cash_below_zero", str(ctx.exception))
        self.assertEqual(paper_store.list_positions(), [])
        self.assertEqual(paper_store.recent_trades(), [])
        self.assertEqual(paper_store.get_account()["cash_krw"], 10_000_000)

    def test_cash_debits_accumulate_across_fills(self):
        for ticker in ("AAPL", "MSFT"):
            paper_store.apply_buy(
                ticker, "US", ticker, "USD", 1.0, 200.0, 1300.0, 260_000.0, "테스트",
                -260_000.0,
                {"ticker": ticker, "market": "US", "side": "BUY", "shares": 1.0, "price": 200.0,
                 "currency": "USD", "fx_rate": 1300.0, "amount_krw": 260_000.0},
            )
        account = paper_store.get_account()
        self.assertAlmostEqual(account["cash_krw"], 10_000_000 - 520_000, places=2)
        self.assertEqual(len(paper_store.recent_trades()), 2)

    def test_sell_adds_proceeds_to_cash(self):
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0)
        paper_store.update_account({"cash_krw": 9_300_000})
        paper_store.apply_sell(
            "005930.KS", 4.0, 280_000.0, 400_000.0,
            {"ticker": "005930.KS", "market": "KR", "side": "SELL", "shares": 4.0,
             "price": 100_000.0, "currency": "KRW", "fx_rate": 1.0, "amount_krw": 400_000.0,
             "fee_krw": 780.0, "realized_pnl_krw": 119_220.0},
        )
        self.assertAlmostEqual(paper_store.get_account()["cash_krw"], 9_700_000, places=2)
        self.assertAlmostEqual(paper_store.list_positions()[0]["shares"], 6.0, places=4)
        self.assertAlmostEqual(paper_store.recent_trades()[0]["fee_krw"], 780.0, places=2)


# ---------------------------------------------------------------------------
# 5.6 거래 비용 — 수수료(한국 0.015/0.195% · 미국 0.15%) · 증권거래세 · 환차
# ---------------------------------------------------------------------------


class FeeTests(PaperCase):
    def setUp(self):
        super().setUp()
        paper_trading.configure(initial_cash_krw=10_000_000, markets="ALL")

    def test_fee_policy_constants_match_the_korean_standard(self):
        policy = paper_trading.fee_policy()
        self.assertEqual(policy["KR"]["buy"]["total_pct"], 0.015)
        self.assertEqual(policy["KR"]["sell"]["commission_pct"], 0.015)
        self.assertEqual(policy["KR"]["sell"]["tax_pct"], 0.18)
        self.assertEqual(policy["KR"]["sell"]["total_pct"], 0.195)
        self.assertEqual(policy["US"]["buy"]["total_pct"], 0.15)
        self.assertEqual(policy["US"]["sell"]["total_pct"], 0.15)
        # 표(%)와 계산에 쓰는 비율이 어긋나지 않는다.
        self.assertAlmostEqual(paper_trading.BUY_FEE_RATE["KR"] * 100, 0.015, places=9)
        self.assertAlmostEqual(paper_trading.SELL_FEE_RATE["KR"] * 100, 0.195, places=9)
        self.assertAlmostEqual(paper_trading.BUY_FEE_RATE["US"] * 100, 0.15, places=9)
        self.assertAlmostEqual(paper_trading.SELL_FEE_RATE["US"] * 100, 0.15, places=9)
        self.assertAlmostEqual(paper_trading.BUY_FEE_RATE["KR"] + paper_trading.SELL_TAX_RATE["KR"],
                               paper_trading.SELL_FEE_RATE["KR"], places=9)

    def test_buy_spends_principal_plus_fee_within_budget(self):
        """현금 전부를 쓰라고 해도 원금+수수료가 현금을 넘지 않고, 잔고가 음수가 되지 않는다."""
        ai = self.ai({"sells": [], "buys": [{"ticker": "005930.KS", "amount_krw": 10_000_000,
                                               "reason": "전액"}]})
        result = paper_trading.run(markets="KR", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"KR": [kr("005930.KS", 71_300)]}),
                                   price_fn=self.prices({"005930.KS": 71_300}), fx_fn=self.fx())
        buy = result["buys"][0]
        self.assertAlmostEqual(buy["settle_krw"], buy["amount_krw"] + buy["fee_krw"], places=2)
        self.assertLessEqual(buy["settle_krw"], 10_000_000 + 0.01)
        self.assertAlmostEqual(buy["fee_krw"], round(buy["amount_krw"] * 0.00015, 2), places=2)
        account = paper_store.get_account()
        self.assertGreaterEqual(account["cash_krw"], 0)
        self.assertAlmostEqual(account["cash_krw"], 10_000_000 - buy["settle_krw"], places=2)
        # 원가(수수료 포함)가 포지션에 그대로 들어간다.
        self.assertAlmostEqual(paper_store.list_positions()[0]["cost_krw"], buy["settle_krw"], places=2)

    def test_kr_sell_deducts_commission_and_tax(self):
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0)
        paper_store.update_account({"cash_krw": 9_300_000})
        ai = self.ai({"sells": [{"ticker": "005930.KS", "fraction": 1.0, "reason": "정리"}], "buys": []})
        result = paper_trading.run(markets="KR", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 100_000}), fx_fn=self.fx())
        sell = result["sells"][0]
        self.assertAlmostEqual(sell["amount_krw"], 1_000_000, places=2)      # 총 매도대금
        self.assertAlmostEqual(sell["fee_krw"], 1_950, places=2)             # 0.195%
        self.assertAlmostEqual(sell["settle_krw"], 998_050, places=2)        # 실제 입금
        self.assertAlmostEqual(sell["realized_pnl_krw"], 298_050, places=2)  # 30만 원 - 비용
        self.assertAlmostEqual(paper_store.get_account()["cash_krw"],
                               9_300_000 + 998_050, places=2)

    def test_us_sell_deducts_commission_and_fx_spread(self):
        paper_store.add_position("AAPL", "US", "USD", 10.0, 100.0, 1300.0, 1_300_000.0)
        paper_store.update_account({"cash_krw": 8_700_000})
        ai = self.ai({"sells": [{"ticker": "AAPL", "fraction": 1.0, "reason": "정리"}], "buys": []})
        result = paper_trading.run(markets="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": []}),
                                   price_fn=self.prices({"AAPL": 110}), fx_fn=self.fx(1300))
        sell = result["sells"][0]
        self.assertAlmostEqual(sell["amount_krw"], 1_430_000, places=2)
        self.assertAlmostEqual(sell["fee_krw"], 2_145, places=2)             # 0.15%
        self.assertAlmostEqual(sell["settle_krw"], 1_427_855, places=2)
        self.assertAlmostEqual(sell["realized_pnl_krw"], 127_855, places=2)

    def test_net_pnl_reflects_fx_loss_and_sell_cost(self):
        """달러 가격이 그대로여도 환율이 내리면 순손실 — 수수료까지 더해 진다."""
        paper_store.add_position("MSFT", "US", "USD", 10.0, 100.0, 1300.0, 1_300_000.0)
        paper_store.update_account({"cash_krw": 8_700_000})
        body = paper_trading.status(price_fn=self.prices({"MSFT": 100}), fx_fn=self.fx(1250))
        row = body["positions"][0]
        self.assertAlmostEqual(row["market_value_krw"], 1_250_000, places=2)
        self.assertAlmostEqual(row["pnl_pct"], -3.85, places=2)              # 주가·환율만
        self.assertAlmostEqual(row["fx_change_pct"], -3.85, places=2)        # 환차만
        self.assertAlmostEqual(row["buy_fx_rate"], 1300.0, places=2)
        self.assertAlmostEqual(row["sell_fee_krw"], 1_875, places=2)         # 0.15%
        self.assertAlmostEqual(row["net_proceeds_krw"], 1_248_125, places=2)
        self.assertAlmostEqual(row["net_pnl_krw"], -51_875, places=2)
        self.assertAlmostEqual(row["net_pnl_pct"], round(-51_875 / 1_300_000 * 100, 2), places=2)
        # 순손익은 항상 수수료만큼 손해 쪽이다.
        self.assertLess(row["net_pnl_krw"], row["pnl_krw"])

    def test_stop_loss_triggers_on_net_pnl_not_gross(self):
        """주가만 보면 -7.9%라 손절 전이지만, 수수료·세금을 빼면 -8%를 넘어 팔린다."""
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0)
        paper_store.update_account({"cash_krw": 9_300_000})
        flat = paper_trading.status(price_fn=self.prices({"005930.KS": 64_500}), fx_fn=self.fx())
        row = flat["positions"][0]
        self.assertGreater(row["pnl_pct"], -8.0)          # 주가 등락만 보면 아직 -8% 아님
        self.assertLessEqual(row["net_pnl_pct"], -8.0)    # 수수료·세금 반영하면 넘는다

        result = paper_trading.run(markets="KR", force=True,
                                   ai_call=self.ai({"sells": [], "buys": []}),
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 64_500}), fx_fn=self.fx())
        self.assertEqual(len(result["sells"]), 1)
        self.assertIn("순손익", result["sells"][0]["reason"])

    def test_take_profit_rule_counts_fees_too(self):
        """+25%에 딱 걸친 종목은 수수료를 빼면 익절 기준에 못 미쳐 팔지 않는다."""
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0)
        paper_store.update_account({"cash_krw": 9_300_000})
        body = paper_trading.status(price_fn=self.prices({"005930.KS": 87_500}), fx_fn=self.fx())
        self.assertAlmostEqual(body["positions"][0]["pnl_pct"], 25.0, places=2)
        self.assertLess(body["positions"][0]["net_pnl_pct"], 25.0)

        result = paper_trading.run(markets="KR", force=True,
                                   ai_call=self.ai({"sells": [], "buys": []}),
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 87_500}), fx_fn=self.fx())
        self.assertEqual(result["sells"], [])

    def test_prompt_tells_the_ai_the_fee_and_fx_numbers(self):
        paper_store.add_position("MSFT", "US", "USD", 2.0, 100.0, 1300.0, 260_000.0)
        paper_store.update_account({"cash_krw": 9_740_000})
        body = paper_trading.status(price_fn=self.prices({"MSFT": 100}), fx_fn=self.fx(1280))
        prompt = paper_trading.build_prompt(
            paper_store.get_account(), ["US"], 9_740_000, 9_996_000,
            body["positions"], {"US": [us("MSFT", 100)]},
            {"rate": 1280.0, "source": "test"})
        for key in ('"net_pnl_krw"', '"net_pnl_pct"', '"buy_fx_rate"', '"current_fx_rate"',
                    '"fx_change_pct"', '"sell_fee_krw"', '"price_pnl_pct"'):
            self.assertIn(key, prompt)
        self.assertIn("0.195%", prompt)      # 한국 매도 비용 안내
        self.assertIn("0.15%", prompt)       # 미국 매도 비용 안내
        self.assertIn("환전 스프레드", prompt)

    def test_status_exposes_fee_policy(self):
        body = paper_trading.status(price_fn=self.prices({}), fx_fn=self.fx())
        self.assertEqual(body["fee_policy"]["KR"]["sell"]["total_pct"], 0.195)
        self.assertEqual(body["fee_policy"]["US"]["buy"]["total_pct"], 0.15)

    def test_run_result_exposes_fee_policy_and_trade_fees(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "AAPL", "amount_krw": 1_000_000, "reason": "x"}]})
        result = paper_trading.run(markets="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": [us("AAPL", 200)]}),
                                   price_fn=self.prices({"AAPL": 200}), fx_fn=self.fx())
        self.assertEqual(result["fee_policy"]["US"]["buy"]["total_pct"], 0.15)
        self.assertGreater(result["buys"][0]["fee_krw"], 0)

    def test_legacy_sqlite_file_gains_fee_column_without_losing_rows(self):
        """기존 계좌·체결은 그대로 두고 fee_krw 컬럼만 안전하게 더한다."""
        import history_store
        import sqlite3

        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0)
        paper_store.update_account({"cash_krw": 9_300_000})
        paper_store.record_trade({"ticker": "005930.KS", "market": "KR", "side": "BUY",
                                  "shares": 10.0, "price": 70_000.0, "currency": "KRW",
                                  "fx_rate": 1.0, "amount_krw": 700_000.0})
        before_trades = paper_store.recent_trades()
        self.assertEqual(len(before_trades), 1)

        # 컬럼이 없던 예전 스키마로 되돌린다.
        with sqlite3.connect(history_store.get_db_path()) as conn:
            conn.execute("ALTER TABLE paper_trades DROP COLUMN fee_krw")
        paper_store._SQLITE_READY.discard(history_store.get_db_path())

        trades = paper_store.recent_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["ticker"], "005930.KS")
        self.assertEqual(trades[0]["fee_krw"], 0.0)              # 예전 체결은 0
        self.assertAlmostEqual(trades[0]["settle_krw"], 700_000.0, places=2)
        self.assertAlmostEqual(paper_store.get_account()["cash_krw"], 9_300_000, places=2)
        self.assertEqual(len(paper_store.list_positions()), 1)


# ---------------------------------------------------------------------------
# 6.5 PostgreSQL SQL (실서버에서 물린 자리표시자 버그 회귀 방지)
# ---------------------------------------------------------------------------


class PostgresSqlTests(unittest.TestCase):
    """psycopg는 `?`를 모른다 — 모든 문장이 `%s` 자리표시자로 나가야 한다.

    2026-11-24 실서버(Neon)에서 체결 INSERT만 `?`인 채로 나가
    `the query has 0 placeholders but 15 parameters were passed`로 매수가 기록되지 않았다.
    """

    class _Cursor:
        def __init__(self, row=None):
            self._row = row

        def fetchone(self):
            return self._row

        def fetchall(self):
            return []

    class _Conn:
        def __init__(self, row=None):
            self.queries = []
            self._row = row

        def execute(self, query, params=None):
            self.queries.append((query, params))
            return PostgresSqlTests._Cursor(self._row)

    def payload(self):
        return paper_store._trade_payload({
            "ticker": "AAPL", "market": "US", "side": "BUY", "shares": 1.5, "price": 100.0,
            "currency": "USD", "fx_rate": 1300.0, "amount_krw": 195_000.0, "reason": "테스트",
            "ai_source": "quant",
        })

    def test_trade_insert_uses_percent_placeholders(self):
        conn = self._Conn(row={"id": 7})
        trade_id = paper_store._insert_trade(conn, "postgres", self.payload())
        query, params = conn.queries[0]
        self.assertNotIn("?", query)
        self.assertEqual(query.count("%s"), len(params))
        self.assertIn("RETURNING id", query)
        self.assertEqual(trade_id, 7)

    def test_sqlite_keeps_question_marks(self):
        conn = self._Conn()
        paper_store._insert_trade(conn, "sqlite", self.payload())
        query, params = conn.queries[0]
        self.assertEqual(query.count("?"), len(params))
        self.assertNotIn("%s", query)

    def test_select_statements_use_percent_placeholders(self):
        # 조회 경로도 같은 규칙이다(? 를 그대로 보내면 Postgres에서 죽는다).
        query = paper_store._sql("SELECT * FROM paper_positions WHERE ticker = ?", "postgres")
        self.assertEqual(query, "SELECT * FROM paper_positions WHERE ticker = %s")


# ---------------------------------------------------------------------------
# 7. 환율 폴백
# ---------------------------------------------------------------------------


class FxTests(PaperCase):
    def test_fixed_fallback_when_sources_fail(self):
        def boom():
            raise OSError("network down")
        with patch.object(paper_trading, "_fetch_naver_fx", side_effect=OSError("naver")), \
             patch.object(paper_trading, "_fetch_yahoo_fx", side_effect=OSError("yahoo")):
            fx = paper_trading.get_usd_krw(force=True)
        self.assertEqual(fx["source"], "fixed_fallback")
        self.assertEqual(fx["rate"], paper_trading.DEFAULT_USD_KRW)

    def test_naver_first_then_cache(self):
        with patch.object(paper_trading, "_fetch_naver_fx", return_value=1344.5), \
             patch.object(paper_trading, "_fetch_yahoo_fx", side_effect=AssertionError("should not call")):
            first = paper_trading.get_usd_krw(force=True)
            second = paper_trading.get_usd_krw()
        self.assertEqual(first["source"], "naver_fx")
        self.assertEqual(first["rate"], 1344.5)
        self.assertEqual(second["rate"], 1344.5)   # 캐시 재사용

    def test_yahoo_fallback(self):
        with patch.object(paper_trading, "_fetch_naver_fx", side_effect=OSError("naver")), \
             patch.object(paper_trading, "_fetch_yahoo_fx", return_value=1330.0):
            fx = paper_trading.get_usd_krw(force=True)
        self.assertEqual(fx["source"], "yahoo_fx")
        self.assertEqual(fx["rate"], 1330.0)


# ---------------------------------------------------------------------------
# 8. HTTP 표면
# ---------------------------------------------------------------------------


@unittest.skipIf(TestClient is None, "httpx 미설치")
class ApiTests(PaperCase):
    def setUp(self):
        super().setUp()
        import api
        self.api = api
        self.client = TestClient(api.app)

    def test_status_endpoint(self):
        response = self.client.get("/api/paper/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["account"]["initial_cash_krw"], 10_000_000)
        self.assertEqual(body["positions"], [])
        self.assertIn("equity_history", body)
        self.assertEqual(body["storage_backend"], "sqlite")
        self.assertEqual(body["fee_policy"]["KR"]["sell"]["total_pct"], 0.195)
        self.assertEqual(body["fee_policy"]["US"]["buy"]["total_pct"], 0.15)

    def test_configure_endpoint(self):
        response = self.client.post("/api/paper/configure",
                                    json={"initial_cash_krw": 4_000_000, "markets": "US",
                                          "strategy": "momentum", "auto_enabled": False})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"]["account"]["cash_krw"], 4_000_000)
        self.assertEqual(body["status"]["account"]["markets"], "US")
        self.assertFalse(body["status"]["account"]["auto_enabled"])

    def test_configure_rejects_bad_seed(self):
        response = self.client.post("/api/paper/configure", json={"initial_cash_krw": 10})
        self.assertEqual(response.status_code, 400)
        self.assertIn("initial_cash_krw", response.json()["detail"])

    def test_configure_reset(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0)
        response = self.client.post("/api/paper/configure",
                                    json={"reset": True, "initial_cash_krw": 2_000_000})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"]["positions"], [])
        self.assertEqual(body["status"]["account"]["cash_krw"], 2_000_000)

    def test_run_endpoint_uses_quant_fallback(self):
        with patch.object(self.api, "_paper_ai_call", side_effect=RuntimeError("gemini down")), \
             patch.object(paper_trading, "default_candidates", self.candidates(
                 {"US": [us("NVDA", 500, 90)], "KR": [kr("005930.KS", 70_000, 85)]})), \
             patch.object(paper_trading, "default_prices",
                          self.prices({"NVDA": 500, "005930.KS": 70_000})), \
             patch.object(paper_trading, "get_usd_krw", self.fx()):
            response = self.client.post("/api/paper/run", json={"force": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"]["ai_source"], "quant")
        self.assertEqual(len(body["result"]["buys"]), 2)
        self.assertEqual(len(body["status"]["positions"]), 2)
        tickers = {row["ticker"] for row in body["status"]["positions"]}
        self.assertEqual(tickers, {"NVDA", "005930.KS"})

    def test_health_reports_paper_auto(self):
        body = self.client.get("/").json()
        self.assertIn("paper_auto", body)
        self.assertEqual(body["paper_auto"]["kr_run_at"], "15:40")


if __name__ == "__main__":
    unittest.main()
