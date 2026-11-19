"""NaN 안전성 회귀 테스트 — 2026-11 한국 종목 HTTP 500 사고.

사고 경과
---------
야후 파이낸스가 한국 종목(005930.KS) 일봉의 **마지막 행을 OHLC 전부 NaN, 거래량만 있는
자리표시자 행**으로 내려줬다. `get_history`가 그 행을 그대로 넘기면서 현재가·20일선·RSI가
NaN이 되었고, Starlette의 `JSONResponse`는 `allow_nan=False`로 직렬화하므로 NaN 하나가
섞이자 `ValueError: Out of range float values are not JSON compliant: nan` →
`/api/screen`·`/api/stock/{ticker}`가 평문 500이 됐다.

여기서 지키는 것
- 종가가 빈 행은 지표 계산·캐시에서 빠진다(근본 수정).
- 그래도 NaN이 남아도 응답은 500이 아니라 `null`로 나간다(마지막 방어선).
- Gemini가 실패해도 200 + 서버 점수 기반 폴백 분석을 돌려준다.
- 처리되지 않은 예외는 JSON 500 + `error_id`로 남고 `GET /`에서 확인할 수 있다.

실행: python -m unittest discover -s tests -v
"""
import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import naver_finance  # noqa: E402
import stock_tools  # noqa: E402

try:  # TestClient는 httpx에 의존한다.
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


def make_hist(days: int = 130, nan_tail: int = 0, last_close_nan: bool = False):
    """종가 시리즈를 가진 일봉. `nan_tail`만큼 끝에 자리표시자(전부 NaN) 행을 붙인다."""
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-19"), periods=days + nan_tail)
    close = list(np.linspace(100.0, 150.0, days))
    frames = []
    for value in close:
        frames.append({"Open": value - 0.5, "High": value + 1, "Low": value - 1,
                       "Close": value, "Volume": 1_000_000})
    for _ in range(nan_tail):
        frames.append({"Open": float("nan"), "High": float("nan"), "Low": float("nan"),
                       "Close": float("nan"), "Volume": 500_000})
    if last_close_nan and not nan_tail:  # 종가만 비고 거래량은 남은 실제 사고 모양
        frames[-1] = dict(frames[-1], Open=float("nan"), **{"Close": float("nan")})
    return pd.DataFrame(frames, index=idx)


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeGenai:
    """`genai.Client` 대역 — 고정 JSON을 돌려준다."""

    def __init__(self, payload):
        self.payload = payload
        self.models = self

    def generate_content(self, **_kwargs):
        return FakeResponse("```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```")


class RaisingGenai:
    """Gemini 쿼터 초과·장애를 흉내낸다."""

    def __init__(self):
        self.models = self

    def generate_content(self, **_kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")


class GarbageGenai:
    """모델이 JSON이 아닌 답을 준 경우."""

    def __init__(self):
        self.models = self

    def generate_content(self, **_kwargs):
        return FakeResponse("죄송합니다. 지금은 분석을 제공할 수 없습니다.")


class FakeTicker:
    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        return {"shortName": f"{self.symbol} Inc.", "trailingPE": 12.5}

    def history(self, period="6mo"):
        # 한국 종목에만 사고 모양(마지막 종가 NaN)을 준다.
        nan_tail = 1 if self.symbol.upper().endswith((".KS", ".KQ")) else 0
        return make_hist(nan_tail=nan_tail)


def assert_no_nonfinite(testcase, value, path="$"):
    """응답 어디에도 NaN/inf가 없어야 한다(JSON 표준 위반 = 500의 원인)."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_nonfinite(testcase, item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            assert_no_nonfinite(testcase, item, f"{path}[{i}]")
    elif isinstance(value, float):
        testcase.assertTrue(math.isfinite(value), f"{path}에 NaN/inf: {value}")


class BaseCase(unittest.TestCase):
    def patch(self, target, name, value):
        from unittest.mock import patch as mock_patch
        patcher = mock_patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        stock_tools._info_fail_streak = 0
        stock_tools._info_cooldown_until = 0.0
        naver_finance.clear_cache()
        # 실네트워크를 타지 않게 막는다.
        self.patch(stock_tools.sec_edgar, "get_financial_metrics", lambda *a, **k: {})
        self.patch(naver_finance, "get_fundamentals", lambda *a, **k: {})
        self.patch(naver_finance, "get_korean_news", lambda *a, **k: [])


# --- 1. 데이터 계층: 종가 빈 행 제거 -----------------------------------------

class CleanHistoryTests(BaseCase):
    def test_trailing_nan_row_is_dropped(self):
        hist = make_hist(nan_tail=1)
        cleaned = stock_tools.clean_history(hist)
        self.assertEqual(len(cleaned), len(hist) - 1)
        self.assertFalse(pd.isna(cleaned["Close"].iloc[-1]))
        self.assertEqual(cleaned["Close"].iloc[-1], 150.0)

    def test_healthy_history_is_returned_unchanged(self):
        hist = make_hist()
        self.assertIs(stock_tools.clean_history(hist), hist)  # 복사·변형 없음

    def test_none_and_empty_are_safe(self):
        self.assertIsNone(stock_tools.clean_history(None))
        empty = pd.DataFrame()
        self.assertIs(stock_tools.clean_history(empty), empty)

    def test_get_history_never_returns_nan_close(self):
        self.patch(stock_tools.yf, "Ticker", FakeTicker)
        hist = stock_tools.get_history("005930.KS")
        self.assertFalse(pd.isna(hist["Close"].iloc[-1]))
        # 캐시에도 정리된 프레임이 들어간다(30분 캐시가 NaN을 되살리지 않게).
        cached = stock_tools._HISTORY_CACHE["005930.KS:6mo"][1]
        self.assertFalse(pd.isna(cached["Close"].iloc[-1]))


class MetricsNanTests(BaseCase):
    def test_price_is_last_valid_close_not_nan(self):
        self.patch(stock_tools.yf, "Ticker", FakeTicker)
        row = stock_tools.get_stock_metrics("005930.KS")
        self.assertEqual(row["price"], 150.0)
        self.assertIsNotNone(row["ma20"])
        self.assertIsNotNone(row["rsi14"])
        self.assertEqual(row["trend"], "상승 추세(정배열: 현재가>20일선>60일선)")
        assert_no_nonfinite(self, {k: v for k, v in row.items()
                                   if k not in ("signals",)})

    def test_rsi_returns_none_instead_of_nan(self):
        close = pd.Series([float("nan")] * 20 + [float("nan")])
        self.assertIsNone(stock_tools._calc_rsi(close, 14))

    def test_build_metrics_with_all_nan_history_falls_back_to_error(self):
        nan_hist = pd.DataFrame({"Close": [float("nan")] * 5,
                                 "High": [float("nan")] * 5,
                                 "Low": [float("nan")] * 5,
                                 "Volume": [1.0] * 5})
        self.patch(stock_tools, "get_history", lambda *a, **k: nan_hist)
        row = stock_tools._build_metrics("005930.KS", None)
        self.assertIn("error", row)
        self.assertNotIn("price", row)


# --- 2. 응답 경계: NaN은 500이 아니라 null ----------------------------------

class JsonSafeTests(BaseCase):
    def test_converts_nonfinite_and_numpy(self):
        cleaned = api._json_safe({
            "nan": float("nan"), "inf": float("inf"), "ninf": float("-inf"),
            "np_float": np.float64(1.5), "np_int": np.int64(3),
            "ts": pd.Timestamp("2026-11-19 00:00:00"), "text": "ok", "flag": True,
            "none": None, "nested": [{"v": float("nan")}],
        })
        self.assertIsNone(cleaned["nan"])
        self.assertIsNone(cleaned["inf"])
        self.assertIsNone(cleaned["ninf"])
        self.assertEqual(cleaned["np_float"], 1.5)
        self.assertEqual(cleaned["np_int"], 3)
        self.assertEqual(cleaned["ts"], "2026-11-19T00:00:00")
        self.assertEqual(cleaned["text"], "ok")
        self.assertIs(cleaned["flag"], True)
        self.assertIsNone(cleaned["nested"][0]["v"])

    def test_safe_response_renders_nan_body(self):
        resp = api.SafeJSONResponse(content={"price": float("nan")})
        self.assertEqual(json.loads(resp.body), {"price": None})

    def test_input_dict_is_not_mutated(self):
        payload = {"price": float("nan")}
        api._json_safe(payload)
        self.assertTrue(math.isnan(payload["price"]))


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ScreenNanIntegrationTests(BaseCase):
    """사고와 같은 모양(한국 종목 NaN 지표)의 /api/screen 요청이 200이어야 한다."""

    def setUp(self):
        super().setUp()
        self.patch(api.genai, "Client", lambda **_: FakeGenai(
            {"market_note": "요약", "top_picks": [
                {"ticker": "005930.KS", "price": 150.0, "reason": "근거", "risk": "위험"}]}))
        self.patch(api, "_get_recent_news", lambda ticker, limit=3: [])

    def test_mixed_kr_us_watchlist_is_200(self):
        self.patch(stock_tools.yf, "Ticker", FakeTicker)
        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주",
                                 "tickers": ["005930.KS", "AAPL", "NVDA"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        assert_no_nonfinite(self, body)  # 이전에는 ValueError → 평문 500
        prices = {row["ticker"]: row["price"] for row in body["all_stocks"]}
        self.assertEqual(prices["005930.KS"], 150.0)

    def test_metrics_with_nan_still_serializes(self):
        # 데이터 계층을 우회해 NaN을 직접 흘려보내도 응답은 살아 있어야 한다(마지막 방어선).
        self.patch(stock_tools, "get_stock_metrics",
                   lambda ticker: {"ticker": ticker, "price": float("nan"),
                                   "ma20": float("nan"), "rsi14": float("inf")})
        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주", "tickers": ["AAPL"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsNone(body["all_stocks"][0]["price"])
        self.assertIsNone(body["all_stocks"][0]["rsi14"])

    def test_stock_detail_with_nan_tail_is_200(self):
        self.patch(stock_tools.yf, "Ticker", FakeTicker)
        resp = TestClient(api.app).get("/api/stock/005930.KS")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        assert_no_nonfinite(self, body)
        self.assertEqual(body["levels"]["current"], 150.0)
        self.assertEqual(body["currency"], "KRW")


# --- 3. Gemini 실패는 500이 아니라 폴백 분석 --------------------------------

@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class GeminiFallbackTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.patch(api, "_get_recent_news", lambda ticker, limit=3: [])
        self.patch(stock_tools, "get_stock_metrics", lambda ticker: {
            "ticker": ticker, "name": ticker, "currency": "USD", "price": 100.0,
            "ma20": 95.0, "ma60": 90.0, "rsi14": 45.0, "trend": "상승 추세"})
        self.patch(stock_tools, "get_history", lambda *a, **k: make_hist())

    def test_gemini_failure_returns_fallback_analysis(self):
        self.patch(api.genai, "Client", lambda **_: RaisingGenai())
        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주", "tickers": ["AAPL", "MSFT"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["analysis_source"], "fallback")
        self.assertTrue(body["analysis_note"])
        self.assertEqual(len(body["analysis"]["top_picks"]), 2)
        self.assertEqual([p["ticker"] for p in body["analysis"]["top_picks"]], ["AAPL", "MSFT"])
        # 폴백도 서버가 계산한 점수를 근거로 쓴다(지어낸 값 없음).
        self.assertIn("quant_score", body["analysis"]["top_picks"][0]["reason"])
        # 실패는 기록되어 헬스에서 확인할 수 있다.
        self.assertTrue(any("gemini" in e["path"] for e in api.recent_errors()))

    def test_gemini_garbage_text_falls_back(self):
        self.patch(api.genai, "Client", lambda **_: GarbageGenai())
        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주", "tickers": ["AAPL"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["analysis_source"], "fallback")

    def test_gemini_ok_marks_source(self):
        self.patch(api.genai, "Client", lambda **_: FakeGenai(
            {"market_note": "요약", "top_picks": []}))
        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주", "tickers": ["AAPL"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["analysis_source"], "gemini")


# --- 4. 구조화 오류 기록 ----------------------------------------------------

class ErrorLogTests(BaseCase):
    def test_record_and_read_recent_errors(self):
        with api._ERRORS_LOCK:
            api._ERRORS_RECENT.clear()
        api._record_error("abcd1234", "GET /boom", ValueError("nan"), note="테스트")
        errors = api.recent_errors()
        self.assertEqual(errors[0]["error_id"], "abcd1234")
        self.assertEqual(errors[0]["type"], "ValueError")
        self.assertEqual(errors[0]["path"], "GET /boom")
        # 최대 개수를 넘기면 오래된 것부터 버린다.
        for i in range(api._ERRORS_MAX + 5):
            api._record_error(f"id{i}", "GET /x", RuntimeError("x"))
        self.assertEqual(len(api.recent_errors()), api._ERRORS_MAX)
        with api._ERRORS_LOCK:
            api._ERRORS_RECENT.clear()

    @unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
    def test_health_exposes_recent_errors(self):
        body = TestClient(api.app).get("/").json()
        self.assertIn("errors_recent", body)
        self.assertIsInstance(body["errors_recent"], list)

    def test_parse_analysis_accepts_fenced_and_noisy_text(self):
        self.assertEqual(api._parse_analysis('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(api._parse_analysis('결과입니다: {"a": 2}'), {"a": 2})
        with self.assertRaises(Exception):
            api._parse_analysis("설명만 있습니다")


if __name__ == "__main__":
    unittest.main()
