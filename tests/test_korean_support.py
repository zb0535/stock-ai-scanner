"""한국 주식 지원(네이버 금융) · 코스피 200 자동 발굴 테스트.

검증 대상
---------
1. 티커 해석 — 한글 이름·6자리 코드·`.KS`/`.KQ` 접미사·미국 티커 통과·내장 폴백·캐시
2. 네이버 파서 — 숫자 파싱, PER/추정PER, 영업이익률(%→소수), 부채비율(%), 매출증가율(확정 연도만)
3. 통화·이름 — `stock_tools`가 한국 종목에 KRW·한글명을 붙이고 SEC/Finnhub를 부르지 않는다,
   성적표(`history_store`)가 티커 접미사로 KRW를 추론한다
4. 코스피 200 — 구성종목 폴백, 프리 스캔·캐시·전략 랭킹, `/api/screen` mode="kospi200",
   `/api/resolve-ticker`, `/api/kospi200`, `GET /`의 `kospi200_cache`
5. 예열 — 플래그 3종, 첫 지연·주기, 중복 방지, S&P 500 스캔과의 메모리 보호, 실패 시 직전 스냅샷

실행: python -m unittest discover -s tests -v
"""
import json
import os
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
import history_store  # noqa: E402
import kospi200_scanner  # noqa: E402
import naver_finance  # noqa: E402
import scoring  # noqa: E402
import sp500_scanner  # noqa: E402
import stock_tools  # noqa: E402

try:  # TestClient는 httpx에 의존한다.
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


# --- 네이버 응답 대역 ---------------------------------------------------------

class FakeResp:
    def __init__(self, payload=None, content: bytes | None = None):
        self._payload = payload
        self.content = content if content is not None else b""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


AC_ITEMS = {
    "삼성전자": [
        {"code": "005930", "name": "삼성전자", "typeCode": "KOSPI"},
        {"code": "0162Z0", "name": "RISE 삼성전자SK하이닉스채권혼합50", "typeCode": "KOSPI"},
    ],
    "에코프로": [
        {"code": "086520", "name": "에코프로", "typeCode": "KOSDAQ"},
        {"code": "247540", "name": "에코프로비엠", "typeCode": "KOSDAQ"},
    ],
    "000660": [{"code": "000660", "name": "SK하이닉스", "typeCode": "KOSPI"}],
    "NAVER": [{"code": "035420", "name": "NAVER", "typeCode": "KOSPI"}],
    "005930": [{"code": "005930", "name": "삼성전자", "typeCode": "KOSPI"}],
    "AAPL": [],
}

INTEGRATION = {
    "stockName": "삼성전자",
    "totalInfos": [
        {"code": "per", "key": "PER", "value": "30.07배"},
        {"code": "cnsPer", "key": "추정PER", "value": "4.33배"},
        {"code": "pbr", "key": "PBR", "value": "2.29배"},
        {"code": "eps", "key": "EPS", "value": "6,564원"},
        {"code": "dividendYieldRatio", "key": "배당수익률", "value": "0.85%"},
        {"code": "marketValue", "key": "시총", "value": "1,154조 554억"},
    ],
}

ANNUAL = {
    "financeInfo": {
        "trTitleList": [
            {"key": "202312", "isConsensus": "N"},
            {"key": "202412", "isConsensus": "N"},
            {"key": "202512", "isConsensus": "N"},
            {"key": "202612", "isConsensus": "Y"},
        ],
        "rowList": [
            {"title": "매출액", "columns": {
                "202312": {"value": "258,935"}, "202412": {"value": "300,870"},
                "202512": {"value": "333,605"}, "202612": {"value": "720,735"}}},
            {"title": "영업이익률", "columns": {
                "202312": {"value": "2.54"}, "202412": {"value": "10.88"},
                "202512": {"value": "13.07"}, "202612": {"value": "53.15"}}},
            {"title": "부채비율", "columns": {
                "202312": {"value": "25.36"}, "202412": {"value": "27.93"},
                "202512": {"value": "29.94"}, "202612": {"value": "-"}}},
        ],
    }
}


class FakeNaver:
    """`naver_finance.requests.get` 대역 — URL·파라미터로 응답을 고른다."""

    def __init__(self, ac=None, integration=None, annual=None, news=None, google=None, fail=False):
        self.ac = AC_ITEMS if ac is None else ac
        self.integration = INTEGRATION if integration is None else integration
        self.annual = ANNUAL if annual is None else annual
        self.news = [{"total": 1, "items": [{"title": "삼성전자 뉴스"}]}] if news is None else news
        # 구글 뉴스 RSS 기본값: '삼성전자 뉴스'와 다른 제목을 준다(우선순위 검증용)
        self.google = ["구글 뉴스 제목"] if google is None else google
        self.fail = fail
        self.calls = []

    def __call__(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        if self.fail:
            raise RuntimeError("naver blocked")
        if "ac.stock.naver.com" in url:
            return FakeResp({"items": list(self.ac.get((params or {}).get("q", ""), []))})
        if "integration" in url:
            return FakeResp(self.integration)
        if "finance/annual" in url:
            return FakeResp(self.annual)
        if "/news/stock/" in url:
            return FakeResp(self.news)
        if "enrollStocks" in url:
            page = int((params or {}).get("page", 1))
            rows = [{"itemCode": f"{i:06d}", "stockName": f"종목{i}",
                     "stockExchangeType": {"code": "KS"}} for i in range(page * 20 - 20, page * 20)]
            return FakeResp(rows if page <= 8 else [])
        if "news.google.com" in url:
            items = "".join(f"<item><title>{t}</title></item>" for t in self.google)
            return FakeResp(content=f"<rss><channel>{items}</channel></rss>".encode("utf-8"))
        raise AssertionError(f"예상하지 못한 URL: {url}")


def hist(close, volume=1_000_000.0):
    close = np.asarray(close, dtype="float64")
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-18"), periods=len(close))
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": volume}, index=idx)


class KoreanBase(unittest.TestCase):
    def setUp(self):
        naver_finance.clear_cache()
        naver_finance.reset_table_cache()
        kospi200_scanner.reset_cache()
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        api.clear_analysis_cache()
        self.addCleanup(api.clear_analysis_cache)
        api.realtime_quotes.clear_cache()
        # 실시간 시세(네이버 polling)는 실네트워크라 테스트에서는 끄고 일봉 종가를 쓴다.
        self.patch(api.realtime_quotes, "get_prices", lambda *a, **k: {})
        stock_tools._info_fail_streak = 0
        stock_tools._info_cooldown_until = 0.0

    def tearDown(self):
        kospi200_scanner.stop_prewarm()
        naver_finance.clear_cache()
        naver_finance.reset_table_cache()
        kospi200_scanner.reset_cache()
        api.clear_analysis_cache()

    def patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value


# --- 1. 티커 해석 -------------------------------------------------------------

class ResolveTickerTests(KoreanBase):
    def test_korean_name_resolves_to_kospi_symbol(self):
        fake = FakeNaver()
        self.patch(naver_finance.requests, "get", fake)

        out = naver_finance.resolve_ticker("삼성전자")

        self.assertEqual(out["ticker"], "005930.KS")
        self.assertEqual(out["name"], "삼성전자")
        self.assertEqual(out["exchange"], "KOSPI")
        self.assertEqual(out["currency"], "KRW")
        self.assertEqual(out["candidates"][0]["ticker"], "005930.KS")

    def test_kosdaq_name_resolves_to_kq_symbol(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        out = naver_finance.resolve_ticker("에코프로")
        self.assertEqual(out["ticker"], "086520.KQ")
        self.assertEqual(out["exchange"], "KOSDAQ")

    def test_six_digit_code_resolves_to_kospi_symbol(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        self.assertEqual(naver_finance.resolve_ticker("000660")["ticker"], "000660.KS")
        self.assertEqual(naver_finance.resolve_ticker("005930")["ticker"], "005930.KS")

    def test_suffix_ticker_is_normalised(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        out = naver_finance.resolve_ticker(" 005930.ks ")
        self.assertEqual(out["ticker"], "005930.KS")
        self.assertEqual(out["name"], "삼성전자")   # 내장 목록에서 오프라인 해석

    def test_us_ticker_passes_through(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        out = naver_finance.resolve_ticker("aapl")
        self.assertEqual(out["ticker"], "AAPL")
        self.assertEqual(out["currency"], "USD")
        self.assertEqual(out["source"], "passthrough")

    def test_korean_name_in_latin_letters_resolves_by_exact_match(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        self.assertEqual(naver_finance.resolve_ticker("NAVER")["ticker"], "035420.KS")

    def test_offline_fallback_uses_builtin_kospi200_list(self):
        self.patch(naver_finance.requests, "get", FakeNaver(fail=True))
        out = naver_finance.resolve_ticker("카카오")
        self.assertIsNotNone(out)
        self.assertEqual(out["ticker"], "035720.KS")
        self.assertEqual(out["source"], "fallback")

    def test_unknown_name_returns_none_and_is_cached(self):
        fake = FakeNaver()
        self.patch(naver_finance.requests, "get", fake)
        self.assertIsNone(naver_finance.resolve_ticker("없는회사이름"))
        calls = len(fake.calls)
        self.assertIsNone(naver_finance.resolve_ticker("없는회사이름"))
        self.assertEqual(len(fake.calls), calls)  # 실패도 TTL 캐시(재호출 없음)

    def test_resolution_is_cached(self):
        fake = FakeNaver()
        self.patch(naver_finance.requests, "get", fake)
        naver_finance.resolve_ticker("삼성전자")
        calls = len(fake.calls)
        naver_finance.resolve_ticker("삼성전자")
        self.assertEqual(len(fake.calls), calls)

    def test_empty_query_returns_none(self):
        self.assertIsNone(naver_finance.resolve_ticker("   "))


# --- 2. 네이버 파서 -----------------------------------------------------------

class NaverParserTests(KoreanBase):
    def test_number_parser_handles_units_and_junk(self):
        self.assertEqual(naver_finance._num("30.07배"), 30.07)
        self.assertEqual(naver_finance._num("6,564원"), 6564.0)
        self.assertEqual(naver_finance._num("0.85%"), 0.85)
        self.assertIsNone(naver_finance._num("-"))
        self.assertIsNone(naver_finance._num("1,154조 554억"))
        self.assertIsNone(naver_finance._num(None))
        self.assertIsNone(naver_finance._num(float("nan")))

    def test_fundamentals_parse_integration_and_annual(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        fund = naver_finance.get_fundamentals("005930.KS")

        self.assertEqual(fund["name"], "삼성전자")
        self.assertEqual(fund["trailing_pe"], 30.07)
        self.assertEqual(fund["forward_pe"], 4.33)
        self.assertEqual(fund["pbr"], 2.29)
        self.assertEqual(fund["dividend_yield"], 0.85)
        # 영업이익률 13.07% → 소수 0.1307 (stock_tools 단위)
        self.assertEqual(fund["operating_margins"], 0.1307)
        # 부채비율은 이미 % 단위 그대로
        self.assertEqual(fund["debt_to_equity"], 29.94)
        # 매출증가율 = 2025 확정 / 2024 확정 (추정 2026 제외)
        self.assertEqual(fund["revenue_growth"], round((333605 - 300870) / 300870, 4))

    def test_fundamentals_ignore_consensus_year(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        fund = naver_finance.get_fundamentals("005930.KS")
        self.assertNotEqual(fund["operating_margins"], 0.5315)  # 2026 추정치를 쓰지 않는다
        self.assertNotEqual(fund["revenue_growth"], round((720735 - 333605) / 333605, 4))

    def test_fundamentals_tolerate_missing_sections(self):
        self.patch(naver_finance.requests, "get",
                   FakeNaver(integration={"totalInfos": []}, annual={"financeInfo": {}}))
        fund = naver_finance.get_fundamentals("005930.KS")
        self.assertIsNone(fund["trailing_pe"])
        self.assertIsNone(fund["operating_margins"])
        self.assertIsNone(fund["revenue_growth"])

    def test_fundamentals_are_cached(self):
        fake = FakeNaver()
        self.patch(naver_finance.requests, "get", fake)
        naver_finance.get_fundamentals("005930.KS")
        calls = len(fake.calls)
        naver_finance.get_fundamentals("005930.KS")
        self.assertEqual(len(fake.calls), calls)

    def test_non_korean_ticker_has_no_naver_fundamentals(self):
        self.assertIsNone(naver_finance.get_fundamentals("AAPL"))

    def test_korean_news_prefers_google_titles(self):
        self.patch(naver_finance.requests, "get", FakeNaver())
        titles = naver_finance.get_korean_news("005930.KS", 3)
        self.assertEqual(titles[0], "구글 뉴스 제목")
        self.assertIn("삼성전자 뉴스", titles)   # 모자란 만큼 네이버로 보충

    def test_korean_news_falls_back_to_naver_when_google_is_empty(self):
        self.patch(naver_finance.requests, "get", FakeNaver(google=[]))
        self.assertEqual(naver_finance.get_korean_news("005930.KS", 3), ["삼성전자 뉴스"])


# --- 3. 통화·이름 전파 ---------------------------------------------------------

class CurrencyTests(KoreanBase):
    def test_stock_tools_prefers_naver_for_korean_stock(self):
        calls = {"info": 0}

        class KrTicker:
            """한국 종목 대역 — `t.info`는 불리면 기록하고 실패한다(불러서는 안 된다)."""

            @property
            def info(self):
                calls["info"] += 1
                raise AssertionError("한국 종목은 Yahoo t.info를 부르면 안 된다")

            def history(self, period="6mo"):
                return hist(np.linspace(100, 120, 130))

        self.patch(stock_tools.yf, "Ticker", lambda sym: KrTicker())
        self.patch(stock_tools.naver_finance, "get_fundamentals", lambda sym: {
            "name": "삼성전자", "trailing_pe": 30.07, "forward_pe": 4.33,
            "revenue_growth": 0.1088, "operating_margins": 0.1307, "debt_to_equity": 29.94,
        })
        sec_calls = []
        self.patch(stock_tools.sec_edgar, "get_financial_metrics",
                   lambda *a, **k: sec_calls.append(1) or {})
        self.patch(stock_tools.finnhub_client, "is_enabled", lambda: True)

        m = stock_tools.get_stock_metrics("005930.KS")

        self.assertEqual(calls["info"], 0)                 # Yahoo t.info 호출 0회(지연의 주범 제거)
        self.assertEqual(m["currency"], "KRW")
        self.assertEqual(m["name"], "삼성전자")
        self.assertEqual(m["trailing_pe"], 30.07)
        self.assertEqual(m["operating_margins"], 0.1307)   # Yahoo 46%가 아니라 국내 공시 13%
        self.assertEqual(m["debt_to_equity"], 29.94)
        self.assertEqual(m["financials_source"], "naver")  # 한국 종목 재무 출처는 네이버 금융
        self.assertEqual(sec_calls, [])                     # 한국 종목은 SEC를 부르지 않는다
        self.assertTrue(m["financials_available"])

    def test_us_stock_keeps_usd_and_yfinance_source(self):
        self.patch(stock_tools.yf, "Ticker", lambda sym: type("T", (), {
            "info": property(lambda self: {"shortName": "Apple Inc.", "trailingPE": 39.2,
                                           "revenueGrowth": 0.117, "operatingMargins": 0.33,
                                           "debtToEquity": 84.2}),
            "history": lambda self, period="6mo": hist(np.linspace(100, 120, 130)),
        })())
        m = stock_tools.get_stock_metrics("AAPL")
        self.assertEqual(m["currency"], "USD")
        self.assertEqual(m["name"], "Apple Inc.")
        self.assertEqual(m["financials_source"], "yfinance")

    def test_performance_infers_currency_and_name_from_suffix(self):
        body = history_store.compute_performance(
            [{"id": 1, "ticker": "005930.KS", "entry_price": 70000, "recommended_date": "2026-11-01",
              "strategy": "value", "mode": "kospi200"},
             {"id": 2, "ticker": "AAPL", "entry_price": 200, "recommended_date": "2026-11-01",
              "strategy": "value", "mode": "watchlist"}],
            {"005930.KS": 73000.0, "AAPL": 190.0},
        )
        kr, us = body["picks"]
        self.assertEqual(kr["currency"], "KRW")
        self.assertEqual(kr["name"], "삼성전자")
        self.assertEqual(kr["return_pct"], 4.29)
        self.assertEqual(us["currency"], "USD")
        self.assertIsNone(us["name"])
        # 픽별 통화는 요약에도 그대로 남는다
        self.assertEqual(body["summary"]["best_pick"]["currency"], "KRW")

    def test_krw_price_display_contract(self):
        """프론트엔드 `fmtPrice`와 같은 규칙(원 단위 반올림·천단위 콤마)을 서버 값으로 확인한다."""
        price = stock_tools._to_number("196900")
        self.assertEqual(f"₩{int(round(price)):,}", "₩196,900")
        self.assertEqual(f"${352.45:,.2f}", "$352.45")


# --- 4. 코스피 200 ------------------------------------------------------------

def scan_rows_with(tickers, frames=None):
    """`kospi200_scanner._scan_universe` 대역 — 요청한 티커에 대해 지표 행을 만든다."""
    frames = frames or {}
    rows = []
    for sym in tickers:
        frame = frames.get(sym)
        if frame is None:
            continue
        close = frame["Close"]
        rows.append({"ticker": sym, "price": round(float(close.iloc[-1]), 2),
                     "ma20": round(float(close.rolling(20).mean().iloc[-1]), 2),
                     "ma60": round(float(close.rolling(60).mean().iloc[-1]), 2),
                     "rsi14": stock_tools._calc_rsi(close, 14),
                     "trend": "하락 추세", **stock_tools.compute_advanced_indicators(frame)})
    return {"rows": rows, "universe": len(tickers), "skipped": len(tickers) - len(rows)}


class Kospi200ScannerTests(KoreanBase):
    def setUp(self):
        super().setUp()
        self._real_table = naver_finance.get_kospi200_table
        self.patch(naver_finance.requests, "get", FakeNaver())
        self.patch(naver_finance, "get_kospi200_table", lambda force_refresh=False: {
            "rows": [{"symbol": f"00593{i}.KS", "name": f"종목{i}", "exchange": "코스피 200"}
                     for i in range(20)],
            "source": "naver"})

    def test_constituent_list_falls_back_to_builtin_when_live_fails(self):
        naver_finance.reset_table_cache()
        self.patch(naver_finance, "get_kospi200_table", self._real_table)  # 라이브 경로를 직접 탄다
        self.patch(naver_finance.requests, "get", FakeNaver(fail=True))
        table = naver_finance.get_kospi200_table()
        self.assertEqual(table["source"], "fallback")
        self.assertGreaterEqual(len(table["rows"]), 190)
        self.assertIn("005930.KS", [r["symbol"] for r in table["rows"]])

    def test_live_constituent_list_uses_naver_pages(self):
        naver_finance.reset_table_cache()
        self.patch(naver_finance, "get_kospi200_table", self._real_table)
        table = naver_finance.get_kospi200_table()
        self.assertEqual(table["source"], "naver")
        self.assertEqual(len(table["rows"]), 160)
        self.assertEqual(table["rows"][0]["symbol"], "000000.KS")

    def test_builtin_list_has_no_duplicates_and_all_korean_symbols(self):
        codes = [code for code, _ in naver_finance.KOSPI200_FALLBACK]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertGreaterEqual(len(codes), 190)
        self.assertTrue(all(naver_finance.is_korean_ticker(f"{c}.KS") for c in codes))

    def test_select_candidates_ranks_by_strategy_and_reports_universe(self):
        washed = hist(np.linspace(200, 120, 130))
        breakout = hist(np.linspace(80, 130, 130), volume=[1_000_000.0] * 129 + [3_000_000.0])
        tickers = ["005930.KS", "000660.KS"]
        self.patch(kospi200_scanner, "_scan_universe",
                   lambda t=None: scan_rows_with(tickers, {"005930.KS": washed,
                                                           "000660.KS": breakout}))

        oversold = kospi200_scanner.select_candidates("단기 눌림목 반등")
        momentum = kospi200_scanner.select_candidates("모멘텀 돌파")

        self.assertEqual(oversold["strategy"], "oversold")
        self.assertEqual(oversold["universe_scanned"], 2)
        self.assertEqual(oversold["scanned"], 2)
        self.assertEqual(oversold["tickers"][0], "005930.KS")
        self.assertEqual(momentum["strategy"], "momentum")
        self.assertEqual(momentum["tickers"][0], "000660.KS")
        self.assertFalse(oversold["cached"])
        self.assertTrue(momentum["cached"])       # 70분 캐시
        self.assertEqual(oversold["universe_source"], "naver")

    def test_snapshot_expires_and_failed_rescan_keeps_previous(self):
        self.patch(kospi200_scanner, "_scan_universe",
                   lambda t=None: scan_rows_with(["005930.KS"], {"005930.KS": hist(np.linspace(100, 120, 130))}))
        kospi200_scanner.prescan(force_refresh=True)
        kospi200_scanner._SNAPSHOT["ts"] = time.time() - (kospi200_scanner.PRESCAN_TTL + 5)
        self.patch(kospi200_scanner, "_scan_universe", lambda t=None: {"rows": [], "universe": 1, "skipped": 1})

        result = kospi200_scanner.prescan()

        self.assertTrue(result["cached"])
        self.assertTrue(result["stale"])
        self.assertEqual([r["ticker"] for r in result["rows"]], ["005930.KS"])

    def test_top_n_is_capped(self):
        tickers = [f"{i:06d}.KS" for i in range(30)]
        frames = {t: hist(np.linspace(200, 150, 130)) for t in tickers}
        self.patch(kospi200_scanner, "_scan_universe", lambda t=None: scan_rows_with(tickers, frames))
        result = kospi200_scanner.select_candidates("눌림목", top_n=15)
        self.assertEqual(len(result["tickers"]), 15)

    def test_health_reports_kospi200_cache(self):
        self.patch(kospi200_scanner, "_scan_universe",
                   lambda t=None: scan_rows_with(["005930.KS"], {"005930.KS": hist(np.linspace(100, 120, 130))}))
        kospi200_scanner.prescan(force_refresh=True)
        body = TestClient(api.app).get("/").json()
        cache = body["kospi200_cache"]
        self.assertTrue(cache["warm"])
        self.assertTrue(cache["fresh"])
        self.assertEqual(cache["tickers"], 1)
        self.assertEqual(cache["ttl_sec"], kospi200_scanner.PRESCAN_TTL)
        self.assertEqual(cache["universe_source"], "naver")


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class Kospi200ApiTests(KoreanBase):
    DISCOVERED = ["005930.KS", "000660.KS", "035420.KS"]

    def setUp(self):
        super().setUp()
        self.patch(naver_finance.requests, "get", FakeNaver())
        self.patch(api, "_get_recent_news", lambda ticker, limit=3: [f"{ticker} 뉴스"])
        self.patch(stock_tools, "get_history", lambda ticker, period="6mo": hist(np.linspace(100, 120, 130)))
        self.patch(stock_tools, "get_stock_metrics", self._row)
        self.discovery_calls = []
        self.patch(api.kospi200_scanner, "select_candidates", self._fake_discovery)
        self.fake_ai = FakeGenai({"market_note": "요약", "top_picks": [
            {"ticker": self.DISCOVERED[0], "price": 196900.0, "reason": "근거", "risk": "위험"}]})
        self.patch(api.genai, "Client", lambda **_: self.fake_ai)

    @staticmethod
    def _row(ticker):
        is_kr = naver_finance.is_korean_ticker(ticker)
        return {"ticker": ticker, "name": "삼성전자" if is_kr else ticker, "currency": "KRW" if is_kr else "USD",
                "price": 196900.0, "ma20": 190000.0, "ma60": 180000.0, "rsi14": 32.0,
                "trend": "하락 추세", "trailing_pe": 30.07, "financials_available": True,
                "news": [f"{ticker} 뉴스"]}

    def _fake_discovery(self, criteria=None, top_n=15, force_refresh=False):
        self.discovery_calls.append({"criteria": criteria, "top_n": top_n})
        return {"tickers": list(self.DISCOVERED), "strategy": scoring.detect_strategy(criteria),
                "universe_scanned": 201, "scanned": 201, "skipped": 0,
                "cached": True, "stale": False, "candidates": [], "universe_source": "naver"}

    def test_screen_mode_kospi200_runs_discovery_and_keeps_currency(self):
        resp = TestClient(api.app).post("/api/screen", json={"criteria": "단기 눌림목 반등", "mode": "kospi200"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(body["mode"], "kospi200")
        self.assertEqual(body["universe_scanned"], 201)
        self.assertEqual(body["strategy"], "oversold")
        self.assertEqual([r["ticker"] for r in body["all_stocks"]], self.DISCOVERED)
        self.assertEqual(body["all_stocks"][0]["currency"], "KRW")
        self.assertEqual(self.discovery_calls, [{"criteria": "단기 눌림목 반등", "top_n": 15}])

        pick = body["analysis"]["top_picks"][0]
        self.assertEqual(pick["currency"], "KRW")
        self.assertEqual(pick["name"], "삼성전자")
        self.assertEqual(len(pick["chart"]), 60)
        prompt = self.fake_ai.models.calls[0]["contents"]
        self.assertIn("코스피 200 201종목", prompt)
        self.assertIn("원화(₩)", prompt)
        self.assertIn("005930.KS", prompt)

    def test_watchlist_mode_does_not_touch_discovery(self):
        body = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER", "tickers": ["005930.KS"]}).json()
        self.assertEqual(body["mode"], "watchlist")
        self.assertIsNone(body["universe_scanned"])
        self.assertEqual(self.discovery_calls, [])

    def test_resolve_ticker_endpoint(self):
        resp = TestClient(api.app).get("/api/resolve-ticker", params={"q": "삼성전자"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["query"], "삼성전자")
        self.assertEqual(body["ticker"], "005930.KS")
        self.assertEqual(body["currency"], "KRW")
        self.assertEqual(body["name"], "삼성전자")

    def test_resolve_ticker_endpoint_passes_us_ticker(self):
        body = TestClient(api.app).get("/api/resolve-ticker", params={"q": "NVDA"}).json()
        self.assertEqual(body["ticker"], "NVDA")
        self.assertEqual(body["currency"], "USD")

    def test_resolve_ticker_unknown_is_404(self):
        resp = TestClient(api.app).get("/api/resolve-ticker", params={"q": "없는회사이름"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "not_found")

    def test_resolve_ticker_requires_query(self):
        self.assertEqual(TestClient(api.app).get("/api/resolve-ticker").status_code, 400)

    def test_kospi200_list_endpoint_never_empty(self):
        self.patch(naver_finance.requests, "get", FakeNaver(fail=True))
        body = TestClient(api.app).get("/api/kospi200").json()
        self.assertGreaterEqual(body["count"], 190)
        self.assertEqual(body["source"], "fallback")
        self.assertEqual(set(body["results"][0]), {"symbol", "name", "exchange"})


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse("```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```")


class FakeGenai:
    def __init__(self, payload):
        self.models = FakeModels(payload)


# --- 5. 코스피 200 예열 -------------------------------------------------------

def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class Kospi200PrewarmTests(KoreanBase):
    def setUp(self):
        super().setUp()
        kospi200_scanner.stop_prewarm()
        env = {kospi200_scanner.PREWARM_ENV: "1", kospi200_scanner.PREWARM_MASTER_ENV: "1",
               kospi200_scanner.PREWARM_LEGACY_ENV: "1"}
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.patch(naver_finance, "get_kospi200_table", lambda force_refresh=False: {
            "rows": [{"symbol": "005930.KS", "name": "삼성전자", "exchange": "코스피 200"}],
            "source": "naver"})
        self.scan_calls = []
        frame = hist(np.linspace(100, 120, 130))

        def fake_scan(tickers=None):
            self.scan_calls.append(list(tickers or []))
            return scan_rows_with(list(tickers or ["005930.KS"]), {"005930.KS": frame})

        self.patch(kospi200_scanner, "_scan_universe", fake_scan)

    def tearDown(self):
        kospi200_scanner.stop_prewarm()
        super().tearDown()

    def test_prewarm_enabled_needs_all_three_flags(self):
        self.assertTrue(kospi200_scanner.prewarm_enabled())
        for name in (kospi200_scanner.PREWARM_ENV, kospi200_scanner.PREWARM_MASTER_ENV,
                     kospi200_scanner.PREWARM_LEGACY_ENV):
            with patch.dict(os.environ, {name: "0"}):
                self.assertFalse(kospi200_scanner.prewarm_enabled(), name)

    def test_first_delay_is_after_sp500_and_interval_under_ttl(self):
        self.assertGreater(kospi200_scanner.PREWARM_FIRST_DELAY,
                           sp500_scanner.PREWARM_FIRST_DELAY)
        self.assertLess(kospi200_scanner.PREWARM_INTERVAL, kospi200_scanner.PRESCAN_TTL)

    def test_background_thread_warms_the_snapshot(self):
        self.assertFalse(kospi200_scanner.cache_status()["warm"])
        started = kospi200_scanner.start_prewarm(first_delay=0.01, interval=3600)
        self.assertTrue(started)
        self.assertTrue(wait_until(lambda: kospi200_scanner.cache_status()["warm"]))
        status = kospi200_scanner.cache_status()
        self.assertEqual(status["tickers"], 1)
        self.assertTrue(status["fresh"])
        self.assertIsNotNone(status["prewarm_last_ok_sec"])
        self.assertEqual(len(self.scan_calls), 1)

    def test_start_is_idempotent_and_stop_ends_the_thread(self):
        self.assertTrue(kospi200_scanner.start_prewarm(first_delay=3600, interval=3600))
        self.assertFalse(kospi200_scanner.start_prewarm(first_delay=3600, interval=3600))
        self.assertTrue(kospi200_scanner.cache_status()["prewarm_running"])
        kospi200_scanner.stop_prewarm()
        self.assertFalse(kospi200_scanner.cache_status()["prewarm_running"])
        self.assertIsNone(kospi200_scanner._PREWARM["thread"])

    def test_disabled_flag_starts_nothing(self):
        with patch.dict(os.environ, {kospi200_scanner.PREWARM_ENV: "0"}):
            self.assertFalse(kospi200_scanner.start_prewarm(first_delay=0.01, interval=3600))
            time.sleep(0.1)
            self.assertFalse(kospi200_scanner.cache_status()["warm"])
        self.assertEqual(self.scan_calls, [])

    def test_legacy_sp500_flag_disables_korean_prewarm(self):
        with patch.dict(os.environ, {kospi200_scanner.PREWARM_LEGACY_ENV: "0"}):
            self.assertFalse(kospi200_scanner.start_prewarm(first_delay=0.01, interval=3600))

    def test_force_starts_even_when_disabled(self):
        with patch.dict(os.environ, {kospi200_scanner.PREWARM_ENV: "0"}):
            self.assertTrue(kospi200_scanner.start_prewarm(first_delay=3600, interval=3600, force=True))
        self.assertTrue(kospi200_scanner.cache_status()["prewarm_running"])

    def test_prewarm_skips_a_fresh_snapshot(self):
        kospi200_scanner.prescan(force_refresh=True)
        calls = len(self.scan_calls)
        result = kospi200_scanner.prewarm_once()
        self.assertFalse(result["scanned"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.scan_calls), calls)

    def test_prewarm_waits_for_sp500_scan_instead_of_overlapping(self):
        self.patch(sp500_scanner, "is_scanning", lambda: True)
        self.patch(kospi200_scanner, "PREWARM_OVERLAP_WAIT", 0)
        result = kospi200_scanner.prewarm_once(wait_for_sp500=True)
        self.assertFalse(result["scanned"])
        self.assertEqual(result["error"], "sp500_scanning")
        self.assertEqual(self.scan_calls, [])

    def test_prewarm_failure_keeps_previous_snapshot(self):
        kospi200_scanner.prescan(force_refresh=True)
        kospi200_scanner._SNAPSHOT["ts"] = time.time() - (kospi200_scanner.PREWARM_REFRESH_AGE + 60)
        self.patch(kospi200_scanner, "_scan_universe",
                   lambda t=None: {"rows": [], "universe": 1, "skipped": 1})
        result = kospi200_scanner.prewarm_once()
        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(kospi200_scanner.cache_status()["tickers"], 1)
        self.assertEqual([r["ticker"] for r in kospi200_scanner.prescan()["rows"]], ["005930.KS"])


if __name__ == "__main__":
    unittest.main()
