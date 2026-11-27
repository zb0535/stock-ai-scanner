"""메모리 보호 계층 테스트 — Render 512MB에서 무거운 작업이 겹치지 않게 하는 장치들.

확인하는 것
-----------
1. RSS·한도 읽기와 `auto`/강제 on/off 판정 — 한도를 못 읽는 로컬에서는 아무것도 막지 않는다.
2. `headroom`이 한도(88%)를 기준으로 새 작업을 거부하고, 기다리면 다시 허용한다.
3. 무거운 슬롯(스캔 청크)이 한 번에 하나만 돈다.
4. 프리워밍 단계 락이 S&P 500·코스피 200 스캔/재무 예열을 직렬화한다.
5. 여유가 없을 때 스캔·재무 예열이 죽지 않고 건너뛴다.
6. 캐시 상한(일봉·지표·SEC 원본/지표·네이버·뉴스)과 `GET /`의 `memory` 진단.

실행: python -m unittest discover -s tests -v
"""
import contextlib
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
import kospi200_scanner  # noqa: E402
import memory_guard  # noqa: E402
import naver_finance  # noqa: E402
import sec_edgar  # noqa: E402
import sp500_scanner  # noqa: E402
import stock_tools  # noqa: E402

GUARD_VARS = ("MEM_GUARD", "MEM_LIMIT_MB", "MEM_SOFT_LIMIT_MB", "MEM_HARD_LIMIT_MB")


@contextlib.contextmanager
def guard_env(**values):
    """메모리 가드 환경변수를 테스트 값으로 바꾸고 원래대로 돌린다."""
    saved = {name: os.environ.get(name) for name in GUARD_VARS}
    try:
        for name in GUARD_VARS:
            os.environ.pop(name, None)
        for name, value in values.items():
            os.environ[name] = str(value)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def frame(days: int = 130, start: float = 100.0):
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-27"), periods=days)
    close = pd.Series(np.linspace(start, start + 20, days), index=idx)
    return pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close,
                         "Volume": 1_000_000})


class ReadingTests(unittest.TestCase):
    """RSS·한도·임계값 계산."""

    def test_rss_and_peak_are_readable(self):
        rss = memory_guard.rss_mb()
        peak = memory_guard.peak_rss_mb()
        self.assertIsNotNone(rss)             # /proc이 있는 환경(리눅스·Render)
        self.assertGreater(rss, 1.0)
        self.assertIsNotNone(peak)
        self.assertGreaterEqual(peak, rss * 0.9)

    def test_limits_follow_the_container_limit(self):
        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512"):
            self.assertEqual(memory_guard.limit_mb(), 512.0)
            self.assertEqual(memory_guard.soft_limit_mb(), 384.0)   # 75%
            self.assertEqual(memory_guard.hard_limit_mb(), 450.6)   # 88%
        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512", MEM_SOFT_LIMIT_MB="300",
                       MEM_HARD_LIMIT_MB="400"):
            self.assertEqual(memory_guard.soft_limit_mb(), 300.0)
            self.assertEqual(memory_guard.hard_limit_mb(), 400.0)

    def test_to_mb_handles_max_and_unlimited(self):
        self.assertIsNone(memory_guard._to_mb("max"))
        self.assertIsNone(memory_guard._to_mb("9223372036854771712"))   # cgroup v1 '한도 없음'
        self.assertIsNone(memory_guard._to_mb("0"))
        self.assertAlmostEqual(memory_guard._to_mb(str(512 * 1024 * 1024)), 512.0)

    def test_auto_mode_only_for_small_limits(self):
        with guard_env(MEM_GUARD="auto"):
            with patch.object(memory_guard, "limit_mb", return_value=None):
                self.assertFalse(memory_guard.enabled())      # 로컬·CI — 기존 동작 그대로
            with patch.object(memory_guard, "limit_mb", return_value=512.0):
                self.assertTrue(memory_guard.enabled())       # Render 무료
            with patch.object(memory_guard, "limit_mb", return_value=8192.0):
                self.assertFalse(memory_guard.enabled())      # 큰 머신
        with guard_env(MEM_GUARD="0"):
            with patch.object(memory_guard, "limit_mb", return_value=512.0):
                self.assertFalse(memory_guard.enabled())
        with guard_env(MEM_GUARD="1"):
            with patch.object(memory_guard, "limit_mb", return_value=8192.0):
                self.assertTrue(memory_guard.enabled())


class HeadroomTests(unittest.TestCase):
    """여유 계산 — 한도에 가까우면 새 작업을 거부한다."""

    def test_allows_when_far_from_the_limit(self):
        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512"):
            with patch.object(memory_guard, "rss_mb", return_value=300.0), \
                    patch.object(memory_guard, "trim_caches", return_value={}):
                self.assertTrue(memory_guard.headroom(40))   # 340 < 450.6

    def test_refuses_when_added_work_would_cross_the_limit(self):
        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512"):
            with patch.object(memory_guard, "rss_mb", return_value=440.0), \
                    patch.object(memory_guard, "trim_caches", return_value={"sec_edgar": 1}) as trim:
                self.assertFalse(memory_guard.headroom(30))  # 470 > 450.6
                trim.assert_called_once()                    # 거부 전에 캐시는 정리한다

    def test_waits_for_memory_to_be_released(self):
        readings = iter([440.0, 440.0, 300.0])
        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512"):
            with patch.object(memory_guard, "rss_mb", side_effect=lambda: next(readings, 300.0)), \
                    patch.object(memory_guard, "trim_caches", return_value={}):
                self.assertTrue(memory_guard.headroom(30, wait=2.0))

    def test_disabled_guard_never_refuses(self):
        with guard_env(MEM_GUARD="0"):
            with patch.object(memory_guard, "rss_mb", return_value=10_000.0):
                self.assertTrue(memory_guard.headroom(500))


class HeavySlotTests(unittest.TestCase):
    """무거운 슬롯 — 스캔 청크가 겹치지 않는다."""

    def test_slot_runs_one_at_a_time_and_reports_owner(self):
        order: list[tuple[str, float]] = []
        release = threading.Event()

        def first():
            with memory_guard.heavy_slot("first") as ok:
                order.append(("first", time.time()))
                self.assertTrue(ok)
                self.assertEqual(memory_guard.status()["heavy_owner"], "first")
                release.wait(5)

        def second():
            time.sleep(0.1)
            with memory_guard.heavy_slot("second", timeout=5) as ok:
                order.append(("second", time.time()))
                self.assertTrue(ok)

        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512"), \
                patch.object(memory_guard, "rss_mb", return_value=200.0):
            threads = [threading.Thread(target=first), threading.Thread(target=second)]
            for t in threads:
                t.start()
            time.sleep(0.4)
            release.set()
            for t in threads:
                t.join(5)
        self.assertEqual([name for name, _ in order], ["first", "second"])
        self.assertGreaterEqual(order[1][1] - order[0][1], 0.3)   # 첫 작업이 끝난 뒤 진입
        self.assertIsNone(memory_guard.status()["heavy_owner"])

    def test_slot_yields_false_when_memory_is_tight(self):
        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512"):
            with patch.object(memory_guard, "rss_mb", return_value=445.0), \
                    patch.object(memory_guard, "trim_caches", return_value={}):
                with memory_guard.heavy_slot("scan", need_mb=40) as ok:
                    self.assertFalse(ok)


class ScanMemoryTests(unittest.TestCase):
    """여유가 없을 때 프리 스캔이 죽지 않고 건너뛴다."""

    def setUp(self):
        sp500_scanner.reset_cache()
        self.addCleanup(sp500_scanner.reset_cache)

    def test_scan_rows_skips_chunks_without_headroom(self):
        calls: list[list[str]] = []

        def fake_download(symbols):
            calls.append(list(symbols))
            return None

        @contextlib.contextmanager
        def no_room(_name, **_kwargs):
            yield False

        with patch.object(sp500_scanner, "_download_batch", fake_download), \
                patch.object(memory_guard, "heavy_slot", no_room):
            result = sp500_scanner.scan_rows(["AAA", "BBB"])

        self.assertTrue(result["stopped_for_memory"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(calls, [])          # 여유가 없으면 다운로드도 하지 않는다
        self.assertEqual(result["universe"], 2)

    def test_scan_rows_reports_no_stop_when_memory_is_fine(self):
        def fake_download(symbols):
            return None

        with patch.object(sp500_scanner, "_download_batch", fake_download):
            result = sp500_scanner.scan_rows(["AAA"])
        self.assertFalse(result["stopped_for_memory"])
        self.assertEqual(result["universe"], 1)

    def test_metric_prewarm_skips_tickers_without_headroom(self):
        warmed = []

        def fake_metrics(ticker):
            warmed.append(ticker)
            return {}

        rows = [{"ticker": "AAA"}, {"ticker": "BBB"}]
        with patch.object(sp500_scanner, "rank_rows",
                          side_effect=lambda rows, strategy, top_n=15, intents=None:
                          [{"ticker": "AAA"}, {"ticker": "BBB"}] if strategy == "value" else []), \
                patch.object(sp500_scanner.stock_tools, "has_metrics", return_value=False), \
                patch.object(sp500_scanner.stock_tools, "get_stock_metrics", fake_metrics), \
                patch.object(memory_guard, "headroom", side_effect=[True, False]):
            out = sp500_scanner._warm_candidate_metrics(rows)

        self.assertEqual(warmed, ["AAA"])          # 첫 종목만 받는다
        self.assertEqual(out["warmed"], 1)
        self.assertEqual(out["skipped_for_memory"], 1)


class PrewarmSerializationTests(unittest.TestCase):
    """프리워밍 단계 락 — S&P 500과 코스피 200이 겹치지 않는다."""

    def test_market_prewarms_do_not_overlap(self):
        intervals: list[tuple[str, float, float]] = []
        lock = threading.Lock()

        def record(name, hold=0.3):
            start = time.time()
            time.sleep(hold)
            with lock:
                intervals.append((name, start, time.time()))

        def fake_sp500(**kwargs):
            record("sp500")
            return {"rows": [{"ticker": "AAA"}], "cached": False, "stale": False,
                    "universe": 1, "skipped": 0}

        def fake_kospi(**kwargs):
            record("kospi")
            return {"rows": [{"ticker": "005930.KS"}], "cached": False, "stale": False,
                    "universe": 1, "skipped": 0, "source": "naver"}

        outcomes: dict[str, dict] = {}
        with guard_env(MEM_GUARD="1", MEM_LIMIT_MB="512"), \
                patch.object(memory_guard, "rss_mb", return_value=250.0), \
                patch.object(sp500_scanner, "prescan", fake_sp500), \
                patch.object(sp500_scanner, "snapshot_age", return_value=None), \
                patch.object(sp500_scanner, "_start_metric_prewarm", return_value=False), \
                patch.object(kospi200_scanner, "prescan", fake_kospi), \
                patch.object(kospi200_scanner, "snapshot_age", return_value=None), \
                patch.object(kospi200_scanner, "_wait_for_sp500", return_value=True), \
                patch.object(kospi200_scanner, "_start_fundamental_prewarm", return_value=False):
            threads = [
                threading.Thread(target=lambda: outcomes.update(
                    sp500={"r": sp500_scanner.prewarm_once()})),
                threading.Thread(target=lambda: outcomes.update(
                    kospi={"r": kospi200_scanner.prewarm_once()})),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)

        self.assertTrue(outcomes["sp500"]["r"]["ok"])
        self.assertTrue(outcomes["kospi"]["r"]["ok"])
        self.assertEqual(len(intervals), 2)
        (name_a, start_a, end_a), (name_b, start_b, end_b) = sorted(intervals, key=lambda i: i[1])
        self.assertGreaterEqual(start_b, end_a - 0)   # 두 번째는 첫 번째가 끝난 뒤 시작
        self.assertIsNone(memory_guard.status()["prewarm_phase"])


class CacheBoundTests(unittest.TestCase):
    """캐시 상한 — 무제한으로 쌓이지 않는다."""

    def setUp(self):
        stock_tools.clear_history_cache()
        with stock_tools._CACHE_LOCK:
            stock_tools._METRICS_CACHE.clear()
        sec_edgar.clear_cache()
        self.addCleanup(stock_tools.clear_history_cache)
        self.addCleanup(sec_edgar.clear_cache)

    def test_history_cache_evicts_oldest_beyond_the_cap(self):
        with patch.object(stock_tools, "MAX_HISTORY_ENTRIES", 3):
            for sym in ("AAA", "BBB", "CCC", "DDD"):
                self.assertTrue(stock_tools.prime_history(sym, frame()))
            with stock_tools._CACHE_LOCK:
                keys = list(stock_tools._HISTORY_CACHE)
            self.assertEqual(len(keys), 3)
            self.assertNotIn("AAA:6mo", keys)      # 가장 먼저 넣은 것이 밀려난다
            self.assertIn("DDD:6mo", keys)

    def test_expired_entries_are_dropped_first(self):
        old = frame()
        stock_tools.prime_history("OLD", old, ttl=1)
        with stock_tools._CACHE_LOCK:
            ts, hist, ttl = stock_tools._HISTORY_CACHE["OLD:6mo"]
            stock_tools._HISTORY_CACHE["OLD:6mo"] = (ts - 10, hist, ttl)
        removed = stock_tools.trim_caches()
        self.assertGreaterEqual(removed["history"], 1)
        self.assertNotIn("OLD:6mo", stock_tools._HISTORY_CACHE)

    def test_metrics_cache_is_bounded(self):
        with patch.object(stock_tools, "MAX_METRICS_ENTRIES", 2):
            now = time.time()
            with stock_tools._CACHE_LOCK:
                for idx, sym in enumerate(("A", "B", "C")):
                    stock_tools._METRICS_CACHE[sym] = (now - 10 + idx, {"ticker": sym})
                stock_tools._prune_metrics_locked(now)
                self.assertEqual(list(stock_tools._METRICS_CACHE), ["B", "C"])

    def test_sec_raw_facts_cache_is_small_by_default(self):
        self.assertLessEqual(sec_edgar.MAX_FACTS_CACHE, 2)
        self.assertGreaterEqual(sec_edgar.SEC_MAX_CONCURRENCY, 1)
        self.assertLessEqual(sec_edgar.SEC_MAX_CONCURRENCY, 2)

    def test_sec_metrics_cache_is_bounded(self):
        now = time.time()
        with patch.object(sec_edgar, "MAX_RAW_METRICS", 2):
            with sec_edgar._RAW_METRICS_LOCK:
                for idx, cik in enumerate(("1", "2", "3")):
                    sec_edgar._RAW_METRICS_CACHE[cik] = (now - 10 + idx, 3600, {"trailing_pe": 1})
                removed = sec_edgar._prune_metrics_locked(now)
                self.assertEqual(removed, 1)
                self.assertEqual(list(sec_edgar._RAW_METRICS_CACHE), ["2", "3"])

    def test_sec_rejects_oversized_response(self):
        class Huge:
            headers = {"Content-Length": str(5 * 1024 * 1024)}

            def iter_content(self, chunk_size=65536):
                raise AssertionError("크기를 먼저 보고 자른다")

        with self.assertRaises(ValueError):
            sec_edgar._read_limited(Huge(), 1024 * 1024)

    def test_sec_trim_drops_fresh_raw_facts_but_keeps_metrics(self):
        """압력이 높을 때는 만료되지 않은 원본도 버린다 — 지표는 남아 재다운로드가 없다."""
        now = time.time()
        with sec_edgar._FACTS_LOCK:
            sec_edgar._FACTS_CACHE["0000000001"] = (now, 6 * 3600, {"facts": {"us-gaap": {}}})
        with sec_edgar._RAW_METRICS_LOCK:
            sec_edgar._RAW_METRICS_CACHE["0000000001"] = (now, 6 * 3600, {"trailing_pe": 12.5})

        out = sec_edgar.trim_caches(now)

        self.assertGreaterEqual(out["facts"], 1)
        self.assertEqual(sec_edgar._FACTS_CACHE, {})
        self.assertIn("0000000001", sec_edgar._RAW_METRICS_CACHE)

    def test_sec_skips_parse_when_no_headroom(self):
        calls = []

        def fake_http(url):
            calls.append(url)
            return {"facts": {}}

        with patch.object(sec_edgar, "_http_get_json", fake_http), \
                patch.object(sec_edgar.memory_guard, "headroom", return_value=False):
            self.assertIsNone(sec_edgar._get_raw_metrics("0000000009"))
        self.assertEqual(calls, [])            # 원본을 받지 않는다

    def test_naver_cache_is_bounded(self):
        with patch.object(naver_finance, "MAX_CACHE_ENTRIES", 2):
            now = time.time()
            with naver_finance._CACHE_LOCK:
                naver_finance._CACHE.clear()
                for idx, key in enumerate(("a", "b", "c")):
                    naver_finance._CACHE[key] = (now + 3600 + idx, {"v": key})
                naver_finance._prune_locked(now)
                self.assertEqual(sorted(naver_finance._CACHE), ["b", "c"])
            naver_finance.clear_cache()

    def test_news_cache_is_bounded(self):
        now = time.time()
        with patch.object(api, "_NEWS_MAX_ENTRIES", 2):
            with api._NEWS_LOCK:
                api._NEWS_CACHE.clear()
                for idx, sym in enumerate(("A", "B", "C")):
                    api._NEWS_CACHE[sym] = (now - 10 + idx, 1800, ["t"])
            removed = api._trim_news_cache(now)     # 락은 안에서 잡는다
            self.assertEqual(removed, 1)
            self.assertEqual(sorted(api._NEWS_CACHE), ["B", "C"])
            with api._NEWS_LOCK:
                api._NEWS_CACHE.clear()


class HealthMemoryTests(unittest.TestCase):
    """`GET /`가 메모리 상태를 노출한다 — 배포 후 실서버에서 바로 확인할 수 있게."""

    def test_health_reports_memory(self):
        body = api.health()
        mem = body["memory"]
        for key in ("guard", "rss_mb", "peak_rss_mb", "limit_mb", "soft_mb", "hard_mb",
                    "pressure_pct", "heavy_busy", "prewarm_phase", "caches"):
            self.assertIn(key, mem)
        self.assertIn("stock_tools", mem["caches"])
        self.assertIn("sec_edgar", mem["caches"])
        self.assertIn("naver_finance", mem["caches"])
        self.assertIn("api_news", mem["caches"])

    def test_trim_caches_covers_every_module(self):
        trimmed = memory_guard.trim_caches()
        for name in ("stock_tools", "sec_edgar", "naver_finance", "api_news"):
            self.assertIn(name, trimmed)


if __name__ == "__main__":
    unittest.main()
