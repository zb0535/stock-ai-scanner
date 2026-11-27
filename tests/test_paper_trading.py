"""AI 모의투자(paper_store + paper_trading + /api/paper/*) 테스트 — 미국·한국 두 계좌.

다루는 것
1. 계좌: 미국(id=1)·한국(id=2) 계좌가 각각 원화 ₩1,000,000 시드로 따로 생긴다
2. 완전 분리: 현금·보유·체결·수익률이 `account_id`로 갈라져 한쪽 매매가 다른 쪽을 건드리지 않는다
3. 현금 한도: AI가 현금보다 큰 금액을 말해도 체결 금액 합계가 그 계좌 현금을 넘지 않는다
4. 수량 단위: 미국은 소수점 4자리, 한국은 1주 정수
5. 손익 계산: 미국 종목은 매수 환율(원가) 대비 현재 환율로 원화 손익이 계산된다(환차 포함)
6. 매도: 손절(-8%)·익절(+25%)이 수수료·세금 반영 순수익률로 판정된다
7. 중복 실행 방지: 같은 날 `force=False`면 그 계좌는 건너뛰고, `force=True`면 다시 돈다
8. 폴백: AI가 실패하면 정량 점수 상위 종목으로 매수한다
9. 자동 실행: 한국 15:40·미국 06:20 KST 이후 **계좌별** 하루 1회
10. HTTP 표면: GET /api/paper/status?market=, POST /api/paper/configure, POST /api/paper/run
11. 거래 비용: 매수 예산에 수수료 포함(현금 음수 금지)·매도 수수료+증권거래세·실현손익 순액
12. DB 마이그레이션: 예전 단일 계좌 SQLite 파일을 계좌 2개 구조로 안전하게 옮긴다

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paper_auth  # noqa: E402
import paper_store  # noqa: E402
import paper_trading  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

KST = timezone(timedelta(hours=9))

SEED = 1_000_000.0


def admin_headers(db_path=None):
    """기본 관리자(user 1) 세션 토큰 헤더.

    모의투자 API는 토큰 없는 요청을 **401**로 막으므로(구버전 호환 폴백 제거) HTTP 표면 테스트도
    관리자 토큰을 실어 보낸다. 기존 계좌 1·2의 주인이 관리자라 값은 예전과 같다.
    """
    user = paper_auth.ensure_admin(db_path)
    token, _ = paper_auth.sign_token(user, db_path)
    return {"X-Paper-User-Token": token}


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
# 1. 두 계좌 — 기본값·설정·초기화
# ---------------------------------------------------------------------------


class AccountTests(PaperCase):
    def test_two_accounts_each_start_with_one_million(self):
        for market, account_id in (("US", 1), ("KR", 2)):
            account = paper_store.get_account(account_id)
            self.assertEqual(account["id"], account_id)
            self.assertEqual(account["market_scope"], market)
            self.assertEqual(account["markets"], market)
            self.assertEqual(account["initial_cash_krw"], SEED)
            self.assertEqual(account["cash_krw"], SEED)
            self.assertEqual(account["strategy"], "balanced")
            self.assertTrue(account["auto_enabled"])

    def test_account_selector_accepts_market_names_and_ids(self):
        self.assertEqual(paper_store.normalize_account_id("US"), 1)
        self.assertEqual(paper_store.normalize_account_id("kr"), 2)
        self.assertEqual(paper_store.normalize_account_id(2), 2)
        self.assertEqual(paper_store.account_market(1), "US")
        self.assertEqual(paper_trading.account_scope("KR"), "KR")
        self.assertEqual(paper_trading.account_id_of("US"), 1)
        with self.assertRaises(ValueError):
            paper_store.normalize_account_id("JP")
        with self.assertRaises(ValueError):
            paper_trading.account_scope("JP")

    def test_init_store_seeds_one_equity_point_per_account(self):
        paper_store.init_store()
        us_rows = paper_store.equity_history(account_id=1)
        kr_rows = paper_store.equity_history(account_id=2)
        self.assertEqual(len(us_rows), 1)
        self.assertEqual(len(kr_rows), 1)
        self.assertEqual(us_rows[0]["total_value_krw"], SEED)
        self.assertEqual(kr_rows[0]["total_value_krw"], SEED)
        self.assertEqual(us_rows[0]["return_pct"], 0.0)
        # 다시 불러도 스냅샷이 늘지 않는다(계좌별 날짜 UNIQUE).
        paper_store.init_store()
        self.assertEqual(len(paper_store.equity_history(account_id=1)), 1)
        self.assertEqual(len(paper_store.equity_history(account_id=2)), 1)

    def test_configure_one_account_leaves_the_other_alone(self):
        out = paper_trading.configure(account="KR", initial_cash_krw=3_000_000,
                                      strategy="value", auto_enabled=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["market"], "KR")
        self.assertEqual(out["account"]["initial_cash_krw"], 3_000_000)
        self.assertEqual(out["account"]["cash_krw"], 3_000_000)
        self.assertEqual(out["account"]["strategy"], "value")
        self.assertFalse(out["account"]["auto_enabled"])

        us_account = paper_store.get_account("US")
        self.assertEqual(us_account["initial_cash_krw"], SEED)
        self.assertEqual(us_account["cash_krw"], SEED)
        self.assertEqual(us_account["strategy"], "balanced")
        self.assertTrue(us_account["auto_enabled"])

    def test_account_market_cannot_be_changed_by_settings(self):
        """계좌의 시장은 계좌가 정한다 — 예전 클라이언트의 `markets` 값은 무시된다."""
        out = paper_trading.configure(account="US", markets="KR")
        self.assertEqual(out["account"]["markets"], "US")
        self.assertEqual(paper_store.get_account("US")["market_scope"], "US")

    def test_configure_validates_ranges(self):
        for kwargs in ({"initial_cash_krw": 1_000},          # 10만 원 미만
                       {"initial_cash_krw": 5_000_000_000},  # 10억 초과
                       {"strategy": "scalp"}):
            with self.assertRaises(ValueError):
                paper_trading.configure(account="US", **kwargs)

    def test_configure_without_reset_moves_cash_by_delta(self):
        paper_trading.configure(account="US", initial_cash_krw=2_000_000)
        account = paper_store.get_account("US")
        self.assertEqual(account["initial_cash_krw"], 2_000_000)
        self.assertEqual(account["cash_krw"], 2_000_000)
        self.assertEqual(paper_store.get_account("KR")["cash_krw"], SEED)

    def test_configure_cannot_push_cash_negative(self):
        # 주식에 돈이 들어간 상태에서 시드머니를 현금보다 크게 줄이면 거절한다.
        paper_store.update_account({"cash_krw": 100_000}, account_id=1)
        with self.assertRaises(ValueError) as ctx:
            paper_trading.configure(account="US",
                                    initial_cash_krw=paper_store.MIN_INITIAL_CASH_KRW)
        self.assertIn("cash_below_zero", str(ctx.exception))

    def test_reset_clears_only_that_account(self):
        paper_store.add_position("AAPL", "US", "USD", 1.5, 100.0, 1300.0, 195_000.0, account_id=1)
        paper_store.record_trade({"ticker": "AAPL", "side": "BUY", "shares": 1.5, "price": 100.0,
                                  "currency": "USD", "fx_rate": 1300.0, "amount_krw": 195_000.0},
                                 account_id=1)
        paper_store.add_position("005930.KS", "KR", "KRW", 3.0, 70_000.0, 1.0, 210_000.0,
                                 account_id=2)
        paper_store.record_trade({"ticker": "005930.KS", "side": "BUY", "shares": 3.0,
                                  "price": 70_000.0, "currency": "KRW", "fx_rate": 1.0,
                                  "amount_krw": 210_000.0}, account_id=2)

        out = paper_trading.configure(account="KR", initial_cash_krw=500_000, reset=True)
        self.assertTrue(out["reset"])
        self.assertEqual(paper_store.list_positions(account_id=2), [])
        self.assertEqual(paper_store.recent_trades(account_id=2), [])
        kr_account = paper_store.get_account("KR")
        self.assertEqual(kr_account["cash_krw"], 500_000)
        self.assertEqual(kr_account["initial_cash_krw"], 500_000)
        self.assertIsNone(kr_account["last_run_date"])
        history = paper_store.equity_history(account_id=2)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_value_krw"], 500_000)

        # 미국 계좌는 그대로 (보유 1종목·체결 1건·현금 100만).
        self.assertEqual(len(paper_store.list_positions(account_id=1)), 1)
        self.assertEqual(len(paper_store.recent_trades(account_id=1)), 1)
        self.assertEqual(paper_store.get_account("US")["cash_krw"], SEED)


# ---------------------------------------------------------------------------
# 2. 완전 분리 — 현금·보유·체결·수익률이 섞이지 않는다
# ---------------------------------------------------------------------------


class AccountIsolationTests(PaperCase):
    def test_us_buy_does_not_touch_kr_account(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "AAPL", "amount_krw": 300_000, "reason": "x"}]})
        result = paper_trading.run(
            market="US", force=True, ai_call=ai,
            candidate_fn=self.candidates({"US": [us("AAPL", 200)]}),
            price_fn=self.prices({"AAPL": 200}), fx_fn=self.fx())
        self.assertEqual(len(result["buys"]), 1)

        kr = paper_store.get_account("KR")
        self.assertEqual(kr["cash_krw"], SEED)
        self.assertEqual(paper_store.list_positions(account_id=2), [])
        self.assertEqual(paper_store.recent_trades(account_id=2), [])
        self.assertEqual(len(paper_store.list_positions(account_id=1)), 1)
        self.assertLess(paper_store.get_account("US")["cash_krw"], SEED)

    def test_kr_buy_does_not_touch_us_account(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "005930.KS", "amount_krw": 300_000, "reason": "x"}]})
        paper_trading.run(market="KR", force=True, ai_call=ai,
                          candidate_fn=self.candidates({"KR": [kr("005930.KS", 71_300)]}),
                          price_fn=self.prices({"005930.KS": 71_300}), fx_fn=self.fx())
        self.assertEqual(paper_store.get_account("US")["cash_krw"], SEED)
        self.assertEqual(paper_store.list_positions(account_id=1), [])
        self.assertEqual(len(paper_store.list_positions(account_id=2)), 1)

    def test_same_ticker_can_be_held_in_both_accounts(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 200.0, 1300.0, 260_000.0, account_id=1)
        paper_store.add_position("AAPL", "US", "USD", 2.0, 210.0, 1300.0, 546_000.0, account_id=2)
        self.assertEqual(paper_store.get_position("AAPL", account_id=1)["shares"], 1.0)
        self.assertEqual(paper_store.get_position("AAPL", account_id=2)["shares"], 2.0)

    def test_same_day_snapshot_is_kept_per_account(self):
        paper_store.init_store()
        today = paper_store.kst_today_str()
        paper_store.record_equity(today, 1_050_000, 1_050_000, 0, 5.0, account_id=1)
        paper_store.record_equity(today, 990_000, 990_000, 0, -1.0, account_id=2)
        us_rows = paper_store.equity_history(account_id=1)
        kr_rows = paper_store.equity_history(account_id=2)
        self.assertEqual(us_rows[-1]["total_value_krw"], 1_050_000)
        self.assertEqual(kr_rows[-1]["total_value_krw"], 990_000)

    def test_trade_ids_and_accounts_are_separate(self):
        paper_store.record_trade({"ticker": "AAPL", "market": "US", "side": "BUY", "shares": 1.0,
                                  "price": 200.0, "currency": "USD", "fx_rate": 1300.0,
                                  "amount_krw": 260_000.0}, account_id=1)
        paper_store.record_trade({"ticker": "005930.KS", "market": "KR", "side": "BUY",
                                  "shares": 1.0, "price": 70_000.0, "currency": "KRW",
                                  "fx_rate": 1.0, "amount_krw": 70_000.0}, account_id=2)
        us_trades = paper_store.recent_trades(account_id=1)
        kr_trades = paper_store.recent_trades(account_id=2)
        self.assertEqual([t["ticker"] for t in us_trades], ["AAPL"])
        self.assertEqual([t["ticker"] for t in kr_trades], ["005930.KS"])
        self.assertEqual(us_trades[0]["account_id"], 1)
        self.assertEqual(kr_trades[0]["account_id"], 2)
        self.assertEqual(paper_store.trade_count(account_id=1), 1)

    def test_status_shows_both_accounts_side_by_side(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "AAPL", "amount_krw": 300_000, "reason": "x"}]})
        paper_trading.run(market="US", force=True, ai_call=ai,
                          candidate_fn=self.candidates({"US": [us("AAPL", 200)]}),
                          price_fn=self.prices({"AAPL": 200}), fx_fn=self.fx())
        body = paper_trading.status(price_fn=self.prices({"AAPL": 200}), fx_fn=self.fx())
        self.assertEqual(body["market"], "US")
        self.assertEqual(body["account"]["market_scope"], "US")
        self.assertEqual(len(body["positions"]), 1)
        self.assertEqual([row["market"] for row in body["comparison"]], ["US", "KR"])
        comparison = {row["market"]: row for row in body["comparison"]}
        self.assertEqual(comparison["US"]["positions_count"], 1)
        self.assertEqual(comparison["KR"]["positions_count"], 0)
        self.assertEqual(comparison["KR"]["cash_krw"], SEED)
        self.assertEqual(comparison["KR"]["total_value_krw"], SEED)
        self.assertEqual(comparison["KR"]["total_return_pct"], 0.0)
        self.assertAlmostEqual(comparison["US"]["total_value_krw"],
                               paper_store.get_account("US")["cash_krw"] + 299_546.0, places=2)
        self.assertIn("KR", body["accounts"])

    def test_kr_status_returns_only_kr_detail(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 200.0, 1300.0, 260_000.0, account_id=1)
        paper_store.add_position("005930.KS", "KR", "KRW", 2.0, 70_000.0, 1.0, 140_000.0,
                                 account_id=2)
        body = paper_trading.status(price_fn=self.prices({"005930.KS": 71_000}),
                                    fx_fn=self.fx(), market="KR")
        self.assertEqual(body["market"], "KR")
        self.assertEqual([row["ticker"] for row in body["positions"]], ["005930.KS"])
        self.assertEqual(body["account"]["cash_krw"], SEED)
        self.assertEqual(body["accounts"]["US"]["positions_count"], 1)


# ---------------------------------------------------------------------------
# 3~5. 매매 실행
# ---------------------------------------------------------------------------


class RunTests(PaperCase):
    def test_buy_respects_cash_limit(self):
        """AI가 현금의 3배를 말해도 체결 합계(수수료 포함)는 현금을 넘지 않는다."""
        ai = self.ai({"market_note": "공격적", "sells": [], "buys": [
            {"ticker": "NVDA", "amount_krw": 20_000_000, "reason": "1위"},
            {"ticker": "AAPL", "amount_krw": 20_000_000, "reason": "2위"},
            {"ticker": "MSFT", "amount_krw": 20_000_000, "reason": "3위"},
        ]})
        with patch.object(paper_trading, "MAX_POSITION_WEIGHT", 0.30):
            result = paper_trading.run(
                market="US", force=True, ai_call=ai,
                candidate_fn=self.candidates({"US": [us("NVDA", 1000), us("AAPL", 200), us("MSFT", 400)]}),
                price_fn=self.prices({"NVDA": 1000, "AAPL": 200, "MSFT": 400}), fx_fn=self.fx())
        spent = sum(trade["settle_krw"] for trade in result["buys"])
        fees = sum(trade["fee_krw"] for trade in result["buys"])
        account = paper_store.get_account("US")
        self.assertEqual(len(result["buys"]), 3)
        self.assertGreater(fees, 0)
        self.assertLessEqual(spent, SEED + 0.01)
        self.assertGreaterEqual(account["cash_krw"], 0)
        self.assertAlmostEqual(account["cash_krw"], SEED - spent, places=2)
        # 한국 계좌는 그대로.
        self.assertEqual(paper_store.get_account("KR")["cash_krw"], SEED)

    def test_position_weight_cap_is_applied(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "AAPL", "amount_krw": 900_000, "reason": "몰빵"}]})
        result = paper_trading.run(market="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": [us("AAPL", 200)]}),
                                   price_fn=self.prices({"AAPL": 200}), fx_fn=self.fx())
        # 총자산 30% 상한 → 30만 원 안쪽(수수료 포함).
        self.assertLessEqual(result["buys"][0]["settle_krw"], SEED * 0.30 + 1)

    def test_us_shares_rounded_to_four_decimals(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "AAPL", "amount_krw": 1_000_000, "reason": "x"}]})
        with patch.object(paper_trading, "MAX_POSITION_WEIGHT", 1.0):
            paper_trading.run(market="US", force=True, ai_call=ai,
                              candidate_fn=self.candidates({"US": [us("AAPL", 211.37)]}),
                              price_fn=self.prices({"AAPL": 211.37}), fx_fn=self.fx(1300))
        position = paper_store.list_positions(account_id=1)[0]
        # 1,000,000은 수수료 포함 예산 → 원금 예산 1,000,000/1.0015 = 998,502.25
        # / (211.37 * 1300) = 3.63384... → 3.6338주(소수점 4자리 버림)
        self.assertEqual(position["shares"], 3.6338)
        self.assertEqual(position["currency"], "USD")
        principal = round(3.6338 * 211.37 * 1300, 2)
        self.assertAlmostEqual(position["cost_krw"], round(principal * 1.0015, 2), places=2)

    def test_kr_shares_are_integer(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "005930.KS", "amount_krw": 1_000_000, "reason": "x"}]})
        with patch.object(paper_trading, "MAX_POSITION_WEIGHT", 1.0):
            paper_trading.run(market="KR", force=True, ai_call=ai,
                              candidate_fn=self.candidates({"KR": [kr("005930.KS", 71_300)]}),
                              price_fn=self.prices({"005930.KS": 71_300}), fx_fn=self.fx())
        position = paper_store.list_positions(account_id=2)[0]
        self.assertEqual(position["shares"], 14.0)          # 1,000,000 / 71,300 = 14.02 → 14주
        self.assertEqual(position["currency"], "KRW")
        self.assertEqual(position["fx_rate"], 1.0)
        # 원가 = 체결원금 998,200 + 매수 수수료 0.015%(149.73)
        self.assertAlmostEqual(position["cost_krw"], 998_349.73, places=2)

    def test_krw_pnl_includes_fx_move(self):
        """달러 가격이 그대로여도 원/달러가 오르면 원화 수익이 난다."""
        ai = self.ai({"sells": [], "buys": [{"ticker": "MSFT", "amount_krw": 999_000, "reason": "x"}]})
        with patch.object(paper_trading, "MAX_POSITION_WEIGHT", 1.0):
            paper_trading.run(market="US", force=True, ai_call=ai,
                              candidate_fn=self.candidates({"US": [us("MSFT", 400)]}),
                              price_fn=self.prices({"MSFT": 400}), fx_fn=self.fx(1300))
        position = paper_store.list_positions(account_id=1)[0]
        cost = position["cost_krw"]

        # 가격 그대로, 환율 1,300 → 1,430 (+10%)
        body = paper_trading.status(price_fn=self.prices({"MSFT": 400}), fx_fn=self.fx(1430))
        row = body["positions"][0]
        value = round(position["shares"] * 400 * 1430, 2)
        self.assertAlmostEqual(row["market_value_krw"], value, places=2)
        self.assertAlmostEqual(row["cost_krw"], cost, places=2)
        self.assertAlmostEqual(row["pnl_pct"], round((value - cost) / cost * 100, 2), places=2)
        self.assertAlmostEqual(row["fx_change_pct"], 10.0, places=2)   # 환차만으로 +10%
        total = value + (SEED - cost)
        self.assertAlmostEqual(body["account"]["total_return_pct"],
                               round((total - SEED) / SEED * 100, 2), places=2)

    def test_stop_loss_sells_position(self):
        paper_store.add_position("AAPL", "US", "USD", 10.0, 200.0, 1300.0, 2_600_000.0,
                                 account_id=1)
        paper_store.update_account({"cash_krw": 7_400_000}, account_id=1)
        result = paper_trading.run(market="US", force=True, ai_call=self.ai({"sells": [], "buys": []}),
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
        self.assertEqual(paper_store.list_positions(account_id=1), [])
        self.assertAlmostEqual(paper_store.get_account("US")["cash_krw"],
                               7_400_000 + 2_336_490, places=2)

    def test_take_profit_rule_sells(self):
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0,
                                 account_id=2)
        paper_store.update_account({"cash_krw": 9_300_000}, account_id=2)
        result = paper_trading.run(market="KR", force=True, ai_call=self.ai({"sells": [], "buys": []}),
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 90_000}), fx_fn=self.fx())
        self.assertEqual(len(result["sells"]), 1)
        # 총대금 900,000 - 한국 매도비용 0.195%(1,755) = 898,245 → 실현 198,245
        self.assertAlmostEqual(result["sells"][0]["fee_krw"], 1_755, places=2)
        self.assertAlmostEqual(result["sells"][0]["realized_pnl_krw"], 198_245, places=2)

    def test_ai_partial_sell_keeps_fraction(self):
        paper_store.add_position("AAPL", "US", "USD", 10.0, 100.0, 1300.0, 1_300_000.0,
                                 account_id=1)
        paper_store.update_account({"cash_krw": 8_700_000}, account_id=1)
        ai = self.ai({"sells": [{"ticker": "AAPL", "fraction": 0.5, "reason": "비중 축소"}],
                      "buys": []})
        result = paper_trading.run(market="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": []}),
                                   price_fn=self.prices({"AAPL": 110}), fx_fn=self.fx())
        self.assertEqual(result["sells"][0]["shares"], 5.0)
        remaining = paper_store.list_positions(account_id=1)[0]
        self.assertEqual(remaining["shares"], 5.0)
        self.assertAlmostEqual(remaining["cost_krw"], 650_000, places=2)

    def test_unknown_tickers_are_ignored(self):
        ai = self.ai({"sells": [{"ticker": "TSLA", "reason": "없는 보유"}], "buys": [
            {"ticker": "ZZZZ", "amount_krw": 1_000_000, "reason": "후보 아님"},
            {"ticker": "AAPL", "amount_krw": 100, "reason": "너무 작음"},
        ]})
        result = paper_trading.run(market="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": [us("AAPL", 100)]}),
                                   price_fn=self.prices({"AAPL": 100}), fx_fn=self.fx())
        self.assertEqual(result["buys"], [])
        self.assertEqual(result["sells"], [])
        reasons = " ".join(item["reason"] for item in result["skipped"])
        self.assertIn("후보 목록에 없는", reasons)
        self.assertIn("최소 주문", reasons)

    def test_quant_fallback_buys_without_ai(self):
        result = paper_trading.run(
            market="US", force=True, ai_call=self.ai("not json at all"),
            candidate_fn=self.candidates({"US": [us("NVDA", 500, 91), us("AAPL", 200, 88)]}),
            price_fn=self.prices({"NVDA": 500, "AAPL": 200}), fx_fn=self.fx())
        self.assertEqual(result["ai_source"], "quant")
        self.assertEqual(len(result["buys"]), 2)
        self.assertIn("정량 점수", result["buys"][0]["reason"])

    def test_run_only_asks_candidates_for_its_own_market(self):
        calls = []

        def candidate_fn(market, criteria, top_n=paper_trading.CANDIDATES_PER_MARKET):
            calls.append(market)
            return [us("AAPL", 100)] if market == "US" else [kr("005930.KS", 70_000)]

        paper_trading.run(market="KR", force=True, ai_call=self.ai({"sells": [], "buys": []}),
                          candidate_fn=candidate_fn, price_fn=self.prices({}), fx_fn=self.fx())
        self.assertEqual(calls, ["KR"])

    def test_run_records_equity_and_last_run_dates_per_account(self):
        paper_trading.run(market="US", force=True, ai_call=self.ai({"sells": [], "buys": []}),
                          candidate_fn=self.candidates({"US": []}), price_fn=self.prices({}),
                          fx_fn=self.fx())
        today = paper_store.kst_today_str()
        us_account = paper_store.get_account("US")
        kr_account = paper_store.get_account("KR")
        self.assertEqual(us_account["last_run_us_date"], today)
        self.assertIsNone(us_account["last_run_kr_date"])
        self.assertIsNone(kr_account["last_run_us_date"])      # 한국 계좌는 손대지 않았다
        self.assertEqual(paper_store.equity_history(account_id=1)[-1]["date"], today)

    def test_duplicate_run_same_day_skipped_unless_forced(self):
        kwargs = dict(market="US", ai_call=self.ai({"sells": [], "buys": []}),
                      candidate_fn=self.candidates({"US": []}), price_fn=self.prices({}),
                      fx_fn=self.fx())
        first = paper_trading.run(force=True, **kwargs)
        self.assertTrue(first["ok"])
        second = paper_trading.run(force=False, **kwargs)
        self.assertEqual(second.get("skipped"), "already_ran_today")
        self.assertIn("미국", second["message"])
        third = paper_trading.run(force=True, **kwargs)
        self.assertTrue(third["ok"])
        self.assertNotEqual(third.get("skipped"), "already_ran_today")
        # 다른 계좌는 오늘 아직 안 돌았다.
        kr = paper_trading.run(market="KR", force=False,
                               ai_call=self.ai({"sells": [], "buys": []}),
                               candidate_fn=self.candidates({"KR": []}),
                               price_fn=self.prices({}), fx_fn=self.fx())
        self.assertNotEqual(kr.get("skipped"), "already_ran_today")

    def test_missing_price_marks_position_at_cost(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0,
                                 account_id=1)
        paper_store.update_account({"cash_krw": 870_000}, account_id=1)
        body = paper_trading.status(price_fn=self.prices({}), fx_fn=self.fx())
        row = body["positions"][0]
        self.assertEqual(row["price_source"], "avg_price_fallback")
        self.assertAlmostEqual(row["market_value_krw"], 130_000, places=2)
        self.assertAlmostEqual(row["pnl_pct"], 0.0, places=2)


# ---------------------------------------------------------------------------
# 6. 자동 실행 시각 — 계좌별
# ---------------------------------------------------------------------------


class AutoScheduleTests(PaperCase):
    def account(self, market="US", auto=True, last_us=None, last_kr=None):
        account_id = paper_trading.account_id_of(market)
        paper_store.update_account({"auto_enabled": 1 if auto else 0,
                                    "last_run_us_date": last_us,
                                    "last_run_kr_date": last_kr}, account_id=account_id)
        return paper_store.get_account(account_id)

    def test_nothing_due_before_us_close(self):
        self.assertEqual(paper_trading.auto_due_markets(
            datetime(2026, 11, 24, 5, 0, tzinfo=KST)), [])

    def test_kr_due_after_1540_only_for_kr_account(self):
        paper_store.init_store()
        paper_store.update_account({"last_run_us_date": "2026-11-24"}, account_id=1)
        due = paper_trading.auto_due_markets(datetime(2026, 11, 24, 15, 41, tzinfo=KST))
        self.assertEqual(due, ["KR"])

    def test_us_due_after_0620_next_morning(self):
        paper_store.init_store()
        due = paper_trading.auto_due_markets(datetime(2026, 11, 25, 6, 21, tzinfo=KST))
        self.assertEqual(due, ["US"])

    def test_both_due_when_neither_ran(self):
        paper_store.init_store()
        due = paper_trading.auto_due_markets(datetime(2026, 11, 25, 16, 0, tzinfo=KST))
        self.assertEqual(due, ["KR", "US"])

    def test_already_ran_today_is_not_due(self):
        paper_store.init_store()
        for aid in (1, 2):
            paper_store.update_account({"last_run_us_date": "2026-11-25",
                                        "last_run_kr_date": "2026-11-25"}, account_id=aid)
        self.assertEqual(paper_trading.auto_due_markets(
            datetime(2026, 11, 25, 16, 0, tzinfo=KST)), [])

    def test_account_toggle_turns_only_that_account_off(self):
        paper_store.init_store()
        self.account(market="KR", auto=False)
        due = paper_trading.auto_due_markets(datetime(2026, 11, 25, 16, 30, tzinfo=KST))
        self.assertNotIn("KR", due)
        self.assertIn("US", due)

    def test_next_auto_info_is_per_account(self):
        self.account(market="US", last_us="2026-11-24")
        info = paper_trading.next_auto_info(paper_store.get_account("US"),
                                            datetime(2026, 11, 24, 7, 0, tzinfo=KST))
        self.assertEqual(info["market"], "US")
        self.assertTrue(info["at"].startswith("2026-11-25T06:20"))
        self.assertIn("미국", info["label"])

    def test_next_auto_says_catch_up_when_due_now(self):
        self.account(market="KR")
        info = paper_trading.next_auto_info(paper_store.get_account("KR"),
                                            datetime(2026, 11, 24, 19, 30, tzinfo=KST))
        self.assertEqual(info["market"], "KR")
        self.assertIn("곧 자동 실행", info["label"])
        self.assertIn("한국", info["label"])

    def test_next_auto_after_all_ran_today(self):
        self.account(market="US", last_us="2026-11-24")
        info = paper_trading.next_auto_info(paper_store.get_account("US"),
                                            datetime(2026, 11, 24, 20, 0, tzinfo=KST))
        self.assertEqual(info["market"], "US")
        self.assertTrue(info["at"].startswith("2026-11-25T06:20"))

    def test_status_reports_schedule_per_account(self):
        body = paper_trading.status(price_fn=self.prices({}), fx_fn=self.fx())
        markets = {item["market"]: item for item in body["auto_schedule"]}
        self.assertEqual(markets["KR"]["at"], "15:40")
        self.assertEqual(markets["US"]["at"], "06:20")
        self.assertTrue(markets["KR"]["enabled"])
        self.assertTrue(markets["US"]["enabled"])
        self.assertEqual(markets["KR"]["account_id"], 2)
        self.assertEqual(body["account"]["markets"], "US")
        self.assertEqual(body["account"]["id"], 1)

    def test_auto_tick_runs_only_the_due_account(self):
        paper_store.init_store()
        calls = []

        def fake_run(market=None, force=False, trigger="manual", **kwargs):
            calls.append((market, force, trigger))
            return {"ok": True, "summary": {}, "buys": [], "sells": []}

        with patch.object(paper_trading, "auto_due_markets",
                          return_value=["KR"]), \
                patch.object(paper_trading, "run", side_effect=fake_run):
            paper_trading._auto_tick()
        self.assertEqual(calls, [("KR", False, "auto")])


# ---------------------------------------------------------------------------
# 7. 동시 실행·현금 원자성 (실서버에서 자동·수동 실행이 겹쳐 초과 매수한 자리)
# ---------------------------------------------------------------------------


class ConcurrencyTests(PaperCase):
    def test_second_run_is_rejected_while_one_is_running(self):
        """자동 실행과 수동 실행이 겹치면 두 번째는 기다리지 않고 건너뛴다."""
        acquired = paper_trading._RUN_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            result = paper_trading.run(market="KR", force=True,
                                       ai_call=self.ai({"buys": []}),
                                       candidate_fn=self.candidates({}), price_fn=self.prices({}),
                                       fx_fn=self.fx())
        finally:
            paper_trading._RUN_LOCK.release()
        self.assertEqual(result.get("skipped"), "already_running")
        self.assertEqual(result["market"], "KR")
        self.assertEqual(paper_store.list_positions(account_id=2), [])

    def test_store_refuses_overdraft_and_leaves_no_position(self):
        with self.assertRaises(ValueError) as ctx:
            paper_store.apply_buy(
                "AAPL", "US", "Apple", "USD", 100.0, 200.0, 1300.0, 26_000_000.0, "과다 매수",
                -26_000_000.0,
                {"ticker": "AAPL", "market": "US", "side": "BUY", "shares": 100.0,
                 "price": 200.0, "currency": "USD", "fx_rate": 1300.0, "amount_krw": 26_000_000.0},
            )
        self.assertIn("cash_below_zero", str(ctx.exception))
        self.assertEqual(paper_store.list_positions(account_id=1), [])
        self.assertEqual(paper_store.recent_trades(account_id=1), [])
        self.assertEqual(paper_store.get_account("US")["cash_krw"], SEED)

    def test_cash_debits_accumulate_across_fills(self):
        for ticker in ("AAPL", "MSFT"):
            paper_store.apply_buy(
                ticker, "US", ticker, "USD", 1.0, 200.0, 1300.0, 260_000.0, "테스트",
                -260_000.0,
                {"ticker": ticker, "market": "US", "side": "BUY", "shares": 1.0, "price": 200.0,
                 "currency": "USD", "fx_rate": 1300.0, "amount_krw": 260_000.0},
            )
        account = paper_store.get_account("US")
        self.assertAlmostEqual(account["cash_krw"], SEED - 520_000, places=2)
        self.assertEqual(len(paper_store.recent_trades(account_id=1)), 2)

    def test_sell_adds_proceeds_to_cash(self):
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0,
                                 account_id=2)
        paper_store.update_account({"cash_krw": 930_000}, account_id=2)
        paper_store.apply_sell(
            "005930.KS", 4.0, 280_000.0, 400_000.0,
            {"ticker": "005930.KS", "market": "KR", "side": "SELL", "shares": 4.0,
             "price": 100_000.0, "currency": "KRW", "fx_rate": 1.0, "amount_krw": 400_000.0,
             "fee_krw": 780.0, "realized_pnl_krw": 119_220.0},
            account_id=2,
        )
        self.assertAlmostEqual(paper_store.get_account("KR")["cash_krw"], 1_330_000, places=2)
        self.assertAlmostEqual(paper_store.list_positions(account_id=2)[0]["shares"], 6.0, places=4)
        self.assertAlmostEqual(paper_store.recent_trades(account_id=2)[0]["fee_krw"], 780.0, places=2)


# ---------------------------------------------------------------------------
# 8. 거래 비용 — 수수료(한국 0.015/0.195% · 미국 0.15%) · 증권거래세 · 환차
# ---------------------------------------------------------------------------


class FeeTests(PaperCase):
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
        ai = self.ai({"sells": [], "buys": [{"ticker": "005930.KS", "amount_krw": 1_000_000,
                                               "reason": "전액"}]})
        with patch.object(paper_trading, "MAX_POSITION_WEIGHT", 1.0):
            result = paper_trading.run(market="KR", force=True, ai_call=ai,
                                       candidate_fn=self.candidates({"KR": [kr("005930.KS", 71_300)]}),
                                       price_fn=self.prices({"005930.KS": 71_300}), fx_fn=self.fx())
        buy = result["buys"][0]
        self.assertAlmostEqual(buy["settle_krw"], buy["amount_krw"] + buy["fee_krw"], places=2)
        self.assertLessEqual(buy["settle_krw"], SEED + 0.01)
        self.assertAlmostEqual(buy["fee_krw"], round(buy["amount_krw"] * 0.00015, 2), places=2)
        account = paper_store.get_account("KR")
        self.assertGreaterEqual(account["cash_krw"], 0)
        self.assertAlmostEqual(account["cash_krw"], SEED - buy["settle_krw"], places=2)
        # 원가(수수료 포함)가 포지션에 그대로 들어간다.
        self.assertAlmostEqual(paper_store.list_positions(account_id=2)[0]["cost_krw"],
                               buy["settle_krw"], places=2)

    def test_kr_sell_deducts_commission_and_tax(self):
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0,
                                 account_id=2)
        paper_store.update_account({"cash_krw": 930_000}, account_id=2)
        ai = self.ai({"sells": [{"ticker": "005930.KS", "fraction": 1.0, "reason": "정리"}], "buys": []})
        result = paper_trading.run(market="KR", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 100_000}), fx_fn=self.fx())
        sell = result["sells"][0]
        self.assertAlmostEqual(sell["amount_krw"], 1_000_000, places=2)      # 총 매도대금
        self.assertAlmostEqual(sell["fee_krw"], 1_950, places=2)             # 0.195%
        self.assertAlmostEqual(sell["settle_krw"], 998_050, places=2)        # 실제 입금
        self.assertAlmostEqual(sell["realized_pnl_krw"], 298_050, places=2)  # 30만 원 - 비용
        self.assertAlmostEqual(paper_store.get_account("KR")["cash_krw"],
                               930_000 + 998_050, places=2)

    def test_us_sell_deducts_commission_and_fx_spread(self):
        paper_store.add_position("AAPL", "US", "USD", 10.0, 100.0, 1300.0, 1_300_000.0,
                                 account_id=1)
        paper_store.update_account({"cash_krw": 870_000}, account_id=1)
        ai = self.ai({"sells": [{"ticker": "AAPL", "fraction": 1.0, "reason": "정리"}], "buys": []})
        result = paper_trading.run(market="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": []}),
                                   price_fn=self.prices({"AAPL": 110}), fx_fn=self.fx(1300))
        sell = result["sells"][0]
        self.assertAlmostEqual(sell["amount_krw"], 1_430_000, places=2)
        self.assertAlmostEqual(sell["fee_krw"], 2_145, places=2)             # 0.15%
        self.assertAlmostEqual(sell["settle_krw"], 1_427_855, places=2)
        self.assertAlmostEqual(sell["realized_pnl_krw"], 127_855, places=2)

    def test_net_pnl_reflects_fx_loss_and_sell_cost(self):
        """달러 가격이 그대로여도 환율이 내리면 순손실 — 수수료까지 더해 진다."""
        paper_store.add_position("MSFT", "US", "USD", 10.0, 100.0, 1300.0, 1_300_000.0,
                                 account_id=1)
        paper_store.update_account({"cash_krw": 870_000}, account_id=1)
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
        self.assertLess(row["net_pnl_krw"], row["pnl_krw"])

    def test_stop_loss_triggers_on_net_pnl_not_gross(self):
        """주가만 보면 -7.9%라 손절 전이지만, 수수료·세금을 빼면 -8%를 넘어 팔린다."""
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0,
                                 account_id=2)
        paper_store.update_account({"cash_krw": 930_000}, account_id=2)
        flat = paper_trading.status(price_fn=self.prices({"005930.KS": 64_500}), fx_fn=self.fx(),
                                    market="KR")
        row = flat["positions"][0]
        self.assertGreater(row["pnl_pct"], -8.0)          # 주가 등락만 보면 아직 -8% 아님
        self.assertLessEqual(row["net_pnl_pct"], -8.0)    # 수수료·세금 반영하면 넘는다

        result = paper_trading.run(market="KR", force=True,
                                   ai_call=self.ai({"sells": [], "buys": []}),
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 64_500}), fx_fn=self.fx())
        self.assertEqual(len(result["sells"]), 1)
        self.assertIn("순손익", result["sells"][0]["reason"])

    def test_take_profit_rule_counts_fees_too(self):
        """+25%에 딱 걸친 종목은 수수료를 빼면 익절 기준에 못 미쳐 팔지 않는다."""
        paper_store.add_position("005930.KS", "KR", "KRW", 10.0, 70_000.0, 1.0, 700_000.0,
                                 account_id=2)
        paper_store.update_account({"cash_krw": 930_000}, account_id=2)
        body = paper_trading.status(price_fn=self.prices({"005930.KS": 87_500}), fx_fn=self.fx(),
                                    market="KR")
        self.assertAlmostEqual(body["positions"][0]["pnl_pct"], 25.0, places=2)
        self.assertLess(body["positions"][0]["net_pnl_pct"], 25.0)

        result = paper_trading.run(market="KR", force=True,
                                   ai_call=self.ai({"sells": [], "buys": []}),
                                   candidate_fn=self.candidates({"KR": []}),
                                   price_fn=self.prices({"005930.KS": 87_500}), fx_fn=self.fx())
        self.assertEqual(result["sells"], [])

    def test_prompt_names_the_account_and_fee_numbers(self):
        paper_store.add_position("MSFT", "US", "USD", 2.0, 100.0, 1300.0, 260_000.0,
                                 account_id=1)
        paper_store.update_account({"cash_krw": 740_000}, account_id=1)
        body = paper_trading.status(price_fn=self.prices({"MSFT": 100}), fx_fn=self.fx(1280))
        prompt = paper_trading.build_prompt(
            paper_store.get_account("US"), ["US"], 740_000, 996_000,
            body["positions"], {"US": [us("MSFT", 100)]},
            {"rate": 1280.0, "source": "test"})
        for key in ('"net_pnl_krw"', '"net_pnl_pct"', '"buy_fx_rate"', '"current_fx_rate"',
                    '"fx_change_pct"', '"sell_fee_krw"', '"price_pnl_pct"'):
            self.assertIn(key, prompt)
        self.assertIn("0.195%", prompt)      # 한국 매도 비용 안내
        self.assertIn("0.15%", prompt)       # 미국 매도 비용 안내
        self.assertIn("환전 스프레드", prompt)
        self.assertIn("미국 계좌", prompt)
        self.assertIn("다른 시장 종목은 사지도 팔지도 마라", prompt)

    def test_status_exposes_fee_policy(self):
        body = paper_trading.status(price_fn=self.prices({}), fx_fn=self.fx())
        self.assertEqual(body["fee_policy"]["KR"]["sell"]["total_pct"], 0.195)
        self.assertEqual(body["fee_policy"]["US"]["buy"]["total_pct"], 0.15)

    def test_run_result_exposes_fee_policy_and_trade_fees(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "AAPL", "amount_krw": 300_000, "reason": "x"}]})
        result = paper_trading.run(market="US", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"US": [us("AAPL", 200)]}),
                                   price_fn=self.prices({"AAPL": 200}), fx_fn=self.fx())
        self.assertEqual(result["fee_policy"]["US"]["buy"]["total_pct"], 0.15)
        self.assertGreater(result["buys"][0]["fee_krw"], 0)

    def test_each_trade_records_its_account(self):
        ai = self.ai({"sells": [], "buys": [{"ticker": "005930.KS", "amount_krw": 300_000, "reason": "x"}]})
        result = paper_trading.run(market="KR", force=True, ai_call=ai,
                                   candidate_fn=self.candidates({"KR": [kr("005930.KS", 71_300)]}),
                                   price_fn=self.prices({"005930.KS": 71_300}), fx_fn=self.fx())
        self.assertEqual(result["buys"][0]["account_id"], 2)
        self.assertEqual(paper_store.recent_trades(account_id=2)[0]["account_id"], 2)


# ---------------------------------------------------------------------------
# 9. DB 마이그레이션 — 예전 단일 계좌(SQLite) → 계좌 2개
# ---------------------------------------------------------------------------


LEGACY_SCHEMA = """
CREATE TABLE paper_account (
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
CREATE TABLE paper_positions (
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
    fee_krw           REAL    NOT NULL DEFAULT 0,
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


class MigrationTests(PaperCase):
    def _legacy_db(self, with_fee=True):
        """예전 단일 계좌 DB 파일을 만든다(account_id 없음)."""
        schema = LEGACY_SCHEMA
        if not with_fee:
            schema = schema.replace("    fee_krw           REAL    NOT NULL DEFAULT 0,\n", "")
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(schema)
            conn.execute("INSERT INTO paper_account (id, initial_cash_krw, cash_krw, markets,"
                         " strategy, auto_enabled, last_run_us_date, created_at, updated_at)"
                         " VALUES (1, 1000000, 700000, 'ALL', 'balanced', 1, '2026-11-26', 'x', 'y')")
            conn.execute("INSERT INTO paper_positions (ticker, market, name, currency, shares,"
                         " avg_price, fx_rate, cost_krw, reason, opened_at)"
                         " VALUES ('AAPL','US','Apple','USD',1.5,200.0,1300.0,390000.0,'기존','x')")
            conn.execute("INSERT INTO paper_trades (run_id, trade_date, traded_at, ticker, market,"
                         " name, side, shares, price, currency, fx_rate, amount_krw,"
                         " realized_pnl_krw, reason, ai_source)"
                         " VALUES ('r1','2026-11-26','2026-11-26T12:00:00+09:00','AAPL','US',"
                         "'Apple','BUY',1.5,200.0,'USD',1300.0,390000.0,NULL,'기존 매수','gemini')"
                         if with_fee else
                         "INSERT INTO paper_trades (run_id, trade_date, traded_at, ticker, market,"
                         " name, side, shares, price, currency, fx_rate, amount_krw,"
                         " realized_pnl_krw, reason, ai_source)"
                         " VALUES ('r1','2026-11-26','2026-11-26T12:00:00+09:00','AAPL','US',"
                         "'Apple','BUY',1.5,200.0,'USD',1300.0,390000.0,NULL,'기존 매수','gemini')")
            conn.execute("INSERT INTO paper_equity_history (date, total_value_krw, cash_krw,"
                         " stock_value_krw, return_pct, recorded_at)"
                         " VALUES ('2026-11-26', 1000000, 700000, 300000, 0.0, 'z')")

    def test_legacy_rows_move_to_the_us_account(self):
        self._legacy_db()
        us = paper_store.get_account("US")
        self.assertEqual(us["cash_krw"], 700_000)
        self.assertEqual(us["market_scope"], "US")
        self.assertEqual(us["markets"], "US")          # 예전 'ALL'은 미국 계좌로 정리
        positions = paper_store.list_positions(account_id=1)
        self.assertEqual([(p["ticker"], p["account_id"]) for p in positions], [("AAPL", 1)])
        trades = paper_store.recent_trades(account_id=1)
        self.assertEqual([(t["ticker"], t["account_id"]) for t in trades], [("AAPL", 1)])
        self.assertEqual(len(paper_store.equity_history(account_id=1)), 1)

    def test_legacy_db_gains_a_fresh_korean_account(self):
        self._legacy_db()
        kr = paper_store.get_account("KR")
        self.assertEqual(kr["id"], 2)
        self.assertEqual(kr["market_scope"], "KR")
        self.assertEqual(kr["initial_cash_krw"], SEED)
        self.assertEqual(kr["cash_krw"], SEED)
        self.assertEqual(paper_store.list_positions(account_id=2), [])
        self.assertEqual(paper_store.recent_trades(account_id=2), [])

    def test_legacy_unique_constraints_are_scoped_per_account(self):
        self._legacy_db()
        # 같은 날짜 스냅샷을 한국 계좌에도 넣을 수 있다(예전에는 date UNIQUE로 막혔다).
        paper_store.record_equity("2026-11-26", 1_000_000, 1_000_000, 0, 0.0, account_id=2)
        self.assertEqual(len(paper_store.equity_history(account_id=2)), 1)
        # 같은 티커를 한국 계좌에도 들 수 있다(예전에는 ticker UNIQUE로 막혔다).
        paper_store.add_position("AAPL", "US", "USD", 1.0, 200.0, 1300.0, 260_000.0, account_id=2)
        self.assertEqual(len(paper_store.list_positions(account_id=2)), 1)
        self.assertEqual(len(paper_store.list_positions(account_id=1)), 1)

    def test_trade_ids_survive_the_migration(self):
        self._legacy_db()
        trades = paper_store.recent_trades(account_id=1)
        self.assertEqual(trades[0]["id"], 1)
        self.assertEqual(trades[0]["fee_krw"], 0.0)

    def test_legacy_db_without_fee_column_is_upgraded(self):
        self._legacy_db(with_fee=False)
        trades = paper_store.recent_trades(account_id=1)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["fee_krw"], 0.0)
        self.assertAlmostEqual(trades[0]["settle_krw"], 390_000.0, places=2)
        self.assertEqual(len(paper_store.list_positions(account_id=1)), 1)

    def test_layout_is_stable_on_second_open(self):
        self._legacy_db()
        paper_store.get_account("US")
        paper_store._SQLITE_READY.discard(paper_store.database_label())
        paper_store.init_store()
        self.assertEqual(len(paper_store.list_positions(account_id=1)), 1)
        self.assertEqual(paper_store.get_account("KR")["cash_krw"], SEED)


# ---------------------------------------------------------------------------
# 10. PostgreSQL SQL (실서버에서 물린 자리표시자 버그 회귀 방지)
# ---------------------------------------------------------------------------


class PostgresSqlTests(unittest.TestCase):
    """psycopg는 `?`를 모른다 — 모든 문장이 `%s` 자리표시자로 나가야 한다."""

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

        def commit(self):
            pass

    def payload(self):
        return paper_store._trade_payload({
            "ticker": "AAPL", "market": "US", "side": "BUY", "shares": 1.5, "price": 100.0,
            "currency": "USD", "fx_rate": 1300.0, "amount_krw": 195_000.0, "reason": "테스트",
            "ai_source": "quant",
        }, account_id=2)

    def test_trade_insert_uses_percent_placeholders(self):
        conn = self._Conn(row={"id": 7})
        trade_id = paper_store._insert_trade(conn, "postgres", self.payload())
        query, params = conn.queries[0]
        self.assertNotIn("?", query)
        self.assertEqual(query.count("%s"), len(params))
        self.assertIn("RETURNING id", query)
        self.assertEqual(trade_id, 7)
        self.assertIn("account_id", query)

    def test_payload_carries_the_account(self):
        payload = self.payload()
        self.assertEqual(payload["account_id"], 2)
        self.assertEqual(payload["ticker"], "AAPL")

    def test_sqlite_keeps_question_marks(self):
        conn = self._Conn()
        paper_store._insert_trade(conn, "sqlite", self.payload())
        query, params = conn.queries[0]
        self.assertEqual(query.count("?"), len(params))
        self.assertNotIn("%s", query)

    def test_select_statements_use_percent_placeholders(self):
        query = paper_store._sql("SELECT * FROM paper_positions WHERE account_id = ? AND ticker = ?",
                                 "postgres")
        self.assertEqual(query, "SELECT * FROM paper_positions WHERE account_id = %s AND ticker = %s")

    def test_pg_schema_migration_runs_on_a_legacy_table(self):
        """계좌 컬럼·유니크 제약 정리가 Postgres에서도 `%s`로 나간다."""
        conn = self._Conn()
        paper_store._pg_scope_accounts(conn)
        for query, _ in conn.queries:
            self.assertNotIn("?", query)
            self.assertIn("market_scope", query)
        conn = self._Conn()
        paper_store._pg_drop_global_unique(conn, paper_store.TABLE_POSITIONS,
                                           ("account_id", "ticker"))
        joined = "\n".join(query for query, _ in conn.queries)
        self.assertIn("pg_constraint", joined)
        self.assertIn("DROP CONSTRAINT", joined)
        self.assertNotIn("?::regclass", joined)


# ---------------------------------------------------------------------------
# 11. 환율 폴백
# ---------------------------------------------------------------------------


class FxTests(PaperCase):
    def test_fixed_fallback_when_sources_fail(self):
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
# 12. HTTP 표면
# ---------------------------------------------------------------------------


@unittest.skipIf(TestClient is None, "httpx 미설치")
class ApiTests(PaperCase):
    def setUp(self):
        super().setUp()
        import api
        self.api = api
        self.client = TestClient(api.app)
        # 보호된 API는 토큰 필수 — 이 클래스의 모든 요청에 관리자 토큰을 기본 헤더로 싣는다.
        self.client.headers.update(admin_headers(self.db_path))

    def test_status_endpoint_defaults_to_us_with_comparison(self):
        response = self.client.get("/api/paper/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["market"], "US")
        self.assertEqual(body["account"]["initial_cash_krw"], SEED)
        self.assertEqual(body["account"]["market_scope"], "US")
        self.assertEqual(body["positions"], [])
        self.assertIn("equity_history", body)
        self.assertEqual(body["storage_backend"], "sqlite")
        self.assertEqual(body["fee_policy"]["KR"]["sell"]["total_pct"], 0.195)
        self.assertEqual(body["fee_policy"]["US"]["buy"]["total_pct"], 0.15)
        comparison = {row["market"]: row for row in body["comparison"]}
        self.assertEqual(comparison["US"]["initial_cash_krw"], SEED)
        self.assertEqual(comparison["KR"]["initial_cash_krw"], SEED)
        self.assertEqual(comparison["KR"]["positions_count"], 0)

    def test_status_endpoint_for_korean_account(self):
        body = self.client.get("/api/paper/status?market=KR").json()
        self.assertEqual(body["market"], "KR")
        self.assertEqual(body["account"]["id"], 2)
        self.assertEqual(body["account"]["markets"], "KR")
        self.assertEqual(body["account"]["cash_krw"], SEED)

    def test_status_rejects_unknown_market(self):
        response = self.client.get("/api/paper/status?market=JP")
        self.assertEqual(response.status_code, 400)

    def test_configure_endpoint_targets_one_account(self):
        response = self.client.post("/api/paper/configure",
                                    json={"market": "KR", "initial_cash_krw": 2_000_000,
                                          "strategy": "momentum", "auto_enabled": False})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["market"], "KR")
        self.assertEqual(body["status"]["account"]["cash_krw"], 2_000_000)
        self.assertEqual(body["status"]["account"]["markets"], "KR")
        self.assertFalse(body["status"]["account"]["auto_enabled"])
        # 미국 계좌는 그대로.
        us = self.client.get("/api/paper/status?market=US").json()["account"]
        self.assertEqual(us["cash_krw"], SEED)
        self.assertTrue(us["auto_enabled"])

    def test_configure_accepts_legacy_markets_field(self):
        """v2.8 앱·웹이 보내던 `markets`는 받아만 두고 무시한다(계좌 시장 고정)."""
        response = self.client.post("/api/paper/configure",
                                    json={"initial_cash_krw": 1_500_000, "markets": "US"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"]["account"]["cash_krw"], 1_500_000)
        self.assertEqual(body["status"]["account"]["markets"], "US")

    def test_configure_rejects_bad_seed(self):
        response = self.client.post("/api/paper/configure", json={"initial_cash_krw": 10})
        self.assertEqual(response.status_code, 400)
        self.assertIn("initial_cash_krw", response.json()["detail"])

    def test_configure_reset_only_that_account(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0, account_id=1)
        paper_store.add_position("005930.KS", "KR", "KRW", 1.0, 70_000.0, 1.0, 70_000.0,
                                 account_id=2)
        response = self.client.post("/api/paper/configure",
                                    json={"market": "US", "reset": True,
                                          "initial_cash_krw": 2_000_000})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"]["positions"], [])
        self.assertEqual(body["status"]["account"]["cash_krw"], 2_000_000)
        kr = self.client.get("/api/paper/status?market=KR").json()
        self.assertEqual(len(kr["positions"]), 1)       # 한국 계좌 보유는 남아 있다

    def test_run_endpoint_uses_quant_fallback_in_one_account(self):
        with patch.object(self.api, "_paper_ai_call", side_effect=RuntimeError("gemini down")), \
             patch.object(paper_trading, "default_candidates", self.candidates(
                 {"US": [us("NVDA", 500, 90)], "KR": [kr("005930.KS", 70_000, 85)]})), \
             patch.object(paper_trading, "default_prices",
                          self.prices({"NVDA": 500, "005930.KS": 70_000})), \
             patch.object(paper_trading, "get_usd_krw", self.fx()):
            response = self.client.post("/api/paper/run", json={"market": "US", "force": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"]["market"], "US")
        self.assertEqual(body["result"]["ai_source"], "quant")
        self.assertEqual(len(body["result"]["buys"]), 1)
        tickers = {row["ticker"] for row in body["status"]["positions"]}
        self.assertEqual(tickers, {"NVDA"})
        # 한국 계좌에는 아무것도 들어가지 않았다.
        self.assertEqual(paper_store.list_positions(account_id=2), [])

    def test_run_endpoint_rejects_unknown_market(self):
        response = self.client.post("/api/paper/run", json={"market": "JP"})
        self.assertEqual(response.status_code, 400)

    def test_health_reports_paper_auto(self):
        body = self.client.get("/").json()
        self.assertIn("paper_auto", body)
        self.assertEqual(body["paper_auto"]["kr_run_at"], "15:40")


if __name__ == "__main__":
    unittest.main()
