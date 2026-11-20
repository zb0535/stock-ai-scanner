"""실시간 현재가(네이버·Finnhub)·오버레이·AI 분석 10분 캐시 테스트.

다루는 것
1. `naver_finance` — polling 한 번으로 여러 코드, 10초 캐시, 실패 시 단건 폴백/폴백 없음
2. `finnhub_client.get_quote` — `c`만 사용, 0·None·429 폴백, 10초 캐시, 분당 상한
3. `realtime_quotes` — 한국/미국 분기, 실패 종목 누락, `REALTIME_PRICE=0` 스위치
4. `stock_tools.apply_realtime_prices` — price 덮어쓰기·price_source·추세 재판정
5. `history_store.get_performance` — 성적표 현재가에 실시간가 우선
6. `history_store.resolve_current_prices` — 진입가·성적표가 같이 쓰는 가격 우선순위
7. `/api/screen` — AI 분석 캐시 히트에서 Gemini 미호출 + 주가는 매번 갱신, 상위 5종목만 뉴스
8. `/api/stock/{ticker}` — 상세 현재가도 실시간

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import finnhub_client  # noqa: E402
import history_store  # noqa: E402
import naver_finance  # noqa: E402
import realtime_quotes  # noqa: E402
import stock_tools  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


def make_hist(days: int = 130, end="2026-11-18"):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    close = pd.Series(np.linspace(100.0, 150.0, days), index=idx)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": 1_000_000})


# ---------------------------------------------------------------------------
# 1. 네이버 금융 실시간 시세
# ---------------------------------------------------------------------------

class NaverRealtimeTests(unittest.TestCase):
    def setUp(self):
        naver_finance.clear_cache()

    def test_batch_uses_one_request_and_parses_prices(self):
        calls = []

        def fake_get_json(url, params=None, timeout=None):
            calls.append((url, timeout))
            return {"datas": [
                {"itemCode": "005930", "closePrice": "183,200", "marketStatus": "OPEN",
                 "localTradedAt": "2026-11-20T15:11:22+09:00"},
                {"itemCode": "000660", "closePrice": "1,108,000", "marketStatus": "CLOSE",
                 "localTradedAt": "2026-11-20T15:11:22+09:00"},
            ]}

        with patch.object(naver_finance, "_get_json", fake_get_json):
            out = naver_finance.get_realtime_prices(["005930.KS", "000660.KS", "005930.KS"])

        self.assertEqual(len(calls), 1)                       # 종목 수가 아니라 요청 1회
        self.assertTrue(calls[0][0].endswith("/005930,000660"))
        self.assertEqual(calls[0][1], naver_finance.REALTIME_TIMEOUT)   # 3초
        self.assertEqual(out["005930.KS"]["price"], 183200.0)
        self.assertEqual(out["005930.KS"]["source"], "naver_realtime")
        self.assertEqual(out["005930.KS"]["market_status"], "OPEN")
        self.assertEqual(out["000660.KS"]["price"], 1108000.0)

    def test_cached_for_ten_seconds(self):
        calls = []

        def fake_get_json(url, params=None, timeout=None):
            calls.append(url)
            return {"datas": [{"itemCode": "005930", "closePrice": "183,000"}]}

        with patch.object(naver_finance, "_get_json", fake_get_json):
            naver_finance.get_realtime_prices(["005930.KS"])
            naver_finance.get_realtime_prices(["005930.KS"])
        self.assertEqual(len(calls), 1)                       # 10초 캐시

    def test_zero_price_is_not_a_quote(self):
        def fake_get_json(url, params=None, timeout=None):
            if "basic" in url:
                return {"closePrice": "0"}
            return {"datas": [{"itemCode": "005930", "closePrice": "0"}]}

        with patch.object(naver_finance, "_get_json", fake_get_json):
            self.assertEqual(naver_finance.get_realtime_prices(["005930.KS"]), {})

    def test_missing_code_falls_back_to_basic(self):
        calls = []

        def fake_get_json(url, params=None, timeout=None):
            calls.append(url)
            if "basic" in url:
                return {"closePrice": "183,000", "marketStatus": "OPEN"}
            # polling이 첫 코드만 준다
            return {"datas": [{"itemCode": "005930", "closePrice": "183,200"}]}

        with patch.object(naver_finance, "_get_json", fake_get_json):
            out = naver_finance.get_realtime_prices(["005930.KS", "000660.KS"])

        self.assertEqual(out["005930.KS"]["price"], 183200.0)
        self.assertEqual(out["000660.KS"]["price"], 183000.0)   # basic 폴백
        self.assertEqual(len(calls), 2)

    def test_polling_failure_does_not_hammer_basic_per_code(self):
        calls = []

        def fake_get_json(url, params=None, timeout=None):
            calls.append(url)
            return None  # 네트워크·차단

        with patch.object(naver_finance, "_get_json", fake_get_json):
            out = naver_finance.get_realtime_prices(["005930.KS", "000660.KS", "035420.KS"])

        self.assertEqual(out, {})
        self.assertEqual(len(calls), 1)   # polling 1회로 끝 — 코드마다 재시도하지 않는다

    def test_us_ticker_is_ignored(self):
        calls = []
        with patch.object(naver_finance, "_get_json",
                          lambda *a, **k: calls.append(a) or {}):
            self.assertIsNone(naver_finance.get_realtime_price("AAPL"))
            self.assertEqual(naver_finance.get_realtime_prices(["AAPL"]), {})
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# 2. Finnhub /quote
# ---------------------------------------------------------------------------

class FinnhubQuoteTests(unittest.TestCase):
    def setUp(self):
        finnhub_client.clear_cache()
        env = patch.dict(os.environ, {"FINNHUB_API_KEY": "test-key", "FINNHUB_FALLBACK": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_parse_quote_uses_current_price_only(self):
        self.assertEqual(finnhub_client.parse_quote({"c": 195.89, "d": 1.2}), 195.89)
        self.assertEqual(finnhub_client.parse_quote({"c": "3.5"}), 3.5)
        self.assertIsNone(finnhub_client.parse_quote({"c": 0}))       # 미지원 티커·장 마감
        self.assertIsNone(finnhub_client.parse_quote({"c": None}))
        self.assertIsNone(finnhub_client.parse_quote({"c": -1}))
        self.assertIsNone(finnhub_client.parse_quote({}))
        self.assertIsNone(finnhub_client.parse_quote(None))

    def test_quote_is_cached_for_ten_seconds(self):
        calls = []

        def fake_http(url, params, timeout=None):
            calls.append((url, dict(params), timeout))
            return {"c": 200.5}

        with patch.object(finnhub_client, "_http_get_json", fake_http):
            self.assertEqual(finnhub_client.get_quote("AAPL"), 200.5)
            self.assertEqual(finnhub_client.get_quote("aapl"), 200.5)

        self.assertEqual(len(calls), 1)
        url, params, timeout = calls[0]
        self.assertEqual(url, finnhub_client.QUOTE_URL)
        self.assertEqual(params["symbol"], "AAPL")
        self.assertEqual(params["token"], "test-key")
        self.assertEqual(timeout, finnhub_client.QUOTE_TIMEOUT)   # 3초

    def test_zero_price_returns_none(self):
        with patch.object(finnhub_client, "_http_get_json", lambda *a, **k: {"c": 0}):
            self.assertIsNone(finnhub_client.get_quote("005930.KS"))

    def test_429_returns_none_and_records_error(self):
        def boom(url, params, timeout=None):
            raise finnhub_client.FinnhubHTTPError(429, "rate limited")

        with patch.object(finnhub_client, "_http_get_json", boom):
            self.assertIsNone(finnhub_client.get_quote("AAPL"))
        status = finnhub_client.status()
        self.assertIn("429", status["last_error"])
        self.assertNotIn("test-key", json.dumps(status))

    def test_no_key_makes_no_request(self):
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "", "FINN_API_KEY": "", "FINNHUB_KEY": "",
                                     "FINNHUB_TOKEN": "", "FINNHUB_API_TOKEN": ""}):
            calls = []
            with patch.object(finnhub_client, "_http_get_json",
                              lambda *a, **k: calls.append(a) or {"c": 1.0}):
                self.assertIsNone(finnhub_client.get_quote("AAPL"))
            self.assertEqual(calls, [])

    def test_get_quotes_merges_and_skips_failures(self):
        def fake_http(url, params, timeout=None):
            if params["symbol"] == "MSFT":
                raise finnhub_client.FinnhubHTTPError(429, "rate limited")
            return {"c": 100.0}

        with patch.object(finnhub_client, "_http_get_json", fake_http):
            out = finnhub_client.get_quotes(["AAPL", "MSFT", "NVDA"], max_workers=3)

        self.assertEqual(out["AAPL"], {"price": 100.0, "source": "finnhub_quote"})
        self.assertEqual(out["NVDA"]["price"], 100.0)
        self.assertNotIn("MSFT", out)

    def test_minute_budget_stops_calls(self):
        calls = []
        with patch.object(finnhub_client, "QUOTE_MAX_PER_MIN", 1), \
                patch.object(finnhub_client, "_http_get_json",
                             lambda *a, **k: calls.append(1) or {"c": 10.0}):
            self.assertEqual(finnhub_client.get_quote("AAPL"), 10.0)
            self.assertIsNone(finnhub_client.get_quote("MSFT"))   # 상한 초과 → 일봉 종가 폴백
        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------------------
# 3. realtime_quotes 오버레이
# ---------------------------------------------------------------------------

class RealtimeOverlayTests(unittest.TestCase):
    def setUp(self):
        realtime_quotes.clear_cache()
        env = patch.dict(os.environ, {"REALTIME_PRICE": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_splits_korean_and_us(self):
        naver_calls, finnhub_calls = [], []

        with patch.object(naver_finance, "get_realtime_prices",
                          lambda tickers: naver_calls.append(list(tickers)) or
                          {t: {"price": 183200.0, "source": "naver_realtime"} for t in tickers}), \
                patch.object(finnhub_client, "get_quotes",
                             lambda tickers, max_workers=8: finnhub_calls.append(list(tickers)) or
                             {"AAPL": {"price": 195.9, "source": "finnhub_quote"}}), \
                patch.object(finnhub_client, "is_enabled", return_value=True):
            out = realtime_quotes.get_prices(["005930.KS", "AAPL", "000660.KS"])

        self.assertEqual(naver_calls, [["005930.KS", "000660.KS"]])
        self.assertEqual(finnhub_calls, [["AAPL"]])
        self.assertEqual(out["AAPL"]["price"], 195.9)
        self.assertEqual(out["000660.KS"]["source"], "naver_realtime")

    def test_failures_are_omitted_and_cached(self):
        calls = []

        def boom(tickers):
            calls.append(list(tickers))
            raise RuntimeError("naver down")

        with patch.object(naver_finance, "get_realtime_prices", boom), \
                patch.object(finnhub_client, "is_enabled", return_value=False):
            self.assertEqual(realtime_quotes.get_prices(["005930.KS"]), {})
            self.assertEqual(realtime_quotes.get_prices(["005930.KS"]), {})
        self.assertEqual(len(calls), 1)   # 실패도 12초 캐시 — 매번 재시도하지 않는다

    def test_disabled_switch_makes_no_calls(self):
        calls = []
        with patch.dict(os.environ, {"REALTIME_PRICE": "0"}), \
                patch.object(naver_finance, "get_realtime_prices",
                             lambda t: calls.append(t) or {}):
            self.assertEqual(realtime_quotes.get_prices(["005930.KS"]), {})
        self.assertEqual(calls, [])

    def test_source_of_defaults_to_daily_close(self):
        self.assertEqual(realtime_quotes.source_of(None), "daily_close")
        self.assertEqual(realtime_quotes.source_of({"price": 1.0}), "daily_close")
        self.assertEqual(realtime_quotes.source_of({"source": "finnhub_quote"}), "finnhub_quote")


# ---------------------------------------------------------------------------
# 4. stock_tools.apply_realtime_prices
# ---------------------------------------------------------------------------

class ApplyRealtimeTests(unittest.TestCase):
    def test_overrides_price_and_recomputes_trend(self):
        rows = [
            {"ticker": "AAPL", "price": 100.0, "ma20": 120.0, "ma60": 130.0, "trend": "횡보/혼조"},
            {"ticker": "005930.KS", "price": 180000.0, "ma20": 170000.0, "ma60": 160000.0,
             "trend": "횡보/혼조"},
            {"ticker": "ZZZ", "error": "주가 데이터를 찾을 수 없습니다."},
        ]
        quotes = {"AAPL": {"price": 111.5, "source": "finnhub_quote"},
                  "005930.KS": {"price": 183100.0, "source": "naver_realtime",
                                "market_status": "OPEN"}}
        with patch.object(realtime_quotes, "get_prices", lambda *a, **k: quotes):
            out = stock_tools.apply_realtime_prices(rows)

        self.assertEqual(out[0]["price"], 111.5)                      # 실시간가로 덮어씀
        self.assertEqual(out[0]["price_source"], "finnhub_quote")
        self.assertEqual(out[0]["trend"], "하락 추세(역배열: 현재가<20일선<60일선)")
        self.assertEqual(out[1]["price"], 183100.0)
        self.assertEqual(out[1]["price_source"], "naver_realtime")
        self.assertEqual(out[1]["market_status"], "OPEN")
        self.assertEqual(out[1]["trend"], "상승 추세(정배열: 현재가>20일선>60일선)")
        self.assertEqual(out[2], {"ticker": "ZZZ", "error": "주가 데이터를 찾을 수 없습니다."})

    def test_failure_keeps_daily_close(self):
        rows = [{"ticker": "AAPL", "price": 100.0, "ma20": 90.0, "ma60": 80.0,
                 "trend": "상승 추세(정배열: 현재가>20일선>60일선)"}]

        def boom(*args, **kwargs):
            raise RuntimeError("quote layer down")

        with patch.object(realtime_quotes, "get_prices", boom):
            out = stock_tools.apply_realtime_prices(rows)

        self.assertEqual(out[0]["price"], 100.0)          # 일봉 종가 유지
        self.assertEqual(out[0]["price_source"], "daily_close")
        self.assertEqual(out[0]["trend"], "상승 추세(정배열: 현재가>20일선>60일선)")

    def test_missing_quote_falls_back_per_row(self):
        rows = [{"ticker": "AAPL", "price": 100.0},
                {"ticker": "MSFT", "price": 200.0}]
        with patch.object(realtime_quotes, "get_prices",
                          lambda *a, **k: {"AAPL": {"price": 101.0, "source": "finnhub_quote"}}):
            out = stock_tools.apply_realtime_prices(rows)
        self.assertEqual(out[0]["price"], 101.0)
        self.assertEqual(out[1]["price"], 200.0)
        self.assertEqual(out[1]["price_source"], "daily_close")

    def test_prefetched_quotes_skip_network(self):
        rows = [{"ticker": "AAPL", "price": 100.0}]

        def boom(*args, **kwargs):
            raise AssertionError("미리 받은 시세가 있으면 다시 조회하지 않는다")

        with patch.object(realtime_quotes, "get_prices", boom):
            out = stock_tools.apply_realtime_prices(
                rows, quotes={"AAPL": {"price": 101.0, "source": "finnhub_quote"}})
        self.assertEqual(out[0]["price"], 101.0)
        self.assertEqual(out[0]["price_source"], "finnhub_quote")


# ---------------------------------------------------------------------------
# 5. 성적표 현재가
# ---------------------------------------------------------------------------

class PerformanceRealtimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "perf.db")
        history_store.clear_price_cache()
        realtime_quotes.clear_cache()
        self.addCleanup(history_store.clear_price_cache)
        env = patch.dict(os.environ, {"REALTIME_PRICE": "1"})
        env.start()
        self.addCleanup(env.stop)

    def _record(self, ticker="AAPL", price=100.0):
        history_store.record_picks([{"ticker": ticker, "price": price}],
                                   strategy="value", db_path=self.db)

    def test_realtime_price_wins_over_daily_close(self):
        self._record()
        with patch.object(history_store, "_fetch_prices_batch", lambda t: {"AAPL": 100.0}), \
                patch.object(realtime_quotes, "get_prices",
                             lambda *a, **k: {"AAPL": {"price": 110.0,
                                                       "source": "finnhub_quote"}}):
            body = history_store.get_performance(db_path=self.db)

        pick = body["picks"][0]
        self.assertEqual(pick["current_price"], 110.0)        # 실시간 110
        self.assertEqual(pick["price_source"], "finnhub_quote")
        self.assertAlmostEqual(pick["return_pct"], 10.0, places=2)

    def test_korean_source_is_naver(self):
        self._record("005930.KS", 180000.0)
        with patch.object(history_store, "_fetch_prices_batch", lambda t: {"005930.KS": 180000.0}), \
                patch.object(realtime_quotes, "get_prices",
                             lambda *a, **k: {"005930.KS": {"price": 183100.0,
                                                            "source": "naver_realtime"}}):
            body = history_store.get_performance(db_path=self.db)

        pick = body["picks"][0]
        self.assertEqual(pick["current_price"], 183100.0)
        self.assertEqual(pick["price_source"], "naver_realtime")

    def test_realtime_failure_keeps_daily_close_with_source(self):
        self._record()
        with patch.object(history_store, "_fetch_prices_batch", lambda t: {"AAPL": 102.0}), \
                patch.object(realtime_quotes, "get_prices",
                             side_effect=RuntimeError("down")):
            body = history_store.get_performance(db_path=self.db)

        pick = body["picks"][0]
        self.assertEqual(pick["current_price"], 102.0)
        self.assertEqual(pick["price_source"], "daily_close")

    def test_injected_fetcher_skips_realtime(self):
        self._record()

        def no_realtime(*args, **kwargs):
            raise AssertionError("price_fetcher 주입 시 실시간 조회를 하면 안 된다")

        with patch.object(realtime_quotes, "get_prices", no_realtime):
            body = history_store.get_performance(db_path=self.db,
                                                 price_fetcher=lambda t: {"AAPL": 105.0})
        self.assertEqual(body["picks"][0]["current_price"], 105.0)

    def test_realtime_covers_so_daily_batch_is_skipped(self):
        """실시간가가 모든 픽을 덮으면 Yahoo 일봉 배치(2~5초)를 부르지 않는다."""
        self._record()

        def no_batch(tickers):
            raise AssertionError("실시간가가 있으면 일봉 배치를 부르지 않는다")

        with patch.object(history_store, "_fetch_prices_batch", no_batch), \
                patch.object(realtime_quotes, "get_prices",
                             lambda *a, **k: {"AAPL": {"price": 111.0,
                                                       "source": "finnhub_quote"}}):
            body = history_store.get_performance(db_path=self.db)

        self.assertEqual(body["picks"][0]["current_price"], 111.0)
        self.assertEqual(body["picks"][0]["price_source"], "finnhub_quote")


class ResolveCurrentPricesTests(unittest.TestCase):
    """`resolve_current_prices` — 진입가·성적표가 같이 쓰는 가격 우선순위."""

    def setUp(self):
        history_store.clear_price_cache()
        self.addCleanup(history_store.clear_price_cache)

    def test_realtime_wins_and_untouched_tickers_use_close(self):
        with patch.object(realtime_quotes, "get_prices",
                          lambda *a, **k: {"AAPL": {"price": 205.4, "source": "finnhub_quote"}}):
            prices, sources, ok = history_store.resolve_current_prices(
                ["AAPL", "MSFT"], price_fetcher=lambda t: {"MSFT": 410.0})
        self.assertEqual(prices, {"AAPL": 205.4, "MSFT": 410.0})
        self.assertEqual(sources, {"AAPL": "finnhub_quote", "MSFT": "daily_close"})
        self.assertTrue(ok)

    def test_use_realtime_false_skips_quote_layer(self):
        def no_realtime(*args, **kwargs):
            raise AssertionError("use_realtime=False면 실시간 조회를 하지 않는다")

        with patch.object(realtime_quotes, "get_prices", no_realtime):
            prices, sources, ok = history_store.resolve_current_prices(
                ["AAPL"], price_fetcher=lambda t: {"AAPL": 101.0}, use_realtime=False)
        self.assertEqual((prices, sources, ok), ({"AAPL": 101.0},
                                                 {"AAPL": "daily_close"}, True))

    def test_everything_fails_returns_none_prices(self):
        with patch.object(realtime_quotes, "get_prices", lambda *a, **k: {}):
            prices, sources, ok = history_store.resolve_current_prices(
                ["ZZZZ"], price_fetcher=lambda t: {})
        self.assertEqual(prices, {"ZZZZ": None})
        self.assertEqual(sources, {"ZZZZ": "daily_close"})
        self.assertTrue(ok)

    def test_close_failure_keeps_realtime_and_flags_fallback(self):
        def boom(tickers):
            raise RuntimeError("yahoo down")

        with patch.object(realtime_quotes, "get_prices",
                          lambda *a, **k: {"AAPL": {"price": 205.4, "source": "finnhub_quote"}}):
            prices, sources, ok = history_store.resolve_current_prices(
                ["AAPL", "MSFT"], price_fetcher=boom)
        self.assertEqual(prices["AAPL"], 205.4)                       # 실시간가는 살아 있다
        self.assertEqual(sources["AAPL"], "finnhub_quote")
        self.assertFalse(ok)                                         # 폴백 실패 표시


# ---------------------------------------------------------------------------
# 6~7. /api/screen (AI 캐시·뉴스 상위 N) · /api/stock
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeGemini:
    """호출 횟수를 세는 Gemini 대역. `fail`이면 실패한다."""

    def __init__(self, payload, fail: Exception | None = None):
        self.payload = payload
        self.fail = fail
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        return FakeResponse("```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```")


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ScreenCacheTests(unittest.TestCase):
    TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO"]

    def setUp(self):
        api.clear_analysis_cache()
        self.addCleanup(api.clear_analysis_cache)
        api._NEWS_CACHE.clear()
        api.realtime_quotes.clear_cache()
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        self.names = iter(f"뉴스{i}" for i in range(100))
        self.news_calls = []

        def fake_news(ticker, limit=3):
            self.news_calls.append(ticker)
            return [f"{ticker} headline"]

        self.patches = [
            patch.object(stock_tools, "get_stock_metrics",
                         lambda t: {"ticker": t, "name": t, "price": 50.0, "ma20": 45.0,
                                    "ma60": 40.0, "rsi14": 55.0, "trend": "상승", "signals": [],
                                    "financials_available": True}),
            patch.object(stock_tools, "get_history", lambda t, p="6mo": make_hist()),
            patch.object(api, "_get_recent_news", fake_news),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(api.app)
        self.quotes = {t: 50.0 for t in self.TICKERS}

    def _use_quotes(self, source="finnhub_quote"):
        """실시간 시세 대역 — `self.quotes`를 바꾸면 다음 조회에 반영된다."""
        def fake_quotes(tickers, max_workers=8):
            return {t: {"price": self.quotes[t], "source": source}
                    for t in tickers if self.quotes.get(t) is not None}
        p = patch.object(realtime_quotes, "get_prices", fake_quotes)
        p.start()
        self.addCleanup(p.stop)

    def _use_gemini(self, payload, fail=None):
        fake = FakeGemini(payload, fail=fail)
        p = patch.object(api.genai, "Client", lambda **_: fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _screen(self, tickers=None, criteria="저PER 가치주"):
        return self.client.post("/api/screen",
                                json={"criteria": criteria,
                                      "tickers": list(tickers or self.TICKERS)}).json()

    def test_cache_hit_skips_gemini_but_refreshes_price(self):
        self._use_quotes()
        fake = self._use_gemini({"market_note": "요약", "top_picks": [
            {"ticker": "AAPL", "price": 1.0, "reason": "근거", "risk": "위험"}]})

        first = self._screen()
        self.assertFalse(first["analysis_cached"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(first["all_stocks"][0]["price"], 50.0)

        # 두 번째 호출: Gemini는 부르지 않고, 주가는 새 값(60)으로 갱신된다.
        self.quotes["AAPL"] = 60.0
        second = self._screen()
        self.assertTrue(second["analysis_cached"])
        self.assertEqual(len(fake.calls), 1)                       # Gemini 추가 호출 없음
        self.assertEqual(second["timings"]["gemini_ms"], 0)
        prices = {r["ticker"]: r["price"] for r in second["all_stocks"]}
        self.assertEqual(prices["AAPL"], 60.0)
        pick = next(p for p in second["analysis"]["top_picks"] if p["ticker"] == "AAPL")
        self.assertEqual(pick["price"], 60.0)                       # 카드 가격도 실시간
        self.assertEqual(pick["price_source"], "finnhub_quote")
        self.assertEqual(second["analysis"]["market_note"], "요약")

    def test_different_criteria_is_a_cache_miss(self):
        self._use_quotes()
        fake = self._use_gemini({"market_note": "요약", "top_picks": []})
        self._screen(criteria="저PER 가치주")
        second = self._screen(criteria="모멘텀 돌파")
        self.assertFalse(second["analysis_cached"])
        self.assertEqual(len(fake.calls), 2)

    def test_fallback_is_cached_only_briefly(self):
        self._use_quotes()
        broken = self._use_gemini({}, fail=RuntimeError("503 UNAVAILABLE"))
        first = self._screen()
        self.assertEqual(first["analysis_source"], "fallback")
        self.assertFalse(first["analysis_cached"])
        with api._ANALYSIS_LOCK:
            entry = list(api._ANALYSIS_CACHE.values())[0]
        self.assertEqual(entry["ttl"], api.ANALYSIS_FALLBACK_TTL)   # 60초만

        # 60초가 지나면(캐시 항목을 과거로 밀어) 정상 Gemini로 다시 시도한다.
        with api._ANALYSIS_LOCK:
            for value in api._ANALYSIS_CACHE.values():
                value["at"] -= api.ANALYSIS_FALLBACK_TTL + 1
        with patch.object(api.genai, "Client",
                          lambda **_: FakeGemini({"market_note": "정상", "top_picks": []})):
            second = self._screen()
        self.assertFalse(second["analysis_cached"])
        self.assertEqual(second["analysis_source"], "gemini")
        # 실패했던 대역은 두 번째 요청에서 더 불리지 않았다(폴백 결과는 60초만 기억)
        broken_calls_after_first = len(broken.calls)
        with patch.object(api.genai, "Client", lambda **_: broken):
            self._screen(criteria="다른 조건")   # 새 키 → Gemini 경로를 다시 탄다
        self.assertEqual(len(broken.calls), broken_calls_after_first + 2)  # 주 모델 + 2순위

    def test_news_only_for_top_five(self):
        self._use_quotes()
        self._use_gemini({"market_note": "요약", "top_picks": []})
        body = self._screen()
        with_news = [r["ticker"] for r in body["all_stocks"] if r["news"]]
        self.assertEqual(len(with_news), api.NEWS_TOP_N)
        self.assertEqual(sorted(self.news_calls), sorted(with_news))
        self.assertEqual(sum(1 for r in body["all_stocks"] if r["news"] == []),
                         len(self.TICKERS) - api.NEWS_TOP_N)

    def test_prompt_contains_compact_rows_and_strategy(self):
        self._use_quotes()
        fake = self._use_gemini({"market_note": "요약", "top_picks": []})
        self._screen()
        prompt = fake.calls[0]["contents"]
        self.assertIn("AAPL", prompt)
        self.assertIn("quant_score", prompt)
        self.assertIn("strategy: value", prompt)

    def test_realtime_failure_does_not_break_screen(self):
        self._use_gemini({"market_note": "요약", "top_picks": []})
        with patch.object(realtime_quotes, "get_prices", side_effect=RuntimeError("down")):
            body = self._screen(["AAPL"])
        self.assertEqual(body["all_stocks"][0]["price"], 50.0)     # 일봉 종가 유지
        self.assertEqual(body["all_stocks"][0]["price_source"], "daily_close")

    def test_stock_detail_current_price_is_realtime(self):
        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            @property
            def quarterly_income_stmt(self):
                return pd.DataFrame()

        with patch.object(api.yf, "Ticker", FakeTicker), \
                patch.object(stock_tools, "get_history", lambda t, p="6mo": make_hist()), \
                patch.object(realtime_quotes, "get_prices",
                             lambda *a, **k: {"AAPL": {"price": 151.25,
                                                       "source": "finnhub_quote"}}):
            body = self.client.get("/api/stock/AAPL").json()

        self.assertEqual(body["levels"]["current"], 151.25)
        self.assertEqual(body["price_source"], "finnhub_quote")


if __name__ == "__main__":
    unittest.main()
