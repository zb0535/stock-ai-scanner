"""S&P 500 자동 발굴(`sp500_scanner`)과 `/api/screen` `mode="sp500"` 통합 테스트.

검증 대상
---------
1. 청크 배치 다운로드 — 250종목이면 100/100/50 세 번, 파라미터(6mo·1d·group_by=ticker) 고정
2. 누락·NaN·짧은 이력 종목 허용 — 있는 종목만 남고 예외 없음, 다운로드 전면 실패도 견딤
3. 70분 스냅샷 캐시 — 두 번째 조회는 다운로드 0회, 만료 뒤에는 재스캔, 실패 시 직전 스냅샷 유지
4. 전략별 상위 선정 — 과매도/모멘텀/가치(프리 스캔 프록시)마다 1등이 다르고 top_n으로 자름
5. `/api/screen` — `mode="sp500"`은 발굴된 15종목으로 파이프라인을 돌리고, 기존 `watchlist`는 그대로
6. 백그라운드 예열 — 기동 후 자동 예열·`SP500_PREWARM=0` 비활성·중복 스캔 방지·실패 시 스냅샷 유지,
   `GET /`의 `sp500_cache` 요약이 스캔 중에도 즉시 응답

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
import scoring  # noqa: E402
import sp500_scanner  # noqa: E402
import stock_tools  # noqa: E402

try:  # TestClient는 httpx에 의존한다.
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


# --- 데이터 도우미 -----------------------------------------------------------

def hist(close, volume=1_000_000.0):
    """종가 배열로 OHLCV 일봉 DataFrame을 만든다(High/Low=종가, 거래량 일정)."""
    close = np.asarray(close, dtype="float64")
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-18"), periods=len(close))
    vol = volume if isinstance(volume, (list, tuple, np.ndarray)) else [volume] * len(close)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close,
         "Volume": np.asarray(vol, dtype="float64")},
        index=idx,
    )


def uptrend(days=130):
    return hist(np.linspace(100.0, 150.0, days))


def washed_out(days=130):
    """6개월 고점에서 40% 빠지고 RSI가 바닥인 종목(과매도 1등감)."""
    return hist(np.linspace(200.0, 120.0, days))


def breakout(days=130):
    """정배열 + 거래량 급증 + 신고가권 종목(모멘텀 1등감)."""
    close = np.linspace(80.0, 130.0, days)
    vol = [1_000_000.0] * (days - 1) + [3_000_000.0]
    return hist(close, volume=vol)


def quiet_uptrend(days=130):
    return hist(np.linspace(100.0, 105.0, days))


def batch_from(frames: dict):
    """{티커: OHLCV} → yf.download(group_by="ticker") 모양의 MultiIndex DataFrame."""
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


class FakeDownload:
    """`sp500_scanner.yf.download` 대역. 호출된 티커 목록을 기록한다."""

    def __init__(self, frames: dict, fail: bool = False):
        self.frames = frames
        self.fail = fail
        self.calls = []

    def __call__(self, symbols, **kwargs):
        self.calls.append({"symbols": list(symbols), "kwargs": dict(kwargs)})
        if self.fail:
            raise RuntimeError("429 Too Many Requests")
        picked = {sym: self.frames[sym] for sym in symbols if sym in self.frames}
        return batch_from(picked)


class ScannerTestBase(unittest.TestCase):
    def setUp(self):
        sp500_scanner.reset_cache()

    def tearDown(self):
        sp500_scanner.reset_cache()

    def patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def scan_with(self, frames, tickers, fail=False):
        fake = FakeDownload(frames, fail=fail)
        self.patch(sp500_scanner.yf, "download", fake)
        self.patch(sp500_scanner, "get_sp500_tickers", lambda force_refresh=False: list(tickers))
        return fake


# --- 1. 청크 배치 다운로드 ---------------------------------------------------

class ChunkedDownloadTests(ScannerTestBase):
    def test_250_tickers_are_downloaded_in_100_ticker_chunks(self):
        symbols = [f"T{i:03d}" for i in range(250)]
        frames = {sym: uptrend() for sym in symbols}
        fake = self.scan_with(frames, symbols)

        result = sp500_scanner.prescan(force_refresh=True)

        self.assertEqual([len(c["symbols"]) for c in fake.calls], [100, 100, 50])
        self.assertEqual(result["universe"], 250)
        self.assertEqual(len(result["rows"]), 250)
        self.assertFalse(result["cached"])
        for call in fake.calls:
            kwargs = call["kwargs"]
            self.assertEqual(kwargs["period"], "6mo")
            self.assertEqual(kwargs["interval"], "1d")
            self.assertEqual(kwargs["group_by"], "ticker")
            self.assertIs(kwargs["threads"], True)
            self.assertIs(kwargs["progress"], False)

    def test_chunk_request_never_exceeds_chunk_size(self):
        symbols = [f"T{i:03d}" for i in range(503)]
        fake = self.scan_with({sym: uptrend() for sym in symbols}, symbols)
        sp500_scanner.prescan(force_refresh=True)
        self.assertTrue(all(len(c["symbols"]) <= sp500_scanner.CHUNK_SIZE for c in fake.calls))
        self.assertEqual(sum(len(c["symbols"]) for c in fake.calls), 503)


# --- 2. 누락·NaN 허용 -------------------------------------------------------

class ToleranceTests(ScannerTestBase):
    def test_nan_and_missing_tickers_are_skipped_without_raising(self):
        nan_frame = uptrend().copy()
        nan_frame["Close"] = np.nan  # 상장 직후/거래정지처럼 종가가 전부 비어 있는 경우
        frames = {"GOOD": uptrend(), "NAN": nan_frame}
        self.scan_with(frames, ["GOOD", "NAN", "MISSING"])

        result = sp500_scanner.prescan(force_refresh=True)

        self.assertEqual([r["ticker"] for r in result["rows"]], ["GOOD"])
        self.assertEqual(result["universe"], 3)
        self.assertEqual(result["skipped"], 2)

    def test_partial_nan_rows_are_dropped(self):
        frame = uptrend().copy()
        frame.iloc[10:15, frame.columns.get_loc("Close")] = np.nan
        self.scan_with({"PART": frame}, ["PART"])
        rows = sp500_scanner.prescan(force_refresh=True)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(np.isfinite(rows[0]["price"]))

    def test_short_history_is_skipped(self):
        self.scan_with({"SHORT": uptrend(days=10)}, ["SHORT"])
        result = sp500_scanner.prescan(force_refresh=True)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["skipped"], 1)

    def test_total_download_failure_returns_empty_and_does_not_raise(self):
        fake = self.scan_with({}, ["AAA", "BBB"], fail=True)
        result = sp500_scanner.prescan(force_refresh=True)
        self.assertEqual(result["rows"], [])
        self.assertEqual(len(fake.calls), 1)  # 한 청크에서 실패해도 예외 없이 다음 청크로
        self.assertEqual(sp500_scanner.select_candidates("눌림목")["tickers"], [])

    def test_zero_or_negative_price_is_skipped(self):
        frame = uptrend()
        frame["Close"] = 0.0
        frame["High"] = 0.0
        frame["Low"] = 0.0
        frame["Open"] = 0.0
        self.scan_with({"ZERO": frame}, ["ZERO"])
        self.assertEqual(sp500_scanner.prescan(force_refresh=True)["rows"], [])


# --- 3. 1시간 캐시 -----------------------------------------------------------

class CacheTests(ScannerTestBase):
    def test_second_scan_within_ttl_hits_cache(self):
        self.scan_with({"AAA": uptrend(), "BBB": washed_out()}, ["AAA", "BBB"])
        first = sp500_scanner.select_candidates("단기 눌림목 반등")
        fake = sp500_scanner.yf.download
        calls_after_first = len(fake.calls)
        second = sp500_scanner.select_candidates("모멘텀 돌파")

        self.assertEqual(len(fake.calls), calls_after_first)  # 추가 다운로드 0회
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["universe_scanned"], 2)
        # 전략 랭킹은 같은 스냅샷 위에서 매번 다시 계산된다
        self.assertEqual(first["strategy"], "oversold")
        self.assertEqual(second["strategy"], "momentum")

    def test_snapshot_expires_after_one_hour(self):
        fake = self.scan_with({"AAA": uptrend()}, ["AAA"])
        sp500_scanner.prescan(force_refresh=True)
        calls = len(fake.calls)
        sp500_scanner._SNAPSHOT["ts"] = time.time() - (sp500_scanner.PRESCAN_TTL + 5)

        result = sp500_scanner.prescan()

        self.assertEqual(len(fake.calls), calls + 1)
        self.assertFalse(result["cached"])

    def test_failed_rescan_keeps_previous_snapshot(self):
        frames = {"AAA": uptrend()}
        self.scan_with(frames, ["AAA"])
        sp500_scanner.prescan(force_refresh=True)
        sp500_scanner._SNAPSHOT["ts"] = time.time() - (sp500_scanner.PRESCAN_TTL + 5)
        sp500_scanner.yf.download = FakeDownload({}, fail=True)

        result = sp500_scanner.prescan()

        self.assertEqual([r["ticker"] for r in result["rows"]], ["AAA"])
        self.assertTrue(result["cached"])
        self.assertTrue(result["stale"])

    def test_reset_cache_clears_snapshot(self):
        self.scan_with({"AAA": uptrend()}, ["AAA"])
        sp500_scanner.prescan(force_refresh=True)
        sp500_scanner.reset_cache()
        self.assertEqual(sp500_scanner._SNAPSHOT["rows"], [])


# --- 4. 전략별 상위 선정 -----------------------------------------------------

class RankingTests(ScannerTestBase):
    def test_detect_strategy_drives_the_winner(self):
        rows = [
            {"ticker": "BREAK", "price": 130.0, "ma20": 120.0, "ma60": 100.0,
             "rsi14": 63.0, "bb_pct_b": 0.9, "volume_ratio": 3.0, "macd_cross": "bullish",
             "drawdown_from_high_pct": -0.5, "range_position_pct": 98.0},
            {"ticker": "WASHED", "price": 120.0, "ma20": 150.0, "ma60": 175.0,
             "rsi14": 22.0, "bb_pct_b": -0.2, "volume_ratio": 2.2, "macd_cross": "none",
             "drawdown_from_high_pct": -40.0, "range_position_pct": 3.0},
            {"ticker": "QUIET", "price": 102.0, "ma20": 101.0, "ma60": 99.0,
             "rsi14": 52.0, "bb_pct_b": 0.55, "volume_ratio": 1.0, "macd_cross": "none",
             "drawdown_from_high_pct": -4.0, "range_position_pct": 70.0},
        ]
        self.assertEqual(sp500_scanner.rank_rows(rows, "oversold")[0]["ticker"], "WASHED")
        self.assertEqual(sp500_scanner.rank_rows(rows, "momentum")[0]["ticker"], "BREAK")
        self.assertEqual(sp500_scanner.rank_rows(rows, "balanced")[0]["ticker"], "BREAK")

    def test_top_n_caps_candidates_and_keeps_order(self):
        rows = [{"ticker": f"T{i:02d}", "rsi14": 20.0 + i} for i in range(20)]
        top = sp500_scanner.rank_rows(rows, "oversold", top_n=15)
        self.assertEqual(len(top), 15)
        self.assertEqual([r["ticker"] for r in top[:2]], ["T00", "T01"])
        self.assertEqual(len(sp500_scanner.rank_rows(rows, "oversold")), 15)  # 기본 top_n

    def test_select_candidates_reports_universe_and_scanned(self):
        frames = {"WASHED": washed_out(), "BREAK": breakout(), "NAN": uptrend()}
        frames["NAN"]["Close"] = np.nan
        self.scan_with(frames, ["WASHED", "BREAK", "NAN"])

        result = sp500_scanner.select_candidates("단기 눌림목 반등")

        self.assertEqual(result["strategy"], "oversold")
        self.assertEqual(result["universe_scanned"], 3)
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["tickers"][0], "WASHED")
        self.assertEqual(set(result["tickers"]), {"WASHED", "BREAK"})
        self.assertIn("prescore", result["candidates"][0])

    def test_value_prescan_prefers_quality_pullback(self):
        """가치 조건은 '적당한 눌림 + 추세 유지'를 먼저 보고, 붕괴/과열은 뒤로 보낸다."""
        quality_pullback = {"ticker": "QUAL", "price": 92.0, "ma20": 95.0, "ma60": 96.0,
                            "rsi14": 38.0, "bb_pct_b": 0.2, "volume_ratio": 1.2,
                            "drawdown_from_high_pct": -12.0, "range_position_pct": 42.0}
        crashed = {"ticker": "CRASH", "price": 55.0, "ma20": 70.0, "ma60": 92.0,
                   "rsi14": 14.0, "bb_pct_b": -0.3, "volume_ratio": 2.5,
                   "drawdown_from_high_pct": -55.0, "range_position_pct": 1.0}
        overheated = {"ticker": "HOT", "price": 140.0, "ma20": 130.0, "ma60": 110.0,
                      "rsi14": 76.0, "bb_pct_b": 1.1, "volume_ratio": 1.0,
                      "drawdown_from_high_pct": -0.5, "range_position_pct": 97.0}

        ranked = sp500_scanner.rank_rows([crashed, overheated, quality_pullback], "value")

        self.assertEqual(ranked[0]["ticker"], "QUAL")
        self.assertEqual(ranked[-1]["ticker"], "CRASH")  # 고점 -55% 붕괴는 가치 진입 최악
        scores = [r["prescore"] for r in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_value_prescan_uses_technical_signals_not_missing_fundamentals(self):
        score, signals = scoring.score_prescan(
            {"ticker": "X", "price": 92.0, "ma60": 96.0, "rsi14": 36.0,
             "drawdown_from_high_pct": -14.0, "range_position_pct": 40.0}, "value")
        self.assertGreater(score, 50.0)
        self.assertTrue(any("눌림" in s for s in signals))
        # 다른 전략은 기존 score_stock과 동일해야 한다(회귀 방지)
        row = {"ticker": "Y", "rsi14": 24.0, "bb_pct_b": -0.2, "drawdown_from_high_pct": -30.0}
        self.assertEqual(scoring.score_prescan(row, "oversold"), scoring.score_stock(row, "oversold"))


# --- 5. /api/screen 통합 -----------------------------------------------------

def stock_row(ticker):
    return {"ticker": ticker, "name": f"{ticker} Inc.", "price": 120.0,
            "ma20": 118.0, "ma60": 110.0, "rsi14": 41.0,
            "trend": "상승 추세(정배열: 현재가>20일선>60일선)",
            "macd": 1.2, "macd_signal": 0.8, "macd_hist": 0.4, "macd_cross": "bullish",
            "macd_state": "above", "bb_upper": 130.0, "bb_lower": 110.0, "bb_pct_b": 0.5,
            "volume_ratio": 1.4, "drawdown_from_high_pct": -9.0, "range_position_pct": 55.0,
            "trailing_pe": 18.0, "forward_pe": 16.0, "revenue_growth": 0.11,
            "operating_margins": 0.22, "debt_to_equity": 45.0,
            "financials_available": True, "news": [f"{ticker} headline"]}


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


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ScreenModeTests(unittest.TestCase):
    DISCOVERED = [f"D{i:02d}" for i in range(15)]

    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        self._patches = []
        self.patch_attr(api, "_get_recent_news", lambda ticker, limit=3: [f"{ticker} headline"])
        self.patch_attr(stock_tools, "get_history", lambda ticker, period="6mo": uptrend())
        self.patch_attr(stock_tools, "get_stock_metrics", stock_row)
        self.discovery_calls = []
        self.discovery_response = None
        self.patch_attr(api.sp500_scanner, "select_candidates", self._fake_discovery)
        self.fake_ai = FakeGenai({"market_note": "요약", "top_picks": [
            {"ticker": self.DISCOVERED[0], "price": 120.0, "reason": "근거", "risk": "위험"}]})
        self.patch_attr(api.genai, "Client", lambda: self.fake_ai)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def patch_attr(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self._patches.append(patcher)
        return value

    def _fake_discovery(self, criteria=None, top_n=15, force_refresh=False):
        self.discovery_calls.append({"criteria": criteria, "top_n": top_n})
        if self.discovery_response is not None:
            return self.discovery_response
        return {"tickers": list(self.DISCOVERED), "strategy": scoring.detect_strategy(criteria),
                "universe_scanned": 503, "scanned": 498, "skipped": 5,
                "cached": False, "stale": False, "candidates": []}

    def test_sp500_mode_scans_universe_and_screens_top_15(self):
        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "단기 눌림목 반등", "mode": "sp500"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["mode"], "sp500")
        self.assertEqual(body["universe_scanned"], 503)
        self.assertEqual(body["strategy"], "oversold")
        self.assertEqual([r["ticker"] for r in body["all_stocks"]], self.DISCOVERED)
        self.assertEqual(len(body["all_stocks"]), 15)
        self.assertEqual(self.discovery_calls, [{"criteria": "단기 눌림목 반등", "top_n": 15}])
        # 발굴된 종목에도 기존 파이프라인 필드(재무·뉴스·차트)가 그대로 붙는다
        first = body["all_stocks"][0]
        for key in ("ticker", "name", "price", "ma20", "rsi14", "trailing_pe", "news", "quant_score"):
            self.assertIn(key, first)
        pick = body["analysis"]["top_picks"][0]
        self.assertEqual(len(pick["chart"]), 60)
        self.assertIn("news", pick)
        prompt = self.fake_ai.models.calls[0]["contents"]
        self.assertIn("S&P 500 503종목", prompt)
        self.assertIn("D00", prompt)

    def test_watchlist_mode_is_unchanged_and_never_scans(self):
        body = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주", "tickers": ["AAPL", "MSFT"]}).json()

        self.assertEqual(body["mode"], "watchlist")
        self.assertIsNone(body["universe_scanned"])
        self.assertEqual([r["ticker"] for r in body["all_stocks"]], ["AAPL", "MSFT"])
        self.assertEqual(self.discovery_calls, [])
        self.assertEqual(sorted(body), ["all_stocks", "analysis", "mode", "strategy", "universe_scanned"])

    def test_unknown_mode_falls_back_to_watchlist(self):
        body = TestClient(api.app).post(
            "/api/screen", json={"criteria": "모멘텀", "tickers": ["AAPL"], "mode": "nasdaq"}).json()
        self.assertEqual(body["mode"], "watchlist")
        self.assertEqual(self.discovery_calls, [])

    def test_sp500_mode_with_no_candidates_falls_back_to_watchlist(self):
        self.discovery_response = {
            "tickers": [], "strategy": "balanced", "universe_scanned": 503,
            "scanned": 0, "skipped": 503, "cached": False, "stale": False, "candidates": [],
        }
        body = TestClient(api.app).post(
            "/api/screen", json={"criteria": "눌림목", "mode": "sp500"}).json()

        self.assertEqual(body["mode"], "sp500")
        self.assertEqual(body["universe_scanned"], 0)
        self.assertEqual(len(body["all_stocks"]), len(stock_tools.DEFAULT_WATCHLIST))


class Sp500ListTests(ScannerTestBase):
    def test_wikipedia_failure_uses_builtin_fallback(self):
        self.patch(sp500_scanner.requests, "get",
                   lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
        table = sp500_scanner.get_sp500_table(force_refresh=True)
        self.assertEqual(table["source"], "fallback")
        self.assertEqual(len(table["rows"]), len(sp500_scanner.FALLBACK_TICKERS))
        self.assertIn("AAPL", sp500_scanner.get_sp500_tickers())

    def test_api_sp500_never_returns_empty_list(self):
        self.patch(sp500_scanner.requests, "get",
                   lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
        resp = TestClient(api.app).get("/api/sp500")
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(len(body["results"]), 400)
        self.assertEqual(set(body["results"][0]), {"symbol", "name", "exchange"})


# --- 6. 백그라운드 예열 ------------------------------------------------------

def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """`predicate()`가 참이 될 때까지 짧게 기다린다(스레드 테스트용)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class PrewarmTests(ScannerTestBase):
    """예열 스레드: 기동 후 자동 예열·플래그 비활성·중복 스캔 방지·실패 견딤."""

    def setUp(self):
        super().setUp()
        sp500_scanner.stop_prewarm()
        self._env = patch.dict(os.environ, {sp500_scanner.PREWARM_ENV: "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self):
        sp500_scanner.stop_prewarm()
        super().tearDown()

    def test_prewarm_enabled_reads_the_env_flag(self):
        self.assertTrue(sp500_scanner.prewarm_enabled())  # 미설정·빈 값·1은 켜짐
        for raw in ("0", "false", "FALSE", "no", "off"):
            with patch.dict(os.environ, {sp500_scanner.PREWARM_ENV: raw}):
                self.assertFalse(sp500_scanner.prewarm_enabled(), raw)
        for raw in ("1", "true", "on", ""):
            with patch.dict(os.environ, {sp500_scanner.PREWARM_ENV: raw}):
                self.assertTrue(sp500_scanner.prewarm_enabled(), raw)

    def test_background_thread_warms_the_snapshot_after_the_first_delay(self):
        fake = self.scan_with({"AAA": uptrend(), "BBB": washed_out()}, ["AAA", "BBB"])
        self.assertFalse(sp500_scanner.cache_status()["warm"])

        started = sp500_scanner.start_prewarm(first_delay=0.01, interval=3600)

        self.assertTrue(started)
        self.assertTrue(wait_until(lambda: sp500_scanner.cache_status()["warm"]))
        status = sp500_scanner.cache_status()
        self.assertEqual(status["tickers"], 2)
        self.assertTrue(status["fresh"])
        self.assertGreaterEqual(status["age_sec"], 0)
        self.assertIsNotNone(status["prewarm_last_ok_sec"])
        self.assertEqual(len(fake.calls), 1)  # 예열이 500종목 스캔을 한 번 돌렸다

    def test_start_is_idempotent_and_stop_ends_the_thread(self):
        self.scan_with({"AAA": uptrend()}, ["AAA"])
        self.assertTrue(sp500_scanner.start_prewarm(first_delay=3600, interval=3600))
        self.assertFalse(sp500_scanner.start_prewarm(first_delay=3600, interval=3600))
        self.assertTrue(sp500_scanner.cache_status()["prewarm_running"])

        sp500_scanner.stop_prewarm()

        self.assertFalse(sp500_scanner.cache_status()["prewarm_running"])
        self.assertIsNone(sp500_scanner._PREWARM["thread"])

    def test_flag_zero_disables_prewarming_without_scanning(self):
        fake = self.scan_with({"AAA": uptrend()}, ["AAA"])
        with patch.dict(os.environ, {sp500_scanner.PREWARM_ENV: "0"}):
            started = sp500_scanner.start_prewarm(first_delay=0.01, interval=3600)
            time.sleep(0.15)
            status = sp500_scanner.cache_status()

        self.assertFalse(started)
        self.assertFalse(status["prewarm_enabled"])
        self.assertFalse(status["prewarm_running"])
        self.assertFalse(status["warm"])
        self.assertEqual(fake.calls, [])

    def test_force_flag_starts_even_when_disabled(self):
        self.scan_with({"AAA": uptrend()}, ["AAA"])
        with patch.dict(os.environ, {sp500_scanner.PREWARM_ENV: "0"}):
            started = sp500_scanner.start_prewarm(first_delay=3600, interval=3600, force=True)
        self.assertTrue(started)
        self.assertTrue(sp500_scanner.cache_status()["prewarm_running"])

    def test_prewarm_skips_a_snapshot_that_is_still_fresh(self):
        fake = self.scan_with({"AAA": uptrend()}, ["AAA"])
        sp500_scanner.prescan(force_refresh=True)
        calls = len(fake.calls)

        result = sp500_scanner.prewarm_once()

        self.assertFalse(result["scanned"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.calls), calls)  # 다운로드 추가 0회

    def test_prewarm_refreshes_before_the_ttl_expires(self):
        """예열 주기(50분)가 TTL(70분)보다 짧아, 사용자가 만료된 캐시를 맞지 않는다."""
        fake = self.scan_with({"AAA": uptrend()}, ["AAA"])
        sp500_scanner.prescan(force_refresh=True)
        self.assertLess(sp500_scanner.PREWARM_INTERVAL, sp500_scanner.PRESCAN_TTL)
        sp500_scanner._SNAPSHOT["ts"] = time.time() - (sp500_scanner.PREWARM_REFRESH_AGE + 60)

        result = sp500_scanner.prewarm_once()

        self.assertTrue(result["scanned"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.calls), 2)
        self.assertLess(sp500_scanner.snapshot_age(), 5)

    def test_prewarm_failure_keeps_the_previous_snapshot(self):
        self.scan_with({"AAA": uptrend()}, ["AAA"])
        sp500_scanner.prescan(force_refresh=True)
        sp500_scanner._SNAPSHOT["ts"] = time.time() - (sp500_scanner.PREWARM_REFRESH_AGE + 60)
        sp500_scanner.yf.download = FakeDownload({}, fail=True)

        result = sp500_scanner.prewarm_once()

        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(sp500_scanner.cache_status()["tickers"], 1)
        self.assertEqual([r["ticker"] for r in sp500_scanner.prescan()["rows"]], ["AAA"])

    def test_user_scan_and_prewarm_overlap_runs_a_single_scan(self):
        """사용자 스캔이 도는 중에 예열이 겹치면, `_SCAN_LOCK` 뒤 재검사로 두 번 받지 않는다."""
        calls = []
        started = threading.Event()
        release = threading.Event()

        def slow_scan(tickers=None):
            calls.append(1)
            started.set()
            release.wait(5)
            return {"rows": [{"ticker": "AAA"}], "universe": 1, "skipped": 0}

        self.patch(sp500_scanner, "_scan_universe", slow_scan)
        user = threading.Thread(target=sp500_scanner.prescan, kwargs={"force_refresh": True},
                                daemon=True)
        user.start()
        self.assertTrue(started.wait(2))

        outcome = {}
        warmer = threading.Thread(target=lambda: outcome.update(sp500_scanner.prewarm_once()),
                                  daemon=True)
        warmer.start()
        time.sleep(0.05)
        release.set()
        user.join(5)
        warmer.join(5)

        self.assertEqual(len(calls), 1)  # 500종목을 두 번 받지 않았다
        self.assertFalse(outcome["scanned"])
        self.assertTrue(outcome["ok"])
        self.assertEqual(sp500_scanner.cache_status()["tickers"], 1)


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class HealthCacheStatusTests(ScannerTestBase):
    """`GET /`의 `sp500_cache` 요약 — 스캔 중에도 막히지 않는다."""

    def setUp(self):
        super().setUp()
        sp500_scanner.stop_prewarm()
        self._env = patch.dict(os.environ, {sp500_scanner.PREWARM_ENV: "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self):
        sp500_scanner.stop_prewarm()
        super().tearDown()

    def test_health_reports_an_empty_cache(self):
        cache = TestClient(api.app).get("/").json()["sp500_cache"]
        self.assertFalse(cache["warm"])
        self.assertIsNone(cache["age_sec"])
        self.assertEqual(cache["tickers"], 0)
        self.assertTrue(cache["prewarm_enabled"])
        self.assertEqual(cache["ttl_sec"], sp500_scanner.PRESCAN_TTL)

    def test_health_reports_warm_cache_with_tickers_and_age(self):
        self.scan_with({"AAA": uptrend(), "BBB": washed_out()}, ["AAA", "BBB"])
        sp500_scanner.prescan(force_refresh=True)

        cache = TestClient(api.app).get("/").json()["sp500_cache"]

        self.assertTrue(cache["warm"])
        self.assertTrue(cache["fresh"])
        self.assertEqual(cache["tickers"], 2)
        self.assertIsInstance(cache["age_sec"], int)
        self.assertGreaterEqual(cache["age_sec"], 0)

    def test_health_answers_immediately_while_a_scan_is_running(self):
        self.scan_with({"AAA": uptrend()}, ["AAA"])
        sp500_scanner.prescan(force_refresh=True)
        release = threading.Event()
        started = threading.Event()

        def blocking_scan(tickers=None):
            started.set()
            release.wait(5)
            return {"rows": [{"ticker": "BBB"}], "universe": 1, "skipped": 0}

        self.patch(sp500_scanner, "_scan_universe", blocking_scan)
        scan = threading.Thread(target=sp500_scanner.prescan, kwargs={"force_refresh": True},
                                daemon=True)
        scan.start()
        self.assertTrue(started.wait(2))
        try:
            began = time.time()
            body = TestClient(api.app).get("/").json()
            elapsed = time.time() - began
        finally:
            release.set()
            scan.join(5)

        self.assertLess(elapsed, 1.0)  # 스캔(수 분)이 끝나기를 기다리지 않았다
        self.assertTrue(body["sp500_cache"]["warm"])
        self.assertEqual(body["sp500_cache"]["tickers"], 1)  # 직전 스냅샷을 그대로 보고

    def test_lifespan_starts_and_stops_the_prewarmer(self):
        with patch.object(sp500_scanner, "PREWARM_FIRST_DELAY", 3600):  # 테스트 중 실제 스캔 금지
            with TestClient(api.app) as client:
                self.assertTrue(client.get("/").json()["sp500_cache"]["prewarm_running"])
            self.assertFalse(sp500_scanner.cache_status()["prewarm_running"])

    def test_lifespan_respects_the_disable_flag(self):
        with patch.dict(os.environ, {sp500_scanner.PREWARM_ENV: "0"}):
            with TestClient(api.app) as client:
                cache = client.get("/").json()["sp500_cache"]
                self.assertFalse(cache["prewarm_running"])
                self.assertFalse(cache["prewarm_enabled"])


if __name__ == "__main__":
    unittest.main()
