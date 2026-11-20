"""AI 추천 성적표(history_store + 성적표/추적/삭제 API) 테스트.

다루는 것
1. SQLite 기록: 필드·KST 날짜·진입가·전략/모드/점수, 스키마 멱등성
2. 같은 날 중복 제거: (date, ticker, strategy) UNIQUE, 첫 진입가 유지
3. 수익률·적중률 계산: 승/패, 평균, 최고/최악, 반올림 (픽마다 `id` 포함)
4. 현재가 배치 조회: 멀티/단일 컬럼 파싱, 5분 캐시, 실패 시 수익률만 비움
5. 빈 이력: 개수 0, 비율 null
6. **수동 추적**: `/api/screen`은 기록하지 않고, `POST /api/performance/track`만 기록한다
   — 진입가는 실시간 현재가 우선(실패 시 일봉 종가 폴백)
7. **개별 삭제**: `DELETE /api/performance/{id}`가 그 행만 지우고, 없으면 404

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import history_store  # noqa: E402
import realtime_quotes  # noqa: E402
import stock_tools  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None

KST = timezone(timedelta(hours=9))


def make_hist(days: int = 130, end="2026-11-18", start_price=100.0, end_price=150.0):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    close = pd.Series(np.linspace(start_price, end_price, days), index=idx)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": 1_000_000})


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, payload):
        self.payload = payload

    def generate_content(self, **kwargs):
        return FakeResponse("```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```")


class FakeGenai:
    def __init__(self, payload):
        self.models = FakeModels(payload)


class TempDB(unittest.TestCase):
    """테스트마다 빈 SQLite 파일을 쓰도록 HISTORY_DB_PATH를 바꾼다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "sub", "perf.db")
        patcher = patch.dict(os.environ, {"HISTORY_DB_PATH": self.db_path})
        patcher.start()
        self.addCleanup(patcher.stop)
        history_store.clear_price_cache()
        self.addCleanup(history_store.clear_price_cache)

    def picks(self, *rows):
        return history_store.recent_picks(limit=50)


# ---------------------------------------------------------------------------
# 1. 기록
# ---------------------------------------------------------------------------


class RecordTests(TempDB):
    def test_record_persists_all_fields(self):
        out = history_store.record_picks(
            [{"ticker": "aapl", "price": 191.25, "quant_score": 82.5}],
            strategy="value", mode="sp500", criteria="저PER 가치주",
        )
        self.assertEqual(out["inserted"], 1)
        rows = self.picks()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["ticker"], "AAPL")                      # 대문자 정규화
        self.assertEqual(row["entry_price"], 191.25)
        self.assertEqual(row["strategy"], "value")
        self.assertEqual(row["mode"], "sp500")
        self.assertEqual(row["quant_score"], 82.5)
        self.assertEqual(row["criteria"], "저PER 가치주")
        # 날짜는 KST 기준 YYYY-MM-DD, 시각은 ISO(오프셋 포함)
        self.assertRegex(row["recommended_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(row["recommended_date"], history_store.kst_today().strftime("%Y-%m-%d"))
        parsed = datetime.fromisoformat(row["recommended_at"])
        self.assertEqual(parsed.utcoffset(), timedelta(hours=9))

    def test_recommended_date_uses_kst_not_utc(self):
        # UTC로는 11-18 15:30이지만 KST로는 11-19 00:30 → 날짜는 11-19여야 한다.
        at = datetime(2026, 11, 18, 15, 30, tzinfo=timezone.utc)
        history_store.record_picks([{"ticker": "MSFT", "price": 410}],
                                   strategy="balanced", recommended_at=at.isoformat())
        self.assertEqual(self.picks()[0]["recommended_date"], "2026-11-19")

    def test_db_path_default_and_parent_creation(self):
        self.assertFalse(os.path.exists(self.db_path))
        history_store.init_db()
        self.assertTrue(os.path.exists(self.db_path))                # 상위 폴더까지 생성
        self.assertEqual(history_store.init_db(), os.path.join(self._tmp.name, "sub", "perf.db"))

    def test_init_db_is_idempotent(self):
        history_store.record_picks([{"ticker": "NVDA", "price": 100}], strategy="momentum")
        history_store.init_db()
        history_store.get_performance(price_fetcher=lambda t: {})
        self.assertEqual(len(self.picks()), 1)

    def test_missing_price_is_recorded_but_unevaluated(self):
        history_store.record_picks([{"ticker": "TSLA", "price": None}], strategy="value")
        row = self.picks()[0]
        self.assertIsNone(row["entry_price"])
        body = history_store.get_performance(price_fetcher=lambda t: {"TSLA": 250.0})
        self.assertEqual(body["summary"]["total_picks"], 1)
        self.assertEqual(body["summary"]["evaluated_picks"], 0)
        self.assertIsNone(body["picks"][0]["return_pct"])

    def test_invalid_picks_are_skipped_not_fatal(self):
        out = history_store.record_picks(
            [{"ticker": "", "price": 1}, {"price": 2}, "not-a-dict", {"ticker": "AMD", "price": 3}],
            strategy="value",
        )
        self.assertEqual(out["inserted"], 1)
        self.assertEqual(out["skipped"], 3)
        self.assertEqual([r["ticker"] for r in self.picks()], ["AMD"])

    def test_empty_picks_writes_nothing(self):
        out = history_store.record_picks([], strategy="value")
        self.assertEqual((out["inserted"], out["updated"]), (0, 0))
        self.assertEqual(self.picks(), [])


# ---------------------------------------------------------------------------
# 2. 같은 날 중복 제거
# ---------------------------------------------------------------------------


class DedupeTests(TempDB):
    def test_same_day_same_strategy_keeps_one_row_with_first_entry(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100.0, "quant_score": 70}],
                                   strategy="value", mode="watchlist", criteria="첫 조건")
        out = history_store.record_picks([{"ticker": "AAPL", "price": 105.0, "quant_score": 88}],
                                         strategy="value", mode="sp500", criteria="두 번째 조건")
        self.assertEqual((out["inserted"], out["updated"]), (0, 1))

        rows = self.picks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_price"], 100.0)   # 최초 진입가 유지(수익률 왜곡 방지)
        self.assertEqual(rows[0]["quant_score"], 88.0)    # 점수·모드는 최신
        self.assertEqual(rows[0]["mode"], "sp500")
        self.assertEqual(rows[0]["criteria"], "두 번째 조건")

    def test_same_day_different_strategy_is_a_separate_row(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value")
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="momentum")
        # 둘 다 strategy=""가 아니므로 2행
        self.assertEqual(len(self.picks()), 2)

    def test_different_day_same_ticker_is_a_separate_row(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value",
                                   recommended_date="2026-11-17")
        history_store.record_picks([{"ticker": "AAPL", "price": 102}], strategy="value",
                                   recommended_date="2026-11-18")
        rows = self.picks()
        self.assertEqual([r["recommended_date"] for r in rows], ["2026-11-18", "2026-11-17"])

    def test_empty_strategy_is_deduped_too(self):
        history_store.record_picks([{"ticker": "AAPL", "price": 100}])
        history_store.record_picks([{"ticker": "AAPL", "price": 101}])
        self.assertEqual(len(self.picks()), 1)

    def test_many_tickers_in_one_call(self):
        picks = [{"ticker": t, "price": 10.0} for t in ("AAPL", "MSFT", "NVDA")]
        out = history_store.record_picks(picks, strategy="value")
        self.assertEqual(out["inserted"], 3)
        self.assertEqual(sorted(r["ticker"] for r in self.picks()), ["AAPL", "MSFT", "NVDA"])


# ---------------------------------------------------------------------------
# 3. 수익률·적중률 계산
# ---------------------------------------------------------------------------


class MathTests(unittest.TestCase):
    def test_returns_and_win_rate(self):
        picks = [
            {"ticker": "AAA", "recommended_date": "2026-11-10", "entry_price": 100.0,
             "strategy": "value", "mode": "watchlist", "quant_score": 80},
            {"ticker": "BBB", "recommended_date": "2026-11-10", "entry_price": 200.0,
             "strategy": "value", "mode": "watchlist", "quant_score": 75},
            {"ticker": "CCC", "recommended_date": "2026-11-12", "entry_price": 50.0,
             "strategy": "value", "mode": "watchlist", "quant_score": 60},
        ]
        prices = {"AAA": 110.0, "BBB": 190.0, "CCC": 50.0}
        body = history_store.compute_performance(picks, prices, today=date(2026, 11, 19))
        rows = {r["ticker"]: r for r in body["picks"]}

        self.assertAlmostEqual(rows["AAA"]["return_pct"], 10.0)
        self.assertTrue(rows["AAA"]["is_win"])
        self.assertAlmostEqual(rows["BBB"]["return_pct"], -5.0)
        self.assertFalse(rows["BBB"]["is_win"])
        self.assertEqual(rows["CCC"]["return_pct"], 0.0)
        self.assertFalse(rows["CCC"]["is_win"])          # 본전은 승이 아니다(> 0)
        self.assertEqual(rows["AAA"]["days_held"], 9)
        self.assertEqual(rows["CCC"]["days_held"], 7)

        summary = body["summary"]
        self.assertEqual(summary["total_picks"], 3)
        self.assertEqual(summary["evaluated_picks"], 3)
        self.assertEqual(summary["win_count"], 1)
        self.assertAlmostEqual(summary["win_rate_pct"], 33.3)
        self.assertAlmostEqual(summary["avg_return_pct"], 1.67)   # (10-5+0)/3 = 1.667 → 1.67
        self.assertEqual(summary["best_pick"]["ticker"], "AAA")
        self.assertEqual(summary["worst_pick"]["ticker"], "BBB")

    def test_all_winners_and_all_losers(self):
        picks = [{"ticker": "A", "recommended_date": "2026-11-18", "entry_price": 10.0},
                 {"ticker": "B", "recommended_date": "2026-11-18", "entry_price": 20.0}]
        up = history_store.compute_performance(picks, {"A": 11.0, "B": 30.0},
                                               today=date(2026, 11, 19))["summary"]
        self.assertEqual((up["win_count"], up["win_rate_pct"]), (2, 100.0))
        down = history_store.compute_performance(picks, {"A": 9.0, "B": 19.0},
                                                 today=date(2026, 11, 19))["summary"]
        self.assertEqual((down["win_count"], down["win_rate_pct"]), (0, 0.0))
        self.assertIsNotNone(down["avg_return_pct"])
        self.assertLess(down["avg_return_pct"], 0)

    def test_zero_or_missing_entry_price_is_not_evaluated(self):
        picks = [{"ticker": "A", "recommended_date": "2026-11-18", "entry_price": 0},
                 {"ticker": "B", "recommended_date": "2026-11-18", "entry_price": None},
                 {"ticker": "C", "recommended_date": "2026-11-18", "entry_price": "12.5"}]
        body = history_store.compute_performance(picks, {"A": 10, "B": 10, "C": 13.75})
        evaluated = [r for r in body["picks"] if r["return_pct"] is not None]
        self.assertEqual([r["ticker"] for r in evaluated], ["C"])   # 문자열 숫자도 허용
        self.assertAlmostEqual(evaluated[0]["return_pct"], 10.0)
        self.assertEqual(body["summary"]["evaluated_picks"], 1)

    def test_return_is_rounded_to_two_decimals(self):
        picks = [{"ticker": "A", "recommended_date": "2026-11-18", "entry_price": 3.0}]
        body = history_store.compute_performance(picks, {"A": 3.1})
        self.assertEqual(body["picks"][0]["return_pct"], 3.33)

    def test_quant_score_passthrough(self):
        picks = [{"ticker": "A", "recommended_date": "2026-11-18", "entry_price": 1,
                  "quant_score": 91, "strategy": "momentum", "mode": "sp500"}]
        row = history_store.compute_performance(picks, {"A": 1.0})["picks"][0]
        self.assertEqual((row["quant_score"], row["strategy"], row["mode"]),
                         (91.0, "momentum", "sp500"))

    def test_pick_id_is_passed_through_for_individual_delete(self):
        picks = [{"id": 7, "ticker": "A", "recommended_date": "2026-11-18", "entry_price": 1}]
        body = history_store.compute_performance(picks, {"A": 1.1})
        self.assertEqual(body["picks"][0]["id"], 7)          # 클라이언트가 이 값으로 삭제한다
        self.assertEqual(history_store.compute_performance([], {})["picks"], [])


# ---------------------------------------------------------------------------
# 4. 현재가 배치 조회·캐시·실패 견고성
# ---------------------------------------------------------------------------


class PriceTests(unittest.TestCase):
    def setUp(self):
        history_store.clear_price_cache()
        self.addCleanup(history_store.clear_price_cache)

    def test_extract_from_multiindex_columns(self):
        idx = pd.bdate_range(end="2026-11-18", periods=3)
        cols = pd.MultiIndex.from_tuples(
            [("Close", "AAPL"), ("Open", "AAPL"), ("Close", "MSFT"), ("Open", "MSFT")])
        df = pd.DataFrame(np.arange(12, dtype=float).reshape(3, 4), index=idx, columns=cols)
        got = history_store._extract_last_closes(df, ["AAPL", "MSFT"])
        self.assertEqual(got["AAPL"], 8.0)   # 마지막 행 Close/AAPL
        self.assertEqual(got["MSFT"], 10.0)

    def test_extract_from_single_ticker_frame(self):
        idx = pd.bdate_range(end="2026-11-18", periods=3)
        df = pd.DataFrame({"Close": [1.0, 2.0, 3.0], "Open": [0.5, 1.5, 2.5]}, index=idx)
        self.assertEqual(history_store._extract_last_closes(df, ["AAPL"]), {"AAPL": 3.0})

    def test_extract_ignores_empty_and_nan(self):
        self.assertEqual(history_store._extract_last_closes(None, ["AAPL"]), {})
        self.assertEqual(history_store._extract_last_closes(pd.DataFrame(), ["AAPL"]), {})
        idx = pd.bdate_range(end="2026-11-18", periods=2)
        df = pd.DataFrame({"Close": [np.nan, np.nan]}, index=idx)
        self.assertEqual(history_store._extract_last_closes(df, ["AAPL"]), {})

    def test_price_cache_hits_within_five_minutes(self):
        calls = []

        def fetcher(tickers):
            calls.append(list(tickers))
            return {t: 100.0 for t in tickers}

        first = history_store.latest_prices(["AAPL", "MSFT"], fetcher=fetcher)
        second = history_store.latest_prices(["AAPL"], fetcher=fetcher)
        self.assertEqual(first, {"AAPL": 100.0, "MSFT": 100.0})
        self.assertEqual(second, {"AAPL": 100.0})
        self.assertEqual(len(calls), 1)                 # 두 번째는 캐시
        # ttl=0 이면 다시 조회한다
        history_store.latest_prices(["AAPL"], ttl=0, fetcher=fetcher)
        self.assertEqual(len(calls), 2)

    def test_failed_lookup_is_cached_as_none(self):
        calls = []

        def boom(tickers):
            calls.append(1)
            return {}

        self.assertEqual(history_store.latest_prices(["AAPL"], fetcher=boom), {"AAPL": None})
        self.assertEqual(history_store.latest_prices(["AAPL"], fetcher=boom), {"AAPL": None})
        self.assertEqual(len(calls), 1)                 # 실패도 5분 캐시(장애 시 재시도 폭주 방지)

    def test_fetch_batch_swallows_yfinance_errors(self):
        with patch.object(history_store.yf, "download", side_effect=RuntimeError("429")):
            self.assertEqual(history_store._fetch_prices_batch(["AAPL"]), {})

    def test_performance_survives_price_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "perf.db")
            history_store.record_picks([{"ticker": "AAPL", "price": 100}], strategy="value",
                                       db_path=db)

            def boom(tickers):
                raise RuntimeError("network down")

            with patch.object(history_store, "_fetch_prices_batch", side_effect=boom):
                body = history_store.get_performance(db_path=db)
            self.assertEqual(body["summary"]["total_picks"], 1)
            self.assertEqual(body["summary"]["evaluated_picks"], 0)
            self.assertIsNone(body["picks"][0]["current_price"])
            self.assertEqual(body.get("error"), "price_unavailable")

    def test_get_performance_uses_batch_once_for_all_tickers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "perf.db")
            history_store.record_picks(
                [{"ticker": t, "price": 100} for t in ("AAPL", "MSFT", "NVDA")],
                strategy="value", db_path=db)
            calls = []

            def fetcher(tickers):
                calls.append(sorted(tickers))
                return {"AAPL": 101, "MSFT": 99, "NVDA": 120}

            body = history_store.get_performance(db_path=db, price_fetcher=fetcher)
            self.assertEqual(calls, [["AAPL", "MSFT", "NVDA"]])       # 한 번의 배치 호출
            self.assertEqual(body["summary"]["win_count"], 2)


# ---------------------------------------------------------------------------
# 5. 빈 이력
# ---------------------------------------------------------------------------


class EmptyStateTests(TempDB):
    def test_empty_history_summary_shape(self):
        body = history_store.get_performance(price_fetcher=lambda t: {})
        self.assertEqual(body["picks"], [])
        self.assertEqual(body["summary"]["total_picks"], 0)
        self.assertEqual(body["summary"]["evaluated_picks"], 0)
        self.assertEqual(body["summary"]["win_count"], 0)
        self.assertIsNone(body["summary"]["win_rate_pct"])
        self.assertIsNone(body["summary"]["avg_return_pct"])
        self.assertIsNone(body["summary"]["best_pick"])
        self.assertIsNone(body["summary"]["worst_pick"])
        self.assertIn("as_of", body)

    def test_empty_performance_helper_is_consistent(self):
        body = history_store.empty_performance(error="x")
        self.assertEqual(body["error"], "x")
        self.assertEqual(set(body["summary"]),
                         {"total_picks", "evaluated_picks", "win_count", "win_rate_pct",
                          "avg_return_pct", "best_pick", "worst_pick"})

    def test_corrupt_db_path_reports_error_without_raising(self):
        # 디렉터리 자리에 파일을 만들어 SQLite로 열 수 없게 만든다.
        bogus = os.path.join(self._tmp.name, "not_a_dir")
        Path(bogus).write_text("x", encoding="utf-8")
        body = history_store.get_performance(db_path=os.path.join(bogus, "perf.db"))
        self.assertEqual(body["picks"], [])
        self.assertEqual(body["summary"]["total_picks"], 0)
        self.assertEqual(body.get("error"), "history_unavailable")


# ---------------------------------------------------------------------------
# 6~7. /api/screen → /api/performance 통합 + 회귀
# ---------------------------------------------------------------------------


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ScreenIntegrationTests(TempDB):
    PICKS = [
        {"ticker": "AAPL", "price": 999.0, "reason": "r", "risk": "k"},
        {"ticker": "MSFT", "price": 888.0, "reason": "r", "risk": "k"},
    ]

    def setUp(self):
        super().setUp()
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        api.clear_analysis_cache()
        api.realtime_quotes.clear_cache()
        self.addCleanup(stock_tools._METRICS_CACHE.clear)
        self.addCleanup(stock_tools._HISTORY_CACHE.clear)
        self._patch(stock_tools, "get_stock_metrics",
                    lambda t: {"ticker": t, "price": 100.0 if t == "AAPL" else 200.0,
                               "financials_available": True})
        self._patch(api.realtime_quotes, "get_prices", lambda *a, **k: {})
        self._patch(api, "_get_recent_news", lambda t, limit=3: [f"{t} news"])
        self._patch(stock_tools, "get_history", lambda t, p="6mo": make_hist())
        self._patch(api.genai, "Client",
                    lambda **_: FakeGenai({"market_note": "ok", "top_picks": list(self.PICKS)}))
        self.client = TestClient(api.app)

    def _patch(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    def _screen(self, tickers=("AAPL", "MSFT"), criteria="저PER 가치주 상위 3개"):
        return self.client.post("/api/screen", json={"tickers": list(tickers), "criteria": criteria})

    def test_screen_does_not_record_picks(self):
        # 2026-11 변경: 분석만 돌려도 성적표가 오염되던 자동 기록을 없앴다.
        resp = self._screen()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["analysis"]["top_picks"]), 2)
        self.assertEqual(self.picks(), [])

    def test_screen_response_contract_unchanged(self):
        body = self._screen().json()
        # 기존 클라이언트(웹/안드로이드)가 쓰는 키는 그대로 있어야 한다.
        for key in ("analysis", "all_stocks", "strategy", "mode", "universe_scanned"):
            self.assertIn(key, body)
        self.assertTrue(body["mode"])
        self.assertEqual([p["ticker"] for p in body["analysis"]["top_picks"]], ["AAPL", "MSFT"])
        self.assertIn("chart", body["analysis"]["top_picks"][0])
        self.assertNotIn("performance", body)          # 새 키를 끼워 넣지 않는다

    def test_repeated_screens_still_record_nothing(self):
        self._screen()
        self._screen()
        self.assertEqual(self.picks(), [])

    def test_screen_does_not_touch_storage_at_all(self):
        calls = []

        def boom(*a, **k):
            calls.append(a)
            raise sqlite3_like_error()

        self._patch(history_store, "record_picks", boom)
        self._patch(history_store, "record_pick", boom)
        self.assertEqual(self._screen().status_code, 200)
        self.assertEqual(calls, [])                    # DB 쓰기 시도 자체가 없다

    def test_performance_endpoint_empty_before_any_track(self):
        body = self.client.get("/api/performance").json()
        self.assertEqual(body["summary"]["total_picks"], 0)
        self.assertIsNone(body["summary"]["win_rate_pct"])
        self.assertEqual(body["picks"], [])

    def test_performance_endpoint_reports_error_instead_of_500(self):
        self._patch(history_store, "get_performance",
                    lambda *a, **k: (_ for _ in ()).throw(sqlite3_like_error()))
        resp = self.client.get("/api/performance")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"]["total_picks"], 0)
        self.assertEqual(resp.json()["error"], "performance_unavailable")

    def test_screen_with_no_picks_records_nothing(self):
        self._patch(api.genai, "Client",
                    lambda **_: FakeGenai({"market_note": "none", "top_picks": []}))
        resp = self._screen()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.picks(), [])


# ---------------------------------------------------------------------------
# 6. 수동 추적: POST /api/performance/track
# ---------------------------------------------------------------------------

PRICES = {"NVDA": 150.96, "AAPL": 191.25, "MSFT": 410.0}


class StubPricesMixin:
    """가격 조회를 네트워크 없이 고정한다(모르는 티커는 값 없음).

    실시간 시세도 기본은 빈 응답으로 둔다 — 진입가 테스트가 네트워크(네이버·Finnhub)를
    타지 않게. 실시간가를 쓰는 검사는 `stub_quotes`로 대신 넣는다.
    """

    def stub_prices(self, mapping=None):
        table = dict(PRICES if mapping is None else mapping)
        p = patch.object(history_store, "_fetch_prices_batch",
                         lambda tickers: {t: table[t] for t in tickers if t in table})
        p.start()
        self.addCleanup(p.stop)
        history_store.clear_price_cache()
        self.addCleanup(history_store.clear_price_cache)
        return table

    def stub_quotes(self, mapping=None):
        """실시간 시세 대역 — `{티커: (가격, 출처)}` 또는 `None`/예외 안 냄."""
        table = dict(mapping or {})
        calls = []

        def fake(tickers, max_workers=8):
            calls.append(list(tickers))
            return {t: {"price": table[t][0], "source": table[t][1]}
                    for t in tickers if t in table}

        p = patch.object(realtime_quotes, "get_prices", fake)
        p.start()
        self.addCleanup(p.stop)
        realtime_quotes.clear_cache()
        self.addCleanup(realtime_quotes.clear_cache)
        return calls


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class TrackApiTests(StubPricesMixin, TempDB):
    def setUp(self):
        super().setUp()
        self.stub_prices()
        self.quotes = self.stub_quotes()                   # 기본: 실시간가는 없음
        self.client = TestClient(api.app)

    def track(self, **body):
        return self.client.post("/api/performance/track", json=body)

    def test_track_inserts_and_returns_pick_with_id(self):
        resp = self.track(ticker="nvda", entry_price=150.96, strategy="oversold",
                          mode="watchlist", quant_score=92.0, criteria="과매도 반등")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["created"])
        pick = body["pick"]
        self.assertIsInstance(pick["id"], int)
        self.assertEqual((pick["ticker"], pick["entry_price"]), ("NVDA", 150.96))
        self.assertEqual((pick["strategy"], pick["mode"]), ("oversold", "watchlist"))
        self.assertEqual((pick["quant_score"], pick["criteria"]), (92.0, "과매도 반등"))
        # 응답의 id가 실제 저장된 행의 id와 같아야 삭제할 수 있다.
        rows = self.picks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], pick["id"])
        self.assertEqual(rows[0]["recommended_date"], history_store.kst_today().strftime("%Y-%m-%d"))

    def test_track_without_entry_price_uses_latest_close(self):
        resp = self.track(ticker="AAPL", strategy="value")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pick"]["entry_price"], 191.25)
        self.assertEqual(resp.json()["entry_price_source"], "daily_close")

    # --- 진입가: 실시간 현재가 우선, 일봉 종가는 폴백 (2026-11 변경) ----------------

    def test_track_without_entry_price_prefers_realtime_quote(self):
        # 일봉 종가(AAPL 191.25)보다 실시간 현재가가 우선해야 한다.
        quotes = self.stub_quotes({"AAPL": (205.4, "finnhub_quote")})
        body = self.track(ticker="AAPL", strategy="value").json()
        self.assertEqual(body["pick"]["entry_price"], 205.4)
        self.assertEqual(body["entry_price_source"], "finnhub_quote")
        self.assertEqual(quotes, [["AAPL"]])               # 진입가 하나만 조회
        self.assertEqual(self.picks()[0]["entry_price"], 205.4)   # 저장된 값도 실시간가

    def test_track_without_entry_price_prefers_naver_realtime_for_korean(self):
        self.stub_prices({"005930.KS": 180000.0})
        self.stub_quotes({"005930.KS": (183200.0, "naver_realtime")})
        body = self.track(ticker="005930.KS", strategy="value").json()
        self.assertEqual(body["pick"]["entry_price"], 183200.0)
        self.assertEqual(body["entry_price_source"], "naver_realtime")

    def test_track_falls_back_to_daily_close_when_realtime_fails(self):
        with patch.object(realtime_quotes, "get_prices", side_effect=RuntimeError("quote down")):
            body = self.track(ticker="AAPL", strategy="value").json()
        self.assertEqual(body["pick"]["entry_price"], 191.25)   # 일봉 종가로 폴백
        self.assertEqual(body["entry_price_source"], "daily_close")

    def test_track_ignores_zero_or_negative_quote(self):
        for price in (0, -1.0, None, "nope"):
            with self.subTest(quote=price):
                self.stub_quotes({"MSFT": (price, "finnhub_quote")})
                body = self.track(ticker="MSFT", strategy=f"q{price}").json()
                self.assertEqual(body["pick"]["entry_price"], 410.0)
                self.assertEqual(body["entry_price_source"], "daily_close")

    def test_track_explicit_entry_price_wins_and_skips_lookup(self):
        quotes = self.stub_quotes({"AAPL": (205.4, "finnhub_quote")})
        body = self.track(ticker="AAPL", entry_price=100.0, strategy="value").json()
        self.assertEqual(body["pick"]["entry_price"], 100.0)    # 클라이언트 값 그대로
        self.assertIsNone(body["entry_price_source"])
        self.assertEqual(quotes, [])                             # 조회 자체를 하지 않는다

    def test_track_400_when_neither_realtime_nor_close(self):
        self.stub_prices({})
        self.stub_quotes({})
        resp = self.track(ticker="ZZZZ", strategy="value")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.picks(), [])

    def test_track_deduped_row_reports_no_entry_source(self):
        self.track(ticker="AAPL", entry_price=100.0, strategy="value")
        self.stub_quotes({"AAPL": (205.4, "finnhub_quote")})
        body = self.track(ticker="AAPL", strategy="value").json()
        self.assertFalse(body["created"])
        self.assertEqual(body["pick"]["entry_price"], 100.0)     # 첫 진입가 유지
        self.assertIsNone(body["entry_price_source"])            # 이 요청이 저장한 값이 아니다

    def test_track_treats_zero_or_negative_price_as_missing(self):
        for bad in (0, -3.5, None):
            with self.subTest(price=bad):
                body = self.track(ticker="MSFT", entry_price=bad, strategy=f"s{bad}").json()
                self.assertEqual(body["pick"]["entry_price"], 410.0)

    def test_track_treats_nan_or_inf_price_as_missing(self):
        # NaN·Infinity는 표준 JSON이 아니라 원문으로 보낸다(json 모듈은 받아준다).
        for raw in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(price=raw):
                resp = self.client.post(
                    "/api/performance/track",
                    content=f'{{"ticker": "MSFT", "entry_price": {raw}, "strategy": "{raw}"}}',
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["pick"]["entry_price"], 410.0)

    def test_track_unknown_ticker_is_400(self):
        self.stub_prices({})                        # 어떤 티커도 가격이 없다
        resp = self.track(ticker="ZZZZ", strategy="value")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "invalid_ticker")
        self.assertEqual(self.picks(), [])

    def test_track_empty_or_bad_ticker_is_400(self):
        for ticker in ("", "   ", "DROP TABLE", "AAPL;--", "A" * 20):
            with self.subTest(ticker=ticker):
                resp = self.track(ticker=ticker, entry_price=10.0)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.picks(), [])

    def test_track_same_day_same_strategy_keeps_one_row_and_first_entry(self):
        first = self.track(ticker="AAPL", entry_price=100.0, strategy="value",
                           criteria="첫 조건").json()
        second = self.track(ticker="AAPL", entry_price=120.0, strategy="value",
                            criteria="두 번째 조건").json()
        self.assertFalse(second["created"])
        self.assertEqual(second["pick"]["id"], first["pick"]["id"])
        self.assertEqual(second["pick"]["entry_price"], 100.0)   # 최초 진입가 유지
        self.assertEqual(second["pick"]["criteria"], "두 번째 조건")
        self.assertEqual(len(self.picks()), 1)

    def test_track_different_day_or_strategy_is_separate_row(self):
        self.track(ticker="AAPL", entry_price=100.0, strategy="value")
        self.track(ticker="AAPL", entry_price=100.0, strategy="momentum")
        self.track(ticker="AAPL", entry_price=100.0, strategy="value")
        self.assertEqual(len(self.picks()), 2)
        self.assertEqual(len({r["id"] for r in self.picks()}), 2)

    def test_tracked_pick_appears_in_performance_with_id(self):
        tracked = self.track(ticker="NVDA", entry_price=150.96, strategy="oversold").json()
        body = self.client.get("/api/performance").json()
        self.assertEqual(body["summary"]["total_picks"], 1)
        row = body["picks"][0]
        self.assertEqual(row["id"], tracked["pick"]["id"])
        self.assertEqual(row["ticker"], "NVDA")
        self.assertEqual(row["current_price"], 150.96)

    def test_track_reports_storage_failure_as_503(self):
        with patch.object(history_store, "record_pick",
                          side_effect=sqlite3_like_error()):
            resp = self.track(ticker="AAPL", entry_price=10.0)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "storage_unavailable")


# ---------------------------------------------------------------------------
# 7. 개별 삭제: DELETE /api/performance/{id}
# ---------------------------------------------------------------------------


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class DeleteApiTests(StubPricesMixin, TempDB):
    def setUp(self):
        super().setUp()
        self.stub_prices()
        self.stub_quotes()                                  # 성적표 현재가는 일봉 종가 고정
        self.client = TestClient(api.app)

    def seed(self):
        """세 행을 서로 다른 전략으로 만든다(같은 티커도 행이 갈리도록)."""
        ids = {}
        for ticker, strategy in (("AAPL", "value"), ("MSFT", "momentum"), ("NVDA", "oversold")):
            body = self.client.post("/api/performance/track",
                                    json={"ticker": ticker, "entry_price": 100.0,
                                          "strategy": strategy}).json()
            ids[ticker] = body["pick"]["id"]
        return ids

    def test_delete_removes_only_that_row(self):
        ids = self.seed()
        resp = self.client.delete(f"/api/performance/{ids['MSFT']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "deleted_id": ids["MSFT"]})
        rows = self.picks()
        self.assertEqual(sorted(r["ticker"] for r in rows), ["AAPL", "NVDA"])
        self.assertNotIn(ids["MSFT"], [r["id"] for r in rows])

    def test_delete_updates_performance_summary(self):
        ids = self.seed()
        self.assertEqual(self.client.get("/api/performance").json()["summary"]["total_picks"], 3)
        self.client.delete(f"/api/performance/{ids['AAPL']}")
        body = self.client.get("/api/performance").json()
        self.assertEqual(body["summary"]["total_picks"], 2)
        self.assertEqual(sorted(p["ticker"] for p in body["picks"]), ["MSFT", "NVDA"])

    def test_delete_missing_id_is_404(self):
        ids = self.seed()
        gone = max(ids.values()) + 100
        resp = self.client.delete(f"/api/performance/{gone}")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "pick_not_found")
        self.assertEqual(len(self.picks()), 3)      # 아무것도 지워지지 않았다

    def test_delete_twice_returns_404_the_second_time(self):
        ids = self.seed()
        self.assertEqual(self.client.delete(f"/api/performance/{ids['AAPL']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/performance/{ids['AAPL']}").status_code, 404)
        self.assertEqual(len(self.picks()), 2)

    def test_delete_non_positive_or_non_numeric_id_does_nothing(self):
        self.seed()
        self.assertEqual(self.client.delete("/api/performance/0").status_code, 404)
        self.assertEqual(self.client.delete("/api/performance/-7").status_code, 404)
        self.assertEqual(self.client.delete("/api/performance/abc").status_code, 422)
        self.assertEqual(len(self.picks()), 3)

    def test_delete_reports_storage_failure_as_503(self):
        ids = self.seed()
        with patch.object(history_store, "delete_pick", side_effect=sqlite3_like_error()):
            resp = self.client.delete(f"/api/performance/{ids['AAPL']}")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(len(self.picks()), 3)


# ---------------------------------------------------------------------------
# 8. history_store 직접 호출
# ---------------------------------------------------------------------------


class ManualTrackStoreTests(TempDB):
    def test_record_pick_returns_row_with_id(self):
        out = history_store.record_pick("nvda", entry_price=150.96, strategy="oversold")
        self.assertTrue(out["ok"])
        self.assertTrue(out["created"])
        self.assertEqual(out["pick"]["ticker"], "NVDA")
        self.assertIsInstance(out["pick"]["id"], int)
        self.assertEqual(history_store.recent_picks()[0]["id"], out["pick"]["id"])

    def test_record_pick_requires_ticker(self):
        with self.assertRaises(ValueError):
            history_store.record_pick("   ", entry_price=1.0)

    def test_delete_pick_only_target_and_false_when_absent(self):
        a = history_store.record_pick("AAPL", entry_price=1.0, strategy="value")["pick"]["id"]
        b = history_store.record_pick("MSFT", entry_price=2.0, strategy="value")["pick"]["id"]
        self.assertTrue(history_store.delete_pick(b))
        self.assertEqual([r["id"] for r in history_store.recent_picks()], [a])
        self.assertFalse(history_store.delete_pick(b))
        self.assertFalse(history_store.delete_pick(0))
        self.assertFalse(history_store.delete_pick("abc"))
        self.assertEqual(len(history_store.recent_picks()), 1)


def sqlite3_like_error():
    import sqlite3
    return sqlite3.OperationalError("database is locked")


if __name__ == "__main__":
    unittest.main()
