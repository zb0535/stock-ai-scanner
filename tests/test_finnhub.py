"""Finnhub 3순위 재무 폴백 파싱·단위 변환·캐시·stock_tools 연동 테스트.

Finnhub는 값을 퍼센트로 주는 항목이 있어(성장률·영업이익률) yfinance 단위(소수 비율)로
변환하는지, 키가 없을 때 조용히 건너뛰는지, SEC가 이미 채운 종목에는 호출하지 않는지를
중심으로 검증한다.

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import finnhub_client  # noqa: E402
import stock_tools  # noqa: E402

EMPTY = {"trailing_pe": None, "forward_pe": None, "revenue_growth": None,
         "operating_margins": None, "debt_to_equity": None}

# TSM 시절 실제 응답 모양을 본떴다(값은 예시).
TSM_PAYLOAD = {
    "symbol": "TSM",
    "metricType": "all",
    "metric": {
        "peTTM": 31.2,
        "peAnnual": 28.9,
        "forwardPE": 22.4,
        "revenueGrowthQuarterlyYoy": 30.5,
        "revenueGrowthTTMYoy": 25.1,
        "operatingMarginTTM": 47.2,
        "operatingMarginAnnual": 46.0,
        "totalDebt/totalEquityQuarterly": 0.246,
        "totalDebt/totalEquityAnnual": 0.254,
    },
}


class FinnhubTestBase(unittest.TestCase):
    def setUp(self):
        finnhub_client.clear_cache()
        finnhub_client._CACHE.clear()
        self.env = patch.dict(os.environ, {"FINNHUB_API_KEY": "test-key", "FINNHUB_FALLBACK": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def patch_http(self, payload=None, error: Exception | None = None):
        calls = []

        def fake_get(url, params):
            calls.append((url, dict(params)))
            if error is not None:
                raise error
            return payload

        patcher = patch.object(finnhub_client, "_http_get_json", fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


# --- 파싱·단위 변환 ----------------------------------------------------------

class ParseTests(FinnhubTestBase):
    def test_tsm_style_payload(self):
        m = finnhub_client.parse_metrics(TSM_PAYLOAD)
        self.assertAlmostEqual(m["trailing_pe"], 31.2, places=6)
        self.assertAlmostEqual(m["forward_pe"], 22.4, places=6)
        # 퍼센트 → 소수 비율(yfinance와 같은 스케일)
        self.assertAlmostEqual(m["revenue_growth"], 0.305, places=6)
        self.assertAlmostEqual(m["operating_margins"], 0.472, places=6)
        # 부채비율은 yfinance처럼 퍼센트로(0.246 비율 → 24.6%)
        self.assertAlmostEqual(m["debt_to_equity"], 24.6, places=6)

    def test_ordered_fallbacks(self):
        payload = {"metric": {
            "peAnnual": 28.9,
            "revenueGrowthTTMYoy": 25.1,
            "operatingMarginAnnual": 46.0,
            "totalDebt/totalEquityAnnual": 0.254,
        }}
        m = finnhub_client.parse_metrics(payload)
        self.assertAlmostEqual(m["trailing_pe"], 28.9, places=6)
        self.assertAlmostEqual(m["revenue_growth"], 0.251, places=6)
        self.assertAlmostEqual(m["operating_margins"], 0.46, places=6)
        self.assertAlmostEqual(m["debt_to_equity"], 25.4, places=6)

    def test_primary_fields_win_over_fallbacks(self):
        m = finnhub_client.parse_metrics(TSM_PAYLOAD)
        self.assertAlmostEqual(m["trailing_pe"], 31.2, places=6)    # peTTM > peAnnual
        self.assertAlmostEqual(m["revenue_growth"], 0.305, places=6)
        self.assertAlmostEqual(m["operating_margins"], 0.472, places=6)
        self.assertAlmostEqual(m["debt_to_equity"], 24.6, places=6)

    def test_string_numbers_and_blanks(self):
        payload = {"metric": {
            "peTTM": "31.2",
            "forwardPE": "",
            "revenueGrowthQuarterlyYoy": " 30.5 ",
            "operatingMarginTTM": "N/A",
            "totalDebt/totalEquityQuarterly": "-",
        }}
        m = finnhub_client.parse_metrics(payload)
        self.assertAlmostEqual(m["trailing_pe"], 31.2, places=6)
        self.assertIsNone(m["forward_pe"])
        self.assertAlmostEqual(m["revenue_growth"], 0.305, places=6)
        self.assertIsNone(m["operating_margins"])
        self.assertIsNone(m["debt_to_equity"])

    def test_nan_and_bool_are_none(self):
        payload = {"metric": {"peTTM": float("nan"), "forwardPE": True,
                              "operatingMarginTTM": float("inf")}}
        m = finnhub_client.parse_metrics(payload)
        self.assertIsNone(m["trailing_pe"])
        self.assertIsNone(m["forward_pe"])
        self.assertIsNone(m["operating_margins"])

    def test_missing_or_malformed_payload(self):
        for payload in (None, {}, {"metric": {}}, {"metric": None}, [], "x"):
            self.assertEqual(finnhub_client.parse_metrics(payload), EMPTY)


# --- 키·요청·캐시 ------------------------------------------------------------

class RequestCacheTests(FinnhubTestBase):
    def test_missing_api_key_skips_without_http(self):
        calls = self.patch_http(TSM_PAYLOAD)
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "", "FINNHUB_FALLBACK": "1"}):
            self.assertFalse(finnhub_client.is_enabled())
            self.assertEqual(finnhub_client.get_financial_metrics("TSM"), EMPTY)
        self.assertEqual(calls, [])

    def test_whitespace_api_key_treated_as_missing(self):
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "   "}):
            self.assertFalse(finnhub_client.is_enabled())

    def test_request_uses_symbol_metric_and_token(self):
        calls = self.patch_http(TSM_PAYLOAD)
        finnhub_client.get_financial_metrics(" tsm ")
        self.assertEqual(len(calls), 1)
        url, params = calls[0]
        self.assertEqual(url, finnhub_client.BASE_URL)
        self.assertEqual(params, {"symbol": "TSM", "metric": "all", "token": "test-key"})

    def test_response_cached_for_six_hours(self):
        calls = self.patch_http(TSM_PAYLOAD)
        for _ in range(3):
            finnhub_client.get_financial_metrics("TSM")
        self.assertEqual(len(calls), 1)

    def test_expired_cache_is_refetched(self):
        calls = self.patch_http(TSM_PAYLOAD)
        finnhub_client.get_financial_metrics("TSM")
        stored_at, ttl, metrics = finnhub_client._CACHE["TSM"]
        finnhub_client._CACHE["TSM"] = (stored_at - finnhub_client.METRICS_TTL - 1, ttl, metrics)
        finnhub_client.get_financial_metrics("TSM")
        self.assertEqual(len(calls), 2)

    def test_http_error_is_negatively_cached_for_15_minutes(self):
        calls = self.patch_http(error=RuntimeError("429 Too Many Requests"))
        for _ in range(3):
            self.assertEqual(finnhub_client.get_financial_metrics("TSM"), EMPTY)
        self.assertEqual(len(calls), 1)
        stored_at, ttl, metrics = finnhub_client._CACHE["TSM"]
        self.assertEqual(ttl, finnhub_client.ERROR_TTL)
        self.assertIsNone(metrics)

    def test_error_cache_expires_after_15_minutes(self):
        calls = self.patch_http(error=RuntimeError("boom"))
        finnhub_client.get_financial_metrics("TSM")
        stored_at, ttl, metrics = finnhub_client._CACHE["TSM"]
        finnhub_client._CACHE["TSM"] = (stored_at - ttl - 1, ttl, metrics)
        finnhub_client.get_financial_metrics("TSM")
        self.assertEqual(len(calls), 2)

    def test_empty_result_cached_only_15_minutes(self):
        calls = self.patch_http({"symbol": "NOPE", "metric": {}})
        finnhub_client.get_financial_metrics("NOPE")
        self.assertEqual(len(calls), 1)
        self.assertEqual(finnhub_client._CACHE["NOPE"][1], finnhub_client.ERROR_TTL)

    def test_cache_returns_copy(self):
        self.patch_http(TSM_PAYLOAD)
        first = finnhub_client.get_financial_metrics("TSM")
        first["trailing_pe"] = 999.0
        second = finnhub_client.get_financial_metrics("TSM")
        self.assertAlmostEqual(second["trailing_pe"], 31.2, places=6)


class HttpTests(FinnhubTestBase):
    def test_http_error_carries_status_only_without_token(self):
        class Resp:
            status_code = 401
            text = '{"error":"Please verify your email"}'

        with patch.object(finnhub_client.requests, "get", return_value=Resp()):
            with self.assertRaises(finnhub_client.FinnhubHTTPError) as ctx:
                finnhub_client._http_get_json(
                    finnhub_client.BASE_URL,
                    {"symbol": "TSM", "metric": "all", "token": "SECRET-KEY-123"},
                )
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn("SECRET-KEY-123", str(ctx.exception))


class StatusTests(FinnhubTestBase):
    def test_status_reports_configuration_without_secret(self):
        st = finnhub_client.status()
        self.assertTrue(st["configured"])
        self.assertEqual(st["key_len"], len("test-key"))
        self.assertTrue(st["enabled"])
        self.assertNotIn("test-key", json.dumps(st))

    def test_status_without_key(self):
        with patch.dict(os.environ, {"FINNHUB_API_KEY": ""}):
            st = finnhub_client.status()
        self.assertFalse(st["configured"])
        self.assertFalse(st["enabled"])
        self.assertEqual(st["env_name"], "")

    def test_alias_env_name_is_accepted_and_reported(self):
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "", "FINNHUB_KEY": "alias-key"}):
            st = finnhub_client.status()
            self.assertEqual(st["env_name"], "FINNHUB_KEY")
            self.assertTrue(st["enabled"])
            self.assertEqual(finnhub_client.get_api_key(), "alias-key")
            self.assertFalse(st["env_flags"]["FINNHUB_API_KEY"])
            self.assertTrue(st["env_flags"]["FINNHUB_KEY"])

    def test_finn_api_key_name_is_accepted(self):
        """대시보드에서 FINN_API_KEY로 저장한 경우에도 읽어야 한다."""
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "", "FINN_API_KEY": "finn-key"}):
            st = finnhub_client.status()
            self.assertEqual(st["env_name"], "FINN_API_KEY")
            self.assertEqual(st["key_len"], len("finn-key"))
            self.assertTrue(st["enabled"])

    def test_last_error_redacts_token(self):
        leaky = RuntimeError(
            "Max retries exceeded with url: /api/v1/stock/metric?symbol=TSM&metric=all&token=SECRET123"
        )
        self.patch_http(error=leaky)
        finnhub_client.get_financial_metrics("TSM")
        st = finnhub_client.status()
        self.assertNotIn("SECRET123", st["last_error"])
        self.assertIn("token=***", st["last_error"])

    def test_success_clears_last_error(self):
        self.patch_http(error=RuntimeError("boom"))
        finnhub_client.get_financial_metrics("TSM")
        self.assertNotEqual(finnhub_client.status()["last_error"], "")
        finnhub_client.clear_cache()
        finnhub_client._CACHE.clear()
        with patch.object(finnhub_client, "_http_get_json", lambda url, params: TSM_PAYLOAD):
            finnhub_client.get_financial_metrics("TSM")
        self.assertEqual(finnhub_client.status()["last_error"], "")


# --- stock_tools 3단계 폴백 연동 ---------------------------------------------

def make_hist(days=130):
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-18"), periods=days)
    close = pd.Series(np.linspace(100.0, 150.0, days), index=idx)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": 1_000_000})


class FakeTicker:
    info_behavior = "raise"
    info_payload: dict = {}

    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        if FakeTicker.info_behavior == "raise":
            raise RuntimeError("429 Crumb fetch rate-limited")
        if FakeTicker.info_behavior == "empty":
            return {}
        return dict(FakeTicker.info_payload)

    def history(self, period="6mo"):
        return make_hist()


SEC_ALL = {"trailing_pe": 41.0, "revenue_growth": 0.1636,
           "operating_margins": 0.33, "debt_to_equity": 71.47}
SEC_NONE = {"trailing_pe": None, "revenue_growth": None,
            "operating_margins": None, "debt_to_equity": None}
FINNHUB_ALL = {"trailing_pe": 31.2, "forward_pe": 22.4, "revenue_growth": 0.305,
               "operating_margins": 0.472, "debt_to_equity": 24.6}


class FallbackIntegrationTests(FinnhubTestBase):
    def setUp(self):
        super().setUp()
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        stock_tools._info_fail_streak = 0
        stock_tools._info_cooldown_until = 0.0
        FakeTicker.info_behavior = "raise"
        FakeTicker.info_payload = {}
        self.finnhub_calls = []
        self.sec_value = dict(SEC_NONE)

        for patcher in (
            patch.object(stock_tools.yf, "Ticker", FakeTicker),
            patch.object(stock_tools.sec_edgar, "get_financial_metrics", self._sec),
            patch.object(stock_tools.finnhub_client, "get_financial_metrics", self._finnhub),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _sec(self, ticker, price=None):
        return dict(self.sec_value)

    def _finnhub(self, ticker):
        self.finnhub_calls.append(ticker)
        return dict(FINNHUB_ALL)

    def test_tsm_style_falls_back_to_finnhub(self):
        """Yahoo 실패 + SEC 비어 있음(외국 ADR) → Finnhub가 채운다."""
        m = stock_tools.get_stock_metrics("TSM")

        self.assertEqual(self.finnhub_calls, ["TSM"])
        self.assertAlmostEqual(m["trailing_pe"], 31.2, places=6)
        self.assertAlmostEqual(m["forward_pe"], 22.4, places=6)
        self.assertAlmostEqual(m["revenue_growth"], 0.305, places=6)
        self.assertAlmostEqual(m["operating_margins"], 0.472, places=6)
        self.assertAlmostEqual(m["debt_to_equity"], 24.6, places=6)
        self.assertTrue(m["financials_available"])
        self.assertNotIn("financials_stale", m)
        self.assertEqual(m["financials_source"], "finnhub")
        self.assertIn("Finnhub", m["financials_note"])
        self.assertIsNotNone(m["price"])

    def test_sec_complete_us_stock_does_not_consume_finnhub(self):
        self.sec_value = dict(SEC_ALL)
        m = stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(self.finnhub_calls, [])
        self.assertAlmostEqual(m["trailing_pe"], 41.0, places=6)
        self.assertEqual(m["financials_source"], "sec_edgar")

    def test_complete_yahoo_info_skips_both_fallbacks(self):
        FakeTicker.info_behavior = "ok"
        FakeTicker.info_payload = {"shortName": "TSM", "trailingPE": 30.0, "forwardPE": 22.0,
                                   "revenueGrowth": 0.30, "operatingMargins": 0.47,
                                   "debtToEquity": 24.0}
        m = stock_tools.get_stock_metrics("TSM")
        self.assertEqual(self.finnhub_calls, [])
        self.assertEqual(m["financials_source"], "yfinance")

    def test_partial_yahoo_filled_by_sec_then_finnhub(self):
        """PER만 Yahoo, 영업이익률은 SEC, 나머지는 Finnhub → 출처가 합쳐진다."""
        FakeTicker.info_behavior = "ok"
        FakeTicker.info_payload = {"shortName": "Test", "trailingPE": 30.0}
        self.sec_value = dict(SEC_NONE, operating_margins=0.40)
        m = stock_tools.get_stock_metrics("AAPL")

        self.assertEqual(self.finnhub_calls, ["AAPL"])
        self.assertAlmostEqual(m["trailing_pe"], 30.0, places=6)      # Yahoo 우선
        self.assertAlmostEqual(m["operating_margins"], 0.40, places=6)  # SEC 우선
        self.assertAlmostEqual(m["revenue_growth"], 0.305, places=6)  # Finnhub
        self.assertAlmostEqual(m["debt_to_equity"], 24.6, places=6)
        self.assertEqual(m["financials_source"], "yfinance+sec_edgar+finnhub")

    def test_sec_and_finnhub_combine_when_yahoo_down(self):
        self.sec_value = dict(SEC_NONE, revenue_growth=0.20)
        m = stock_tools.get_stock_metrics("TSM")
        self.assertAlmostEqual(m["revenue_growth"], 0.20, places=6)
        self.assertAlmostEqual(m["trailing_pe"], 31.2, places=6)
        self.assertEqual(m["financials_source"], "sec_edgar+finnhub")
        self.assertIn("SEC EDGAR", m["financials_note"])
        self.assertIn("Finnhub", m["financials_note"])

    def test_finnhub_result_cached_with_metrics(self):
        stock_tools.get_stock_metrics("TSM")
        stock_tools.get_stock_metrics("TSM")
        self.assertEqual(len(self.finnhub_calls), 1)

    def test_finnhub_failure_keeps_previous_behaviour(self):
        def boom(ticker):
            raise RuntimeError("429")

        with patch.object(stock_tools.finnhub_client, "get_financial_metrics", boom):
            m = stock_tools.get_stock_metrics("TSM")
        self.assertIsNotNone(m["price"])
        self.assertFalse(m["financials_available"])
        self.assertIn("일시 미수신", m["financials_note"])

    def test_finnhub_fallback_can_be_disabled(self):
        with patch.object(stock_tools, "FINNHUB_FALLBACK_ENABLED", False):
            m = stock_tools.get_stock_metrics("TSM")
        self.assertEqual(self.finnhub_calls, [])
        self.assertFalse(m["financials_available"])

    def test_no_api_key_skips_finnhub(self):
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "", "FINNHUB_FALLBACK": "1"}):
            m = stock_tools.get_stock_metrics("TSM")
        self.assertEqual(self.finnhub_calls, [])
        self.assertFalse(m["financials_available"])

    def test_all_none_finnhub_response_keeps_stale_marking(self):
        def empty(ticker):
            return dict(EMPTY)

        with patch.object(stock_tools.finnhub_client, "get_financial_metrics", empty):
            m = stock_tools.get_stock_metrics("TSM")
        self.assertEqual(self.finnhub_calls, [])
        self.assertFalse(m["financials_available"])
        json_ok = m["ticker"] == "TSM"
        self.assertTrue(json_ok)


if __name__ == "__main__":
    unittest.main()
