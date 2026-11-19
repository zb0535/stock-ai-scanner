"""캐시 히트, t.info 실패 시 폴백, 429/401 방어 동작을 검증하는 단위 테스트.

실행: python -m unittest discover -s tests -v
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import stock_tools  # noqa: E402


def make_hist(days: int = 130):
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-18"), periods=days)
    close = pd.Series(np.linspace(100.0, 150.0, days), index=idx)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": 1_000_000,
        }
    )


class FakeTicker:
    """yfinance.Ticker 대역. info_behavior에 따라 정상/빈 응답/예외를 흉내낸다."""

    calls = {"info": [], "history": [], "news": []}
    info_behavior = "ok"          # ok | empty | raise
    info_payload: dict = {}
    hist_payload = None           # None이면 make_hist()
    news_payload: list = []

    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        FakeTicker.calls["info"].append(self.symbol)
        if FakeTicker.info_behavior == "raise":
            raise RuntimeError("429 Crumb fetch rate-limited")
        if FakeTicker.info_behavior == "empty":
            return {}
        return dict(FakeTicker.info_payload)

    def history(self, period="6mo"):
        FakeTicker.calls["history"].append((self.symbol, period))
        hist = FakeTicker.hist_payload
        return make_hist() if hist is None else hist

    @property
    def news(self):
        FakeTicker.calls["news"].append(self.symbol)
        return FakeTicker.news_payload


class BaseCase(unittest.TestCase):
    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        stock_tools._info_fail_streak = 0
        stock_tools._info_cooldown_until = 0.0
        FakeTicker.calls = {"info": [], "history": [], "news": []}
        FakeTicker.info_behavior = "ok"
        FakeTicker.info_payload = {
            "shortName": "Apple Inc.",
            "trailingPE": np.float32(30.5),
            "forwardPE": 25.0,
            "revenueGrowth": 0.117,
            "operatingMargins": 0.33,
            "debtToEquity": 84.3,
        }
        FakeTicker.hist_payload = None
        FakeTicker.news_payload = [
            {"title": "Apple beats earnings"},
            {"content": {"title": "Apple unveils new chip"}},
            {"id": "x", "content": None},
        ]
        self.patcher = patch.object(stock_tools.yf, "Ticker", FakeTicker)
        self.patcher.start()
        # SEC EDGAR 폴백은 네트워크를 타므로 이 테스트들에서는 끈다.
        # (폴백 자체는 tests/test_sec_edgar.py에서 검증한다.)
        self.sec_patcher = patch.object(stock_tools.sec_edgar, "get_financial_metrics", return_value={})
        self.sec_patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.sec_patcher.stop()


class MetricsCacheTests(BaseCase):
    def test_second_call_uses_cache(self):
        first = stock_tools.get_stock_metrics("aapl")
        second = stock_tools.get_stock_metrics("AAPL")

        self.assertEqual(first["price"], second["price"])
        self.assertEqual(len(FakeTicker.calls["info"]), 1)
        self.assertEqual(len(FakeTicker.calls["history"]), 1)
        self.assertTrue(first["financials_available"])
        self.assertAlmostEqual(first["trailing_pe"], 30.5, places=2)

    def test_cache_copy_is_isolated(self):
        first = stock_tools.get_stock_metrics("AAPL")
        first["news"] = ["mutated"]          # api.py가 하는 것과 같은 변경
        second = stock_tools.get_stock_metrics("AAPL")
        self.assertNotIn("news", second)

    def test_partial_result_cached_only_60s(self):
        FakeTicker.info_behavior = "empty"
        stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(len(FakeTicker.calls["info"]), 1)

        # 30초 뒤에는 캐시 히트(재호출 없음)
        stock_tools._METRICS_CACHE["AAPL"] = (time.time() - 30, stock_tools._METRICS_CACHE["AAPL"][1])
        stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(len(FakeTicker.calls["info"]), 1)

        # 61초 뒤에는 TTL 만료 → 재조회
        stock_tools._METRICS_CACHE["AAPL"] = (time.time() - 61, stock_tools._METRICS_CACHE["AAPL"][1])
        stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(len(FakeTicker.calls["info"]), 2)

    def test_full_result_cached_30min(self):
        stock_tools.get_stock_metrics("AAPL")
        stock_tools._METRICS_CACHE["AAPL"] = (time.time() - 1700, stock_tools._METRICS_CACHE["AAPL"][1])
        stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(len(FakeTicker.calls["info"]), 1)
        self.assertEqual(len(FakeTicker.calls["history"]), 1)


class InfoFallbackTests(BaseCase):
    def test_info_empty_without_cache_keeps_chart_data(self):
        FakeTicker.info_behavior = "empty"
        m = stock_tools.get_stock_metrics("AAPL")

        self.assertIsNotNone(m["price"])
        self.assertIsNotNone(m["ma20"])
        self.assertIsNotNone(m["rsi14"])
        self.assertEqual(m["trailing_pe"], None)
        self.assertEqual(m["forward_pe"], None)
        self.assertFalse(m["financials_available"])
        self.assertIn("일시 미수신", m["financials_note"])
        self.assertNotIn("financials_stale", m)
        json.dumps(m)  # JSON 직렬화 가능해야 함

    def test_info_exception_without_cache_does_not_raise(self):
        FakeTicker.info_behavior = "raise"
        m = stock_tools.get_stock_metrics("NVDA")   # 예외가 밖으로 나가면 500
        self.assertIsNotNone(m["price"])
        self.assertIsNone(m["operating_margins"])
        self.assertFalse(m["financials_available"])

    def test_info_exception_with_cache_keeps_previous_financials(self):
        good = stock_tools.get_stock_metrics("AAPL")
        self.assertTrue(good["financials_available"])

        # 캐시 만료(30분 경과) 후 t.info가 429로 실패
        stock_tools._METRICS_CACHE["AAPL"] = (time.time() - 1900, stock_tools._METRICS_CACHE["AAPL"][1])
        stock_tools._HISTORY_CACHE["AAPL:6mo"] = (time.time() - 1900, stock_tools._HISTORY_CACHE["AAPL:6mo"][1])
        FakeTicker.info_behavior = "raise"
        m = stock_tools.get_stock_metrics("AAPL")

        self.assertAlmostEqual(m["trailing_pe"], 30.5, places=2)
        self.assertAlmostEqual(m["operating_margins"], 0.33, places=4)
        self.assertEqual(m["name"], "Apple Inc.")
        self.assertTrue(m["financials_stale"])
        self.assertIn("직전 조회값", m["financials_note"])

    def test_missing_fresh_field_keeps_stale_value(self):
        stock_tools.get_stock_metrics("AAPL")
        stock_tools._METRICS_CACHE["AAPL"] = (time.time() - 1900, stock_tools._METRICS_CACHE["AAPL"][1])
        FakeTicker.info_payload = {"shortName": "Apple Inc.", "trailingPE": 41.0}  # PER만 수신
        m = stock_tools.get_stock_metrics("AAPL")
        self.assertAlmostEqual(m["trailing_pe"], 41.0, places=2)
        self.assertAlmostEqual(m["operating_margins"], 0.33, places=4)

    def test_no_price_data_returns_error_payload(self):
        FakeTicker.hist_payload = pd.DataFrame()
        m = stock_tools.get_stock_metrics("ZZZZ")
        self.assertEqual(m["error"], "주가 데이터를 찾을 수 없습니다.")

    def test_consecutive_info_failures_enter_cooldown(self):
        FakeTicker.info_behavior = "raise"
        stock_tools._METRICS_CACHE.clear()
        for i in range(stock_tools._INFO_FAIL_THRESHOLD):
            stock_tools.get_stock_metrics(f"T{i}")
        # 쿨다운 중에는 t.info를 아예 호출하지 않는다
        before = len(FakeTicker.calls["info"])
        m = stock_tools.get_stock_metrics("AFTER")
        self.assertEqual(len(FakeTicker.calls["info"]), before)
        self.assertIsNotNone(m["price"])


class HistoryCacheTests(BaseCase):
    def test_history_cached(self):
        stock_tools.get_history("AAPL", "6mo")
        stock_tools.get_history("aapl", "6mo")
        self.assertEqual(len(FakeTicker.calls["history"]), 1)

    def test_history_empty_falls_back_to_stale(self):
        h1 = stock_tools.get_history("AAPL", "6mo")
        self.assertFalse(h1.empty)
        stock_tools._HISTORY_CACHE["AAPL:6mo"] = (time.time() - 1900, h1)
        FakeTicker.hist_payload = pd.DataFrame()
        h2 = stock_tools.get_history("AAPL", "6mo")
        self.assertEqual(len(h2), len(h1))


class NewsCacheTests(BaseCase):
    def test_news_cached_and_limited(self):
        two = api._get_recent_news("aapl", limit=2)
        three = api._get_recent_news("AAPL", limit=3)

        self.assertEqual(len(FakeTicker.calls["news"]), 1)   # 한 번만 조회
        self.assertEqual(len(two), 2)
        self.assertEqual(len(three), 2)                       # content=None 항목은 제외
        self.assertEqual(two[0], "Apple beats earnings")

    def test_news_failure_returns_empty(self):
        class Boom(FakeTicker):
            @property
            def news(self):
                raise RuntimeError("429 rate limited")

        with patch.object(api.yf, "Ticker", Boom):
            self.assertEqual(api._get_recent_news("AAPL"), [])


if __name__ == "__main__":
    unittest.main()
