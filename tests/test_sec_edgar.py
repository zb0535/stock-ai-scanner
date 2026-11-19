"""SEC EDGAR 무료 재무 폴백 파싱·캐시·연동 테스트.

XBRL 원본은 "기간(start~end)" 값 목록이라 TTM 계산이 핵심이다.
아래 fixture는 실제 AAPL(연간 최신), MSFT(분기 최신 + 누적 보정), NVDA(태그 변경)의
공시 모양을 본떠 만들었다.

실행: python -m unittest discover -s tests -v
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sec_edgar  # noqa: E402
import stock_tools  # noqa: E402


# --- fixture 도우미 ----------------------------------------------------------

def dur(start, end, val, form="10-Q", filed="2026-05-01", frame=None):
    """기간(손익) 항목 하나."""
    entry = {
        "start": start, "end": end, "val": val, "accn": "0000-00-000000",
        "fy": 2026, "fp": "Q1", "form": form, "filed": filed,
    }
    if frame:
        entry["frame"] = frame
    return entry


def inst(end, val, filed="2026-05-01", frame=None):
    """시점(재무상태표) 항목 하나."""
    entry = {"end": end, "val": val, "accn": "0000-00-000000",
             "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": filed}
    if frame:
        entry["frame"] = frame
    return entry


def facts(us_gaap: dict, namespace: str = "us-gaap") -> dict:
    """companyfacts 응답 모양으로 감싼다. {태그: {"USD": [...]}} 형태를 받는다."""
    shaped = {}
    for tag, units in us_gaap.items():
        shaped[tag] = {"units": units}
    return {"cik": 320193, "entityName": "Test Inc.", "facts": {namespace: shaped}}


TICKERS_RAW = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": 1046179, "ticker": "TSM", "title": "Taiwan Semiconductor"},
}

# AAPL 모양: 최신 공시가 10-K(연간) → 연간 EPS가 곧 TTM
AAPL_FACTS = facts({
    "EarningsPerShareDiluted": {"USD/shares": [
        dur("2025-09-28", "2025-12-27", 2.84, frame="CY2025Q4"),
        dur("2025-12-28", "2026-03-28", 2.01, frame="CY2026Q1"),
        dur("2026-03-29", "2026-06-27", 2.02, frame="CY2026Q2"),
        dur("2025-09-28", "2026-09-26", 8.98, form="10-K", frame="CY2026"),
    ]},
    "Revenues": {"USD": [
        dur("2025-09-28", "2025-12-27", 124_300_000_000),
        dur("2025-12-28", "2026-03-28", 95_400_000_000),
        dur("2026-03-29", "2026-06-27", 94_000_000_000),
        dur("2025-09-28", "2026-09-26", 478_800_000_000, form="10-K"),
    ]},
    "OperatingIncomeLoss": {"USD": [
        dur("2025-09-28", "2026-09-26", 158_063_000_000, form="10-K"),
    ]},
    "StockholdersEquity": {"USD": [inst("2026-09-26", 112_093_000_000)]},
    "LongTermDebtNoncurrent": {"USD": [inst("2026-09-26", 70_000_000_000)]},
    "LongTermDebtCurrent": {"USD": [inst("2026-09-26", 10_000_000_000)]},
})

# MSFT 모양: 최신이 분기 10-Q이고 4분기 합이 끊겨 있어 누적 보정이 필요하다
MSFT_FACTS = facts({
    "EarningsPerShareDiluted": {"USD/shares": [
        dur("2025-07-01", "2025-09-30", 3.72),
        dur("2025-10-01", "2025-12-31", 5.16),
        dur("2026-01-01", "2026-03-31", 4.27),
        dur("2025-07-01", "2026-06-30", 17.95, form="10-K"),
        dur("2026-07-01", "2026-09-30", 4.92),
    ]},
})


class SecTestBase(unittest.TestCase):
    def setUp(self):
        sec_edgar.clear_cache()

    def patch_http(self, responses: dict):
        """URL -> 응답. 테스트에서 네트워크를 타지 않게 한다."""
        calls = []

        def fake_get(url):
            calls.append(url)
            for key, value in responses.items():
                # COMPANY_FACTS_URL에는 {cik} 자리표시자가 있으므로 앞부분으로 맞춘다.
                prefix = key.split("{")[0]
                if prefix and prefix in url:
                    return value
            raise RuntimeError(f"unexpected url: {url}")

        patcher = patch.object(sec_edgar, "_http_get_json", fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


# --- 티커/CIK ---------------------------------------------------------------

class TickerMapTests(SecTestBase):
    def test_cik_is_zero_padded_to_10_digits(self):
        self.patch_http({sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW})
        self.assertEqual(sec_edgar.get_cik("aapl"), "0000320193")
        self.assertEqual(sec_edgar.get_cik("MSFT"), "0000789019")

    def test_ticker_map_is_cached_for_24h(self):
        calls = self.patch_http({sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW})
        for _ in range(5):
            sec_edgar.get_cik("AAPL")
        self.assertEqual(len(calls), 1)          # 하루 한 번만 받는다

    def test_unknown_ticker_returns_none(self):
        self.patch_http({sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW})
        self.assertIsNone(sec_edgar.get_cik("NOPE"))


# --- TTM EPS & PER ----------------------------------------------------------

class TtmEpsTests(SecTestBase):
    def test_four_contiguous_quarters_are_summed(self):
        f = facts({"EarningsPerShareDiluted": {"USD/shares": [
            dur("2025-01-01", "2025-03-31", 1.0),
            dur("2025-04-01", "2025-06-30", 1.1),
            dur("2025-07-01", "2025-09-30", 1.2),
            dur("2025-10-01", "2025-12-31", 1.3),
        ]}})
        self.assertAlmostEqual(sec_edgar.extract_metrics(f)["eps_ttm"], 4.6, places=6)

    def test_latest_annual_is_the_ttm(self):
        self.assertAlmostEqual(sec_edgar.extract_metrics(AAPL_FACTS)["eps_ttm"], 8.98, places=6)

    def test_cumulative_ytd_adjustment(self):
        """FY26 연간 + Q1 FY27 - Q1 FY26 = 19.15 (4분기 합은 끊겨 있어 실패해야 한다)."""
        self.assertAlmostEqual(sec_edgar.extract_metrics(MSFT_FACTS)["eps_ttm"], 19.15, places=6)

    def test_basic_eps_used_when_diluted_missing(self):
        f = facts({"EarningsPerShareBasic": {"USD/shares": [
            dur("2025-01-01", "2025-12-31", 5.0, form="10-K"),
        ]}})
        self.assertAlmostEqual(sec_edgar.extract_metrics(f)["eps_ttm"], 5.0, places=6)

    def test_duplicate_period_uses_latest_filed_value(self):
        f = facts({"EarningsPerShareDiluted": {"USD/shares": [
            dur("2025-01-01", "2025-12-31", 1.00, form="10-K", filed="2026-02-01"),
            dur("2025-01-01", "2025-12-31", 9.99, form="10-K", filed="2026-03-01"),  # 정정 공시
        ]}})
        self.assertAlmostEqual(sec_edgar.extract_metrics(f)["eps_ttm"], 9.99, places=6)

    def test_per_is_price_over_ttm_eps(self):
        self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: AAPL_FACTS,
        })
        m = sec_edgar.get_financial_metrics("AAPL", 352.45)
        self.assertAlmostEqual(m["trailing_pe"], round(352.45 / 8.98, 2), places=2)

    def test_per_is_none_without_price_or_with_negative_eps(self):
        self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: AAPL_FACTS,
        })
        self.assertIsNone(sec_edgar.get_financial_metrics("AAPL", None)["trailing_pe"])
        self.assertIsNone(sec_edgar.get_financial_metrics("AAPL", 0)["trailing_pe"])

        loss = facts({"EarningsPerShareDiluted": {"USD/shares": [
            dur("2025-01-01", "2025-12-31", -2.0, form="10-K"),
        ]}})
        sec_edgar.clear_cache()
        self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: loss,
        })
        self.assertIsNone(sec_edgar.get_financial_metrics("AAPL", 100)["trailing_pe"])


# --- 매출 성장률 / 영업이익률 / 부채비율 -------------------------------------

class OtherMetricTests(SecTestBase):
    def test_revenue_growth_yoy_quarter(self):
        f = facts({"Revenues": {"USD": [
            dur("2025-04-01", "2025-06-30", 100.0),
            dur("2025-07-01", "2025-09-30", 100.0),
            dur("2026-04-01", "2026-06-30", 120.0),
            dur("2026-07-01", "2026-09-30", 145.0),
        ]}})
        growth = sec_edgar.extract_metrics(f)["revenue_growth"]
        self.assertAlmostEqual(growth, 0.45, places=6)   # 최신 분기(145) vs 전년 동기(100)

    def test_revenue_growth_falls_back_to_annual(self):
        f = facts({"Revenues": {"USD": [
            dur("2024-01-01", "2024-12-31", 100.0, form="10-K"),
            dur("2025-01-01", "2025-12-31", 110.0, form="10-K"),
        ]}})
        self.assertAlmostEqual(sec_edgar.extract_metrics(f)["revenue_growth"], 0.10, places=6)

    def test_uses_newest_tag_series(self):
        """NVDA처럼 최근 매출을 Revenues로만 공시하면 끊긴 옛 태그를 고르면 안 된다."""
        f = facts({
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
                dur("2021-02-01", "2022-01-30", 26_914_000_000, form="10-K"),
            ]},
            "Revenues": {"USD": [
                dur("2025-01-27", "2025-04-27", 44_062_000_000),
                dur("2025-04-28", "2025-07-27", 46_743_000_000),
                dur("2026-04-27", "2026-07-26", 96_221_000_000),
            ]},
            "OperatingIncomeLoss": {"USD": [
                dur("2026-04-27", "2026-07-26", 63_734_000_000),
            ]},
        })
        tag, _ = sec_edgar._pick_tag(f, sec_edgar.REVENUE_TAGS)
        self.assertEqual(tag, "Revenues")

    def test_operating_margin_ttm(self):
        f = facts({
            "Revenues": {"USD": [
                dur("2025-01-01", "2025-03-31", 25.0),
                dur("2025-04-01", "2025-06-30", 25.0),
                dur("2025-07-01", "2025-09-30", 25.0),
                dur("2025-10-01", "2025-12-31", 25.0),
            ]},
            "OperatingIncomeLoss": {"USD": [
                dur("2025-01-01", "2025-03-31", 6.0),
                dur("2025-04-01", "2025-06-30", 7.0),
                dur("2025-07-01", "2025-09-30", 8.0),
                dur("2025-10-01", "2025-12-31", 9.0),
            ]},
        })
        self.assertAlmostEqual(sec_edgar.extract_metrics(f)["operating_margins"], 0.30, places=6)

    def test_operating_margin_none_when_series_misaligned(self):
        f = facts({
            "Revenues": {"USD": [dur("2026-01-01", "2026-06-30", 100.0)]},
            "OperatingIncomeLoss": {"USD": [dur("2024-01-01", "2024-12-31", 30.0, form="10-K")]},
        })
        self.assertIsNone(sec_edgar.extract_metrics(f)["operating_margins"])

    def test_debt_to_equity_matches_yfinance_percentage_scale(self):
        self.assertAlmostEqual(
            sec_edgar.extract_metrics(AAPL_FACTS)["debt_to_equity"],
            (70_000_000_000 + 10_000_000_000) / 112_093_000_000 * 100,
            places=6,
        )

    def test_debt_current_is_not_double_counted(self):
        f = facts({
            "StockholdersEquity": {"USD": [inst("2026-09-30", 100.0)]},
            "LongTermDebtNoncurrent": {"USD": [inst("2026-09-30", 40.0)]},
            "DebtCurrent": {"USD": [inst("2026-09-30", 20.0)]},
            "ShortTermBorrowings": {"USD": [inst("2026-09-30", 20.0)]},  # DebtCurrent에 포함
        })
        self.assertAlmostEqual(sec_edgar.extract_metrics(f)["debt_to_equity"], 60.0, places=6)

    def test_debt_to_equity_none_when_equity_negative(self):
        f = facts({
            "StockholdersEquity": {"USD": [inst("2026-09-30", -50.0)]},
            "LongTermDebtNoncurrent": {"USD": [inst("2026-09-30", 40.0)]},
        })
        self.assertIsNone(sec_edgar.extract_metrics(f)["debt_to_equity"])


# --- 외국 ADR / 실패 허용 ----------------------------------------------------

class GracefulFailureTests(SecTestBase):
    def test_foreign_adr_with_only_ifrs_facts_returns_none(self):
        """TSM은 us-gaap 태그가 없고 ifrs-full만 있다 → 예외 없이 None."""
        f = facts({"Revenue": {"USD": [dur("2025-01-01", "2025-12-31", 1.0)]}}, namespace="ifrs-full")
        self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: f,
        })
        m = sec_edgar.get_financial_metrics("TSM", 362.62)
        self.assertEqual(m, {"trailing_pe": None, "revenue_growth": None,
                             "operating_margins": None, "debt_to_equity": None})

    def test_unknown_ticker_returns_empty_without_http_facts_call(self):
        self.patch_http({sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW})
        m = sec_edgar.get_financial_metrics("NOPE", 10.0)
        self.assertTrue(all(v is None for v in m.values()))

    def test_http_failure_returns_empty_and_is_negatively_cached(self):
        calls = self.patch_http({sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW})
        for _ in range(3):
            m = sec_edgar.get_financial_metrics("AAPL", 100.0)   # companyfacts 요청은 실패
            self.assertTrue(all(v is None for v in m.values()))
        facts_calls = [u for u in calls if "companyfacts" in u]
        self.assertEqual(len(facts_calls), 1, "실패한 CIK는 1시간 기억해야 한다")

    def test_malformed_entries_do_not_raise(self):
        f = facts({"EarningsPerShareDiluted": {"USD/shares": [
            {"end": "2026-01-01"},                       # start/val 없음
            {"start": "x", "end": "y", "val": "z"},       # 날짜/값 이상
            {"start": "2025-01-01", "end": "2025-12-31", "val": float("nan")},
        ]}})
        self.assertIsNone(sec_edgar.extract_metrics(f)["eps_ttm"])


# --- companyfacts 캐시 -----------------------------------------------------

class FactsCacheTests(SecTestBase):
    def test_facts_cached_for_six_hours(self):
        calls = self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: AAPL_FACTS,
        })
        for _ in range(3):
            sec_edgar.get_financial_metrics("AAPL", 100.0)
        self.assertEqual(len([u for u in calls if "companyfacts" in u]), 1)

    def test_extracted_metrics_survive_facts_eviction(self):
        """원본 facts를 LRU로 버려도 6시간 동안은 다시 받지 않는다(메모리 보호)."""
        calls = self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: AAPL_FACTS,
        })
        sec_edgar.get_financial_metrics("AAPL", 100.0)
        sec_edgar._FACTS_CACHE.clear()
        m = sec_edgar.get_financial_metrics("AAPL", 100.0)
        self.assertAlmostEqual(m["trailing_pe"], round(100 / 8.98, 2), places=2)
        self.assertEqual(len([u for u in calls if "companyfacts" in u]), 1)

    def test_expired_metrics_are_refetched(self):
        calls = self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: AAPL_FACTS,
        })
        sec_edgar.get_financial_metrics("AAPL", 100.0)
        cik = sec_edgar.get_cik("AAPL")
        stored_at, ttl, raw = sec_edgar._RAW_METRICS_CACHE[cik]
        sec_edgar._RAW_METRICS_CACHE[cik] = (stored_at - sec_edgar.FACTS_TTL - 1, ttl, raw)
        sec_edgar._FACTS_CACHE.clear()          # 원본 캐시도 만료된 상황
        sec_edgar.get_financial_metrics("AAPL", 100.0)
        self.assertEqual(len([u for u in calls if "companyfacts" in u]), 2)

    def test_facts_lru_is_bounded(self):
        self.patch_http({
            sec_edgar.COMPANY_TICKERS_URL: TICKERS_RAW,
            sec_edgar.COMPANY_FACTS_URL: AAPL_FACTS,
        })
        for cik in ("0000000001", "0000000002", "0000000003", "0000000004"):
            sec_edgar.get_company_facts(cik)
        self.assertLessEqual(len(sec_edgar._FACTS_CACHE), sec_edgar.MAX_FACTS_CACHE)


# --- stock_tools 연동 -------------------------------------------------------

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


SEC_VALUES = {"trailing_pe": 41.0, "revenue_growth": 0.1636,
              "operating_margins": 0.33, "debt_to_equity": 71.47}


class FallbackIntegrationTests(unittest.TestCase):
    """t.info가 죽어도 SEC 값으로 재무 지표가 채워져야 한다(30분 캐시·기존 키 유지)."""

    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        stock_tools._info_fail_streak = 0
        stock_tools._info_cooldown_until = 0.0
        FakeTicker.info_behavior = "raise"
        FakeTicker.info_payload = {}
        for patcher in (
            patch.object(stock_tools.yf, "Ticker", FakeTicker),
            patch.object(stock_tools.sec_edgar, "get_financial_metrics", self._sec),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sec_calls = []

    def _sec(self, ticker, price=None):
        self.sec_calls.append((ticker, price))
        return dict(SEC_VALUES)

    def test_info_failure_filled_from_sec(self):
        m = stock_tools.get_stock_metrics("AAPL")

        self.assertIsNotNone(m["price"])
        self.assertEqual(m["trailing_pe"], 41.0)
        self.assertAlmostEqual(m["revenue_growth"], 0.1636, places=4)
        self.assertTrue(m["financials_available"])
        self.assertNotIn("financials_stale", m)
        self.assertEqual(m["financials_source"], "sec_edgar")
        self.assertIn("SEC EDGAR", m["financials_note"])
        # 이미 받은 현재가를 그대로 넘긴다(추가 시세 호출 없음)
        self.assertEqual(len(self.sec_calls), 1)
        self.assertAlmostEqual(self.sec_calls[0][1], m["price"], places=2)

    def test_sec_values_are_cached_with_metrics(self):
        stock_tools.get_stock_metrics("AAPL")
        stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(len(self.sec_calls), 1)     # 30분 캐시 안에서는 SEC도 다시 안 부른다
        self.assertEqual(len(stock_tools._METRICS_CACHE), 1)

    def test_partial_info_fills_only_missing_fields(self):
        FakeTicker.info_behavior = "ok"
        FakeTicker.info_payload = {"shortName": "Apple Inc.", "trailingPE": 30.5}
        m = stock_tools.get_stock_metrics("AAPL")

        self.assertEqual(m["trailing_pe"], 30.5)     # yfinance 값 우선
        self.assertAlmostEqual(m["operating_margins"], 0.33, places=4)   # SEC가 보완
        self.assertEqual(m["financials_source"], "yfinance")

    def test_complete_info_skips_sec(self):
        FakeTicker.info_behavior = "ok"
        FakeTicker.info_payload = {"shortName": "Apple Inc.", "trailingPE": 30.5,
                                   "forwardPE": 25.0, "revenueGrowth": 0.117,
                                   "operatingMargins": 0.33, "debtToEquity": 84.3}
        stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(self.sec_calls, [])         # 불필요한 SEC 호출 없음

    def test_sec_failure_keeps_old_behaviour(self):
        with patch.object(stock_tools.sec_edgar, "get_financial_metrics",
                          side_effect=RuntimeError("SEC down")):
            m = stock_tools.get_stock_metrics("AAPL")
        self.assertIsNotNone(m["price"])
        self.assertFalse(m["financials_available"])
        self.assertIn("일시 미수신", m["financials_note"])

    def test_sec_fallback_can_be_disabled(self):
        with patch.object(stock_tools, "SEC_FALLBACK_ENABLED", False):
            m = stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(self.sec_calls, [])
        self.assertFalse(m["financials_available"])


if __name__ == "__main__":
    unittest.main()
