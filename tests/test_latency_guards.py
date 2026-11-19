"""응답 지연 방어(전역 회로 차단기·타임아웃·프리 스캔 일봉 재사용) 테스트.

다루는 것
1. **한국 종목은 Yahoo `t.info`를 절대 부르지 않는다** (네이버 금융 직행) — 30~100초 지연의 주범
2. **Yahoo `t.info` 전역 회로 차단기**: 429/401/crumb/타임아웃 2회 연속 → 30분 전역 우회,
   우회 중에는 어떤 종목도 `t.info`를 부르지 않고 곧바로 SEC EDGAR → Finnhub로 간다
3. **빈 응답은 종목 문제일 수 있으므로 3회**까지 봐준다(차단으로 오판하지 않는다)
4. **벽시계 타임아웃**: `t.info`가 멈춰도 요청은 제한 시간 안에 차트 지표로 응답한다
5. **뉴스 가드**: Yahoo 뉴스도 같은 규칙으로 차단·우회되고, 뉴스 때문에 배치가 멈추지 않는다
6. **프리 스캔 일봉 재사용**: `scan_rows`가 받은 6개월 일봉을 `get_history`가 다시 받지 않는다

실행: python -m unittest discover -s tests -v
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import sp500_scanner  # noqa: E402
import stock_tools  # noqa: E402


def make_hist(days: int = 130, end="2026-11-18"):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    close = pd.Series(np.linspace(100.0, 150.0, days), index=idx)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": 1_000_000})


class FakeTicker:
    """yfinance.Ticker 대역 — info/history/news 호출 횟수를 기록하고 실패 방식을 흉내낸다."""

    info_calls: list[str] = []
    history_calls: list[tuple] = []
    news_calls: list[str] = []
    info_behavior = "ok"          # ok | empty | block | other | hang
    info_payload: dict = {}
    hist_payload = None
    news_payload: list = []
    hang_seconds = 3.0

    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        FakeTicker.info_calls.append(self.symbol)
        behavior = FakeTicker.info_behavior
        if behavior == "hang":
            time.sleep(FakeTicker.hang_seconds)
            return dict(FakeTicker.info_payload)
        if behavior == "block":
            raise RuntimeError("429 Client Error: Too Many Requests (crumb)")
        if behavior == "other":
            raise KeyError("unexpected payload")
        if behavior == "empty":
            return {}
        return dict(FakeTicker.info_payload)

    def history(self, period="6mo"):
        FakeTicker.history_calls.append((self.symbol, period))
        hist = FakeTicker.hist_payload
        return make_hist() if hist is None else hist

    @property
    def news(self):
        FakeTicker.news_calls.append(self.symbol)
        return FakeTicker.news_payload


class BaseCase(unittest.TestCase):
    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        stock_tools.reset_info_breaker()
        stock_tools.reset_news_breaker()
        FakeTicker.info_calls = []
        FakeTicker.history_calls = []
        FakeTicker.news_calls = []
        FakeTicker.info_behavior = "ok"
        FakeTicker.info_payload = {
            "shortName": "Apple Inc.", "trailingPE": 30.5, "forwardPE": 25.0,
            "revenueGrowth": 0.117, "operatingMargins": 0.33, "debtToEquity": 84.3,
        }
        FakeTicker.hist_payload = None
        FakeTicker.news_payload = [{"title": "Apple beats earnings"}]
        self.patcher = patch.object(stock_tools.yf, "Ticker", FakeTicker)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # SEC EDGAR·Finnhub는 네트워크를 타므로 대역으로 바꾼다(호출 여부만 본다).
        self.sec_calls = []
        sec_patcher = patch.object(stock_tools.sec_edgar, "get_financial_metrics",
                                   side_effect=lambda *a, **k: self.sec_calls.append(a[0]) or {})
        sec_patcher.start()
        self.addCleanup(sec_patcher.stop)
        self.finnhub_calls = []
        fh_patcher = patch.object(stock_tools.finnhub_client, "get_financial_metrics",
                                  side_effect=lambda *a, **k: self.finnhub_calls.append(a[0]) or {})
        fh_patcher.start()
        self.addCleanup(fh_patcher.stop)
        fh_enabled = patch.object(stock_tools.finnhub_client, "is_enabled", return_value=True)
        fh_enabled.start()
        self.addCleanup(fh_enabled.stop)


class KoreanSkipsYahooInfoTests(BaseCase):
    """한국 종목(.KS/.KQ)은 Yahoo t.info를 아예 부르지 않는다 — 네이버 금융 직행."""

    def setUp(self):
        super().setUp()
        self.naver_calls = []
        naver = patch.object(stock_tools.naver_finance, "get_fundamentals",
                             side_effect=lambda sym: self.naver_calls.append(sym) or {
                                 "name": "삼성전자", "trailing_pe": 30.07, "forward_pe": 4.33,
                                 "revenue_growth": 0.1088, "operating_margins": 0.1307,
                                 "debt_to_equity": 29.94})
        self.naver = naver.start()
        self.addCleanup(naver.stop)

    def test_kospl_ticker_never_touches_yahoo_info(self):
        m = stock_tools.get_stock_metrics("005930.KS")
        self.assertEqual(FakeTicker.info_calls, [])
        self.assertEqual(self.naver_calls, ["005930.KS"])
        self.assertEqual(m["currency"], "KRW")
        self.assertEqual(m["name"], "삼성전자")
        self.assertEqual(m["financials_source"], "naver")
        self.assertIsNotNone(m["price"])

    def test_kosdaq_ticker_never_touches_yahoo_info(self):
        stock_tools.get_stock_metrics("086520.KQ")
        self.assertEqual(FakeTicker.info_calls, [])

    def test_korean_batch_does_not_trip_info_breaker(self):
        """한국 종목을 아무리 많이 돌려도 Yahoo 가드가 '실패'를 쌓지 않는다."""
        for sym in ("005930.KS", "000660.KS", "035420.KS"):
            stock_tools.get_stock_metrics(sym)
        status = stock_tools.info_breaker_status()
        self.assertFalse(status["blocked"])
        self.assertEqual(status["fail_streak"], 0)

    def test_us_ticker_still_uses_yahoo_info(self):
        stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(FakeTicker.info_calls, ["AAPL"])


class InfoCircuitBreakerTests(BaseCase):
    """429/401/crumb/타임아웃 2회 → 전역 30분 우회(종목별이 아니라 전역)."""

    def test_two_block_failures_open_global_breaker(self):
        FakeTicker.info_behavior = "block"
        stock_tools.get_stock_metrics("AAPL")      # 1회차 실패
        self.assertFalse(stock_tools.info_breaker_status()["blocked"])
        stock_tools.get_stock_metrics("MSFT")      # 2회차 실패 → 전역 차단
        status = stock_tools.info_breaker_status()
        self.assertTrue(status["blocked"])
        self.assertEqual(status["cooldown_sec"], stock_tools._INFO_COOLDOWN_SECONDS)
        self.assertGreater(status["remaining_sec"], 0)
        self.assertIn("429", status["last_error"])

    def test_breaker_is_global_not_per_ticker(self):
        FakeTicker.info_behavior = "block"
        stock_tools.get_stock_metrics("AAPL")
        stock_tools.get_stock_metrics("MSFT")
        before = len(FakeTicker.info_calls)
        # 전혀 다른 종목도 t.info를 부르지 않는다(종목별 쿨다운이면 여기서 또 부른다).
        for sym in ("NVDA", "TSLA", "GOOGL"):
            stock_tools.get_stock_metrics(sym)
        self.assertEqual(len(FakeTicker.info_calls), before)

    def test_cooldown_is_30_minutes(self):
        self.assertEqual(stock_tools._INFO_COOLDOWN_SECONDS, 1800)
        self.assertEqual(stock_tools._INFO_FAIL_THRESHOLD, 2)

    def test_breaker_falls_through_to_sec_edgar_immediately(self):
        """우회 중에는 곧바로 SEC EDGAR(→Finnhub)로 간다 — 재시도 대기 0."""
        FakeTicker.info_behavior = "block"
        stock_tools.get_stock_metrics("AAPL")
        stock_tools.get_stock_metrics("MSFT")
        self.sec_calls.clear()
        started = time.time()
        m = stock_tools.get_stock_metrics("NVDA")
        elapsed = time.time() - started
        self.assertLess(elapsed, 1.0)              # t.info 대기 없이 즉시
        self.assertEqual(self.sec_calls, ["NVDA"])
        self.assertIsNotNone(m["price"])

    def test_breaker_expires_after_cooldown(self):
        FakeTicker.info_behavior = "block"
        stock_tools.get_stock_metrics("AAPL")
        stock_tools.get_stock_metrics("MSFT")
        self.assertTrue(stock_tools.info_breaker_status()["blocked"])
        # 쿨다운이 지난 것으로 시간을 돌린다
        stock_tools._info_cooldown_until = time.time() - 1
        FakeTicker.info_behavior = "ok"
        m = stock_tools.get_stock_metrics("NVDA")
        self.assertIn("NVDA", FakeTicker.info_calls)   # 다시 시도한다
        self.assertAlmostEqual(m["trailing_pe"], 30.5, places=2)

    def test_empty_info_needs_three_failures(self):
        """빈 응답은 그 종목 문제일 수 있으므로 2회로는 전역 차단하지 않는다."""
        FakeTicker.info_behavior = "empty"
        stock_tools.get_stock_metrics("AAPL")
        stock_tools.get_stock_metrics("MSFT")
        self.assertFalse(stock_tools.info_breaker_status()["blocked"])
        stock_tools.get_stock_metrics("NVDA")
        self.assertTrue(stock_tools.info_breaker_status()["blocked"])

    def test_reset_clears_breaker(self):
        FakeTicker.info_behavior = "block"
        stock_tools.get_stock_metrics("AAPL")
        stock_tools.get_stock_metrics("MSFT")
        stock_tools.reset_info_breaker()
        status = stock_tools.info_breaker_status()
        self.assertFalse(status["blocked"])
        self.assertEqual(status["fail_streak"], 0)


class InfoTimeoutTests(BaseCase):
    """`t.info`가 멈춰도 벽시계 타임아웃이 요청을 붙잡지 않는다."""

    def test_hanging_info_times_out_and_call_returns(self):
        with patch.object(stock_tools, "YAHOO_INFO_TIMEOUT", 0.3):
            FakeTicker.info_behavior = "hang"
            FakeTicker.hang_seconds = 1.5
            started = time.time()
            m = stock_tools.get_stock_metrics("AAPL")
            elapsed = time.time() - started
        self.assertLess(elapsed, 1.2)          # 1.5초 멈춘 호출을 기다리지 않는다
        self.assertIsNotNone(m["price"])       # 차트 지표는 그대로 살아 있다
        self.assertEqual(FakeTicker.info_calls, ["AAPL"])   # 한 번만 시도(재시도 없음)
        self.assertEqual(stock_tools.info_breaker_status()["fail_streak"], 1)

    def test_two_timeouts_open_the_breaker(self):
        with patch.object(stock_tools, "YAHOO_INFO_TIMEOUT", 0.05):
            FakeTicker.info_behavior = "hang"
            FakeTicker.hang_seconds = 0.5
            stock_tools.get_stock_metrics("AAPL")
            stock_tools.get_stock_metrics("MSFT")
        self.assertTrue(stock_tools.info_breaker_status()["blocked"])
        self.assertIn("timeout", stock_tools.info_breaker_status()["last_error"])

    def test_call_with_timeout_propagates_result(self):
        self.assertEqual(stock_tools.call_with_timeout(lambda: 7, 1.0), 7)

    def test_call_with_timeout_raises_on_slow_call(self):
        with self.assertRaises(stock_tools.CallTimeout):
            stock_tools.call_with_timeout(lambda: time.sleep(0.5), 0.05)


class NewsGuardTests(BaseCase):
    """Yahoo 뉴스도 차단되면 곧바로 비우고 30분 우회한다(배치를 붙잡지 않는다)."""

    def test_news_failure_is_empty_and_counted(self):
        class Boom(FakeTicker):
            @property
            def news(self):
                FakeTicker.news_calls.append(self.symbol)
                raise RuntimeError("429 Too Many Requests")

        with patch.object(api.yf, "Ticker", Boom):
            self.assertEqual(api._get_recent_news("AAPL"), [])
            self.assertEqual(api._get_recent_news("MSFT"), [])
        self.assertTrue(stock_tools.news_breaker_status()["blocked"])

    def test_news_skipped_during_cooldown(self):
        FakeTicker.news_payload = []
        stock_tools._note_news_failure("block", "429")
        stock_tools._note_news_failure("block", "429")
        self.assertTrue(stock_tools.news_breaker_status()["blocked"])
        api._NEWS_CACHE.clear()
        before = len(FakeTicker.news_calls)
        self.assertEqual(api._get_recent_news("NVDA"), [])
        self.assertEqual(len(FakeTicker.news_calls), before)

    def test_fetch_yahoo_news_parses_titles(self):
        FakeTicker.news_payload = [{"title": "A"}, {"content": {"title": "B"}}, {"id": "x"}]
        self.assertEqual(stock_tools.fetch_yahoo_news("AAPL", limit=5), ["A", "B"])


class HistoryReuseTests(BaseCase):
    """프리 스캔이 이미 받아 둔 6개월 일봉을 그대로 쓴다(재다운로드 0회)."""

    def test_prime_history_is_used_by_get_history(self):
        self.assertTrue(stock_tools.prime_history("AAPL", make_hist(), ttl=600))
        hist = stock_tools.get_history("AAPL", "6mo")
        self.assertEqual(FakeTicker.history_calls, [])
        self.assertEqual(len(hist), 130)

    def test_prime_history_keeps_only_needed_columns(self):
        stock_tools.prime_history("AAPL", make_hist(), ttl=600)
        cached = stock_tools._HISTORY_CACHE["AAPL:6mo"][1]
        self.assertEqual(sorted(cached.columns), ["Close", "High", "Low", "Volume"])

    def test_primed_history_serves_metrics_and_chart(self):
        stock_tools.prime_history("AAPL", make_hist(), ttl=600)
        m = stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(FakeTicker.history_calls, [])   # 지표 계산도 재다운로드 없음
        self.assertIsNotNone(m["ma60"])
        api._attach_chart({"ticker": "AAPL"})
        self.assertEqual(FakeTicker.history_calls, [])   # 차트 부착도 재다운로드 없음

    def test_prime_history_rejects_empty_and_nan_last_row(self):
        self.assertFalse(stock_tools.prime_history("AAPL", pd.DataFrame()))
        nan_last = make_hist()
        nan_last.loc[nan_last.index[-1], ["Open", "High", "Low", "Close"]] = np.nan
        nan_last["Volume"] = 0
        self.assertTrue(stock_tools.prime_history("AAPL", nan_last, ttl=600))
        cached = stock_tools._HISTORY_CACHE["AAPL:6mo"][1]
        self.assertFalse(pd.isna(cached["Close"].iloc[-1]))   # 빈 마지막 행은 걸러진다

    def test_primed_ttl_is_honored(self):
        stock_tools.prime_history("AAPL", make_hist(), ttl=600)
        stock_tools._HISTORY_CACHE["AAPL:6mo"] = (time.time() - 700, *stock_tools._HISTORY_CACHE["AAPL:6mo"][1:])
        stock_tools.get_history("AAPL", "6mo")
        self.assertEqual(len(FakeTicker.history_calls), 1)   # 만료 뒤에는 다시 받는다

    def test_scan_rows_primes_history_cache(self):
        """S&P 500·코스피 200 프리 스캔이 받은 일봉이 캐시에 남는다."""
        frame = make_hist()
        batch = pd.concat({"AAPL": frame, "MSFT": frame}, axis=1)
        batch.columns = pd.MultiIndex.from_product([["AAPL", "MSFT"],
                                                    ["Open", "High", "Low", "Close", "Volume"]])
        with patch.object(sp500_scanner, "_download_batch", return_value=batch):
            result = sp500_scanner.scan_rows(["AAPL", "MSFT"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertIn("AAPL:6mo", stock_tools._HISTORY_CACHE)
        self.assertIn("MSFT:6mo", stock_tools._HISTORY_CACHE)
        entry = stock_tools._HISTORY_CACHE["AAPL:6mo"]
        self.assertEqual(entry[2], sp500_scanner.HISTORY_PRIME_TTL)
        FakeTicker.history_calls = []
        stock_tools.get_history("AAPL", "6mo")
        self.assertEqual(FakeTicker.history_calls, [])


class ScreenGuardIntegrationTests(BaseCase):
    """모의 `/api/screen`: 한국+미국 혼합이 야후 대기 없이 끝나고 timings가 남는다."""

    def test_mixed_screen_reports_timings_and_never_calls_info_for_korean(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("httpx 미설치")
        naver = patch.object(stock_tools.naver_finance, "get_fundamentals",
                             return_value={"name": "삼성전자", "trailing_pe": 30.07,
                                           "revenue_growth": 0.1088, "operating_margins": 0.1307,
                                           "debt_to_equity": 29.94})
        naver.start()
        self.addCleanup(naver.stop)

        class FakeGenai:
            class models:
                @staticmethod
                def generate_content(**kwargs):
                    class R:
                        text = '{"market_note": "ok", "top_picks": []}'
                    return R()

        ai = patch.object(api.genai, "Client", lambda **_: FakeGenai())
        ai.start()
        self.addCleanup(ai.stop)

        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주",
                                 "tickers": ["005930.KS", "AAPL", "NVDA"]})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn("005930.KS", FakeTicker.info_calls)
        self.assertEqual(sorted(body["timings"]),
                         ["charts_ms", "gemini_ms", "rows_ms", "total_ms"])
        self.assertGreaterEqual(body["timings"]["total_ms"], 0)
        for row in body["all_stocks"]:
            self.assertIsNotNone(row.get("price"))


if __name__ == "__main__":
    unittest.main()
