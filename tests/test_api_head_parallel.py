"""HEAD 헬스 체크(200)와 병렬 스크리닝(순서 보존·동시성 상한·캐시 스레드 안전) 테스트.

실행: python -m unittest discover -s tests -v
(HEAD 라우트 테스트는 fastapi.testclient를 쓰므로 httpx가 필요합니다.
 httpx가 없으면 그 테스트만 건너뜁니다.)
"""
import json
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
import stock_tools  # noqa: E402

try:  # TestClient는 httpx에 의존한다.
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


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


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    """genai client.models 대역. 호출 인자(프롬프트)도 기록한다."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        # 실제 모델처럼 ```json 펜스로 감싸서 돌려준다(api.py가 벗겨내는지 확인).
        return FakeResponse("```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```")


class FakeGenai:
    def __init__(self, payload):
        self.models = FakeModels(payload)


class FakeTicker:
    """병렬 조회에서도 안전한 yfinance.Ticker 대역(스레드 안전 카운터)."""

    lock = threading.Lock()
    calls = {"info": [], "history": []}

    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        with FakeTicker.lock:
            FakeTicker.calls["info"].append(self.symbol)
        time.sleep(0.005)
        return {"shortName": f"{self.symbol} Inc.", "trailingPE": 12.5}

    def history(self, period="6mo"):
        with FakeTicker.lock:
            FakeTicker.calls["history"].append((self.symbol, period))
        time.sleep(0.005)
        return make_hist()


class BaseCase(unittest.TestCase):
    def patch_attr(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        stock_tools._info_fail_streak = 0
        stock_tools._info_cooldown_until = 0.0
        FakeTicker.calls = {"info": [], "history": []}
        # t.info가 일부 값만 주는 대역이라 SEC EDGAR 폴백이 실네트워크를 타지 않게 막는다.
        self.patch_attr(stock_tools.sec_edgar, "get_financial_metrics", lambda *a, **k: {})


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class HeadRouteTests(BaseCase):
    """UptimeRobot 무료 모니터는 HEAD로 찔러온다 → 405가 아니라 200이어야 한다."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app)

    def test_head_watchlist_returns_200(self):
        resp = self.client.head("/api/watchlist")
        self.assertEqual(resp.status_code, 200)          # 이전에는 405
        self.assertEqual(resp.content, b"")               # HEAD 응답에는 본문이 없다

    def test_get_watchlist_response_unchanged(self):
        resp = self.client.get("/api/watchlist")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"tickers": stock_tools.DEFAULT_WATCHLIST})

    def test_head_root_returns_200(self):
        resp = self.client.head("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")

    def test_get_root_returns_ok_json(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        # 배포 확인용 build 마커(로컬 실행은 "local", Render는 커밋 앞 7자리)
        self.assertIn("build", resp.json())


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ParallelScreenTests(BaseCase):
    """15개 종목 스크리닝이 병렬로 돌되, 결과 순서와 기존 폴백 동작은 그대로여야 한다."""

    TICKERS = [f"T{i:02d}" for i in range(15)]

    def _instrumented_fakes(self, tickers, call_log):
        """앞 종목일수록 오래 걸리는 가짜 조회. 순차 실행이면 peak가 1이 된다."""
        lock = threading.Lock()
        state = {"inflight": 0, "peak": 0}

        def _run(ticker, kind, delay_unit):
            rank = tickers.index(ticker)
            with lock:
                state["inflight"] += 1
                state["peak"] = max(state["peak"], state["inflight"])
                call_log.append((kind, ticker))
            time.sleep(delay_unit * (len(tickers) - rank))
            with lock:
                state["inflight"] -= 1

        def fake_metrics(ticker):
            _run(ticker, "metrics", 0.01)
            return {"ticker": ticker, "price": 100.0, "financials_available": True}

        def fake_news(ticker, limit=3):
            _run(ticker, "news", 0.005)
            return [f"{ticker} headline"]

        return state, fake_metrics, fake_news

    def test_screen_runs_in_parallel_and_keeps_order(self):
        call_log = []
        state, fake_metrics, fake_news = self._instrumented_fakes(self.TICKERS, call_log)
        self.patch_attr(stock_tools, "get_stock_metrics", fake_metrics)
        self.patch_attr(api, "_get_recent_news", fake_news)
        self.patch_attr(api.genai, "Client", lambda **_: FakeGenai({"market_note": "ok", "top_picks": []}))

        client = TestClient(api.app)
        resp = client.post("/api/screen", json={"criteria": "저PER 가치주", "tickers": self.TICKERS})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # 순서 보존: 입력 순서 그대로 15행
        self.assertEqual([r["ticker"] for r in body["all_stocks"]], self.TICKERS)
        # 각 행에 뉴스가 붙는다(limit=2)
        self.assertTrue(all(r["news"] == [f"{r['ticker']} headline"] for r in body["all_stocks"]))
        # 병렬 실행 + 동시 호출 6개 상한
        self.assertGreaterEqual(state["peak"], 2, "병렬로 실행되지 않았다")
        self.assertLessEqual(state["peak"], stock_tools.MAX_FETCH_WORKERS)
        # 종목마다 한 번씩만 조회
        self.assertEqual(sorted(t for k, t in call_log if k == "metrics"), sorted(self.TICKERS))

    def test_screen_attaches_charts_to_picks_in_order(self):
        picks = [{"ticker": "AAPL", "price": 1.0, "reason": "r", "risk": "k"},
                 {"ticker": "MSFT", "price": 2.0, "reason": "r", "risk": "k"},
                 {"ticker": "NVDA", "price": 3.0, "reason": "r", "risk": "k"}]
        self.patch_attr(stock_tools, "get_stock_metrics", lambda t: {"ticker": t, "price": 1.0})
        self.patch_attr(api, "_get_recent_news", lambda t, limit=3: [f"{t} news"])
        self.patch_attr(stock_tools, "get_history", lambda t, p="6mo": make_hist())
        self.patch_attr(api.genai, "Client", lambda **_: FakeGenai({"market_note": "ok", "top_picks": picks}))

        resp = TestClient(api.app).post("/api/screen", json={"criteria": "저PER 가치주", "tickers": ["AAPL"]})

        self.assertEqual(resp.status_code, 200)
        got = resp.json()["analysis"]["top_picks"]
        self.assertEqual([p["ticker"] for p in got], ["AAPL", "MSFT", "NVDA"])
        for pick in got:
            self.assertEqual(len(pick["chart"]), 60)      # 최근 60거래일
            self.assertIsInstance(pick["chart"][-1]["price"], float)
            self.assertEqual(pick["news"], [f"{pick['ticker']} news"])
            self.assertIn("date", pick["chart"][0])

    def test_parallel_metrics_are_thread_safe_for_all_tickers(self):
        self.patch_attr(stock_tools.yf, "Ticker", FakeTicker)
        tickers = self.TICKERS
        rows = stock_tools.bounded_map(stock_tools.get_stock_metrics, tickers)

        self.assertEqual([r["ticker"] for r in rows], tickers)
        for row in rows:
            self.assertIsNotNone(row.get("price"))
            self.assertTrue(row.get("financials_available"))
        # 모든 종목이 캐시에 들어갔고, 두 번째 호출은 네트워크를 다시 타지 않는다
        self.assertEqual(sorted(stock_tools._METRICS_CACHE), sorted(tickers))
        info_calls = len(FakeTicker.calls["info"])
        stock_tools.bounded_map(stock_tools.get_stock_metrics, tickers)
        self.assertEqual(len(FakeTicker.calls["info"]), info_calls)


class BoundedMapTests(BaseCase):
    def test_empty_input(self):
        self.assertEqual(stock_tools.bounded_map(lambda x: x, []), [])

    def test_order_preserved_when_later_items_finish_first(self):
        items = list(range(8))

        def slow(x):
            time.sleep(0.01 * (len(items) - x))
            return x * 10

        self.assertEqual(stock_tools.bounded_map(slow, items), [x * 10 for x in items])

    def test_worker_count_is_bounded(self):
        lock = threading.Lock()
        state = {"inflight": 0, "peak": 0}

        def work(x):
            with lock:
                state["inflight"] += 1
                state["peak"] = max(state["peak"], state["inflight"])
            time.sleep(0.02)
            with lock:
                state["inflight"] -= 1
            return x

        stock_tools.bounded_map(work, list(range(12)), max_workers=3)
        self.assertLessEqual(state["peak"], 3)
        self.assertGreaterEqual(state["peak"], 2)

    def test_single_worker_runs_sequentially(self):
        lock = threading.Lock()
        state = {"inflight": 0, "peak": 0}

        def work(x):
            with lock:
                state["inflight"] += 1
                state["peak"] = max(state["peak"], state["inflight"])
            time.sleep(0.005)
            with lock:
                state["inflight"] -= 1
            return x

        self.assertEqual(stock_tools.bounded_map(work, list(range(5)), max_workers=1), list(range(5)))
        self.assertEqual(state["peak"], 1)

    def test_exception_propagates_like_sequential(self):
        def boom(x):
            if x == 3:
                raise ValueError("bad ticker")
            return x

        with self.assertRaises(ValueError):
            stock_tools.bounded_map(boom, list(range(5)))


if __name__ == "__main__":
    unittest.main()
