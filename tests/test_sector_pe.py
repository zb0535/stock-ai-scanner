"""동종업계(업종) 기준 PER 비교 테스트.

무엇을 검증하나
--------------
1. `sector_pe` — 업종명 정리, PER 표본 기록(적자·결측 제외), 섹터 중앙값(최소 표본·자기 제외·
   TTL), `fill_sector_pe`가 `sector`/`industry_pe`를 채우되 이미 있는 값(네이버 업종 PER)은
   덮어쓰지 않는지.
2. `scoring` — `industry_pe`가 있으면 절대 PER만 보지 않고 **업종 대비 할인/할증**을 반영한다.
   (업종이 비싼 곳의 25배가, 업종이 싼 곳의 12배보다 가치 점수가 높다.)
3. `naver_finance` — 종목 통합 응답의 `industryCode`·`industryCompareInfo`로 업종명과
   업종 기준 PER(이웃 PER 중앙값)을 만든다. 표본이 모자라면 None.
4. `/api/screen` — 프롬프트(`_PROMPT_ROW_FIELDS`)와 응답 행에 `sector`·`industry_pe`가 실린다.

실행: python -m unittest discover -s tests -v
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import naver_finance  # noqa: E402
import scoring  # noqa: E402
import sector_pe  # noqa: E402
import stock_tools  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


def row(**overrides):
    base = {
        "ticker": "TEST", "name": "Test Co", "currency": "USD", "price": 100.0,
        "ma20": 100.0, "ma60": 100.0, "rsi14": 50.0, "trend": "횡보/혼조",
        "trailing_pe": 15.0, "forward_pe": 14.0, "revenue_growth": 0.10,
        "operating_margins": 0.15, "debt_to_equity": 60.0,
        "macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0, "macd_cross": "none",
        "macd_state": "equal", "bb_upper": 105.0, "bb_lower": 95.0, "bb_pct_b": 0.5,
        "volume_ratio": 1.0, "drawdown_from_high_pct": -5.0, "range_position_pct": 50.0,
    }
    base.update(overrides)
    return base


# --- 1. sector_pe 코어 -------------------------------------------------------

class SectorSampleTests(unittest.TestCase):
    def setUp(self):
        sector_pe.clear_cache()

    def tearDown(self):
        sector_pe.clear_cache()

    def test_normalize_sector(self):
        self.assertEqual(sector_pe.normalize_sector("  Industrials "), "Industrials")
        for raw in (None, "", "  ", "N/A", "nan", "-"):
            self.assertIsNone(sector_pe.normalize_sector(raw), repr(raw))

    def test_us_sector_names_are_unified_to_gics(self):
        # Yahoo "Technology"와 S&P 500 표 "Information Technology"가 같은 표본으로 모인다.
        self.assertEqual(sector_pe.normalize_sector("Technology"), "Information Technology")
        self.assertEqual(sector_pe.normalize_sector("Information Technology"),
                         "Information Technology")
        self.assertEqual(sector_pe.normalize_sector("Financial Services"), "Financials")
        # 한국 업종명은 그대로 둔다.
        self.assertEqual(sector_pe.normalize_sector("반도체와반도체장비"), "반도체와반도체장비")

    def test_record_ignores_non_positive_or_missing_pe(self):
        self.assertFalse(sector_pe.record_pe("Tech", "AAA", None))
        self.assertFalse(sector_pe.record_pe("Tech", "AAA", 0))
        self.assertFalse(sector_pe.record_pe("Tech", "AAA", -4.0))
        self.assertFalse(sector_pe.record_pe("Tech", "AAA", float("nan")))
        self.assertFalse(sector_pe.record_pe("", "AAA", 12.0))
        self.assertTrue(sector_pe.record_pe("Tech", "AAA", 12.0))

    def test_median_needs_minimum_samples(self):
        for idx, pe in enumerate((10.0, 12.0, 14.0)):
            sector_pe.record_pe("Tech", f"T{idx}", pe)
        median, count = sector_pe.sector_median_pe("Tech")
        self.assertIsNone(median)          # 3개 < MIN_SAMPLES(4)
        self.assertEqual(count, 3)

        sector_pe.record_pe("Tech", "T3", 40.0)
        median, count = sector_pe.sector_median_pe("Tech")
        self.assertEqual(count, 4)
        self.assertAlmostEqual(median, 13.0, places=2)   # (12+14)/2 — 40배 이상치에 흔들리지 않는다

    def test_excludes_self_and_respects_ttl(self):
        for idx, pe in enumerate((10.0, 11.0, 12.0, 13.0, 14.0)):
            sector_pe.record_pe("Tech", f"T{idx}", pe)
        median, count = sector_pe.sector_median_pe("Tech", exclude_ticker="T0")
        self.assertEqual(count, 4)
        self.assertAlmostEqual(median, 12.5, places=2)    # [11,12,13,14]의 중간값

        reference = time.time()
        with patch.object(sector_pe.time, "time",
                          lambda: reference + sector_pe.PE_SAMPLE_TTL + 1):
            stale, stale_count = sector_pe.sector_median_pe("Tech")
        self.assertIsNone(stale)
        self.assertEqual(stale_count, 0)

    def test_fill_sector_pe_uses_table_and_samples(self):
        rows = [row(ticker=f"T{idx}", sector="Technology", trailing_pe=10.0 + idx)
                for idx in range(5)]   # 5종목 → 자기 제외해도 4표본(최소 기준)
        rows.append(row(ticker="K1", sector="반도체", industry_pe=28.9, trailing_pe=5.7, currency="KRW"))
        rows.append(row(ticker="UNKNOWN", trailing_pe=9.0, sector=None))
        with patch.object(sector_pe, "us_sector", lambda ticker: "Technology" if ticker.startswith("T") else None):
            sector_pe.fill_sector_pe(rows)
        kr = next(item for item in rows if item["ticker"] == "K1")
        unknown = next(item for item in rows if item["ticker"] == "UNKNOWN")
        # 네이버가 준 업종 PER은 덮어쓰지 않는다.
        self.assertEqual(kr["industry_pe"], 28.9)
        # 섹터 표본이 충분하면 같은 섹터 종목들의 중앙값이 붙는다(자기 제외: [11,12,13,14]).
        self.assertEqual(rows[0]["industry_pe"], 12.5)
        self.assertIn("Technology", rows[0]["industry_pe_basis"])
        # 섹터를 모르는 종목은 업종 PER을 지어내지 않는다.
        self.assertIsNone(unknown.get("industry_pe"))
        self.assertTrue(unknown.get("sector") is None or unknown["sector"] == "")

    def test_sample_status_counts_live_samples(self):
        sector_pe.record_pe("Tech", "AAA", 12.0)
        self.assertEqual(sector_pe.sample_status(), {"Tech": 1})


# --- 2. 가치 점수의 업종 대비 평가 ------------------------------------------

class IndustryPeScoringTests(unittest.TestCase):
    def test_discount_to_industry_adds_score_and_signal(self):
        cheap_vs_peers, signals = scoring.score_stock(
            row(trailing_pe=18.0, industry_pe=30.0), "value")
        pricey_vs_peers, _ = scoring.score_stock(
            row(trailing_pe=18.0, industry_pe=10.0), "value")
        self.assertGreater(cheap_vs_peers, pricey_vs_peers)
        self.assertTrue(any(sig.startswith("업종대비저PER") for sig in signals))

    def test_relative_comparison_can_beat_a_lower_absolute_per(self):
        # 업종이 비싼 곳(45배)의 24배 vs 업종이 싼 곳(8배)의 12배 → 업종 기준으로는 앞쪽이 싸다.
        relative_cheap, signals = scoring.score_stock(
            row(ticker="REL", trailing_pe=24.0, industry_pe=45.0), "value")
        absolute_cheap, _ = scoring.score_stock(
            row(ticker="ABS", trailing_pe=12.0, industry_pe=8.0), "value")
        self.assertGreater(relative_cheap, absolute_cheap)
        self.assertTrue(any(sig.startswith("업종대비저PER") for sig in signals))

    def test_industry_premium_is_penalised(self):
        premium, signals = scoring.score_stock(row(trailing_pe=16.0, industry_pe=9.0), "value")
        neutral, _ = scoring.score_stock(row(trailing_pe=16.0), "value")
        self.assertLess(premium, neutral)
        self.assertTrue(any(sig.startswith("업종대비고PER") for sig in signals))

    def test_missing_or_invalid_industry_pe_changes_nothing(self):
        baseline, _ = scoring.score_stock(row(trailing_pe=16.0), "value")
        for industry_pe in (None, 0, -5.0):
            score, signals = scoring.score_stock(row(trailing_pe=16.0, industry_pe=industry_pe), "value")
            self.assertEqual(score, baseline)
            self.assertFalse(any("업종" in sig for sig in signals), (industry_pe, signals))

    def test_balanced_strategy_only_nudges(self):
        with_peers, _ = scoring.score_stock(row(trailing_pe=18.0, industry_pe=40.0), "balanced")
        without, _ = scoring.score_stock(row(trailing_pe=18.0), "balanced")
        self.assertGreater(with_peers, without)
        self.assertLessEqual(with_peers - without, 5.0)


# --- 3. 네이버 동종업계 PER -------------------------------------------------

def _fake_naver_json(url, params=None, timeout=None):
    """네이버 응답 대역: 종목 통합·업종 목록·연간 재무."""
    if "/stocks/industry/" in url:
        return {"groupInfo": {"no": 278, "name": "반도체와반도체장비"}}
    if "/finance/annual" in url:
        return {"financeInfo": {"trTitleList": [{"key": "2024", "isConsensus": "N"},
                                                {"key": "2025", "isConsensus": "N"}],
                                "rowList": [
                                    {"title": "매출액", "columns": {
                                        "2024": {"value": "100000"}, "2025": {"value": "120000"}}},
                                    {"title": "영업이익률", "columns": {
                                        "2024": {"value": "10.0"}, "2025": {"value": "12.0"}}},
                                    {"title": "부채비율", "columns": {
                                        "2024": {"value": "40.0"}, "2025": {"value": "35.0"}}}]}}
    if "/integration" in url:
        code = url.rsplit("/stock/", 1)[-1].split("/")[0]
        peers = {"005930": ["000660", "042700", "240810", "036490"],
                 "000660": [], "042700": [], "240810": [], "036490": []}
        per = {"005930": "5.69배", "000660": "4.20배", "042700": "18.00배",
               "240810": "22.40배", "036490": "N/A"}
        return {
            "itemCode": code,
            "stockName": "삼성전자" if code == "005930" else f"종목{code}",
            "industryCode": "278",
            "industryCompareInfo": [{"itemCode": peer} for peer in peers.get(code, [])],
            "totalInfos": [{"code": "per", "value": per.get(code, "N/A")},
                           {"code": "cnsPer", "value": "4.5배"}],
        }
    return None


class NaverIndustryTests(unittest.TestCase):
    def setUp(self):
        naver_finance.clear_cache()

    def tearDown(self):
        naver_finance.clear_cache()

    def test_fundamentals_include_sector_and_industry_pe(self):
        with patch.object(naver_finance, "_get_json", _fake_naver_json):
            fin = naver_finance.get_fundamentals("005930.KS")
        self.assertEqual(fin["sector"], "반도체와반도체장비")
        self.assertAlmostEqual(fin["trailing_pe"], 5.69, places=2)
        # 이웃 4종목 중 PER이 있는 3개(4.2·18.0·22.4)의 중앙값 = 18.0 (N/A은 제외)
        self.assertAlmostEqual(fin["industry_pe"], 18.0, places=2)

    def test_industry_pe_is_none_when_peers_are_unknown(self):
        with patch.object(naver_finance, "_get_json", _fake_naver_json):
            fin = naver_finance.get_fundamentals("000660.KS")   # 이웃 없음
        self.assertIsNone(fin["industry_pe"])
        self.assertEqual(fin["sector"], "반도체와반도체장비")

    def test_peer_median_needs_enough_samples(self):
        calls = []

        def only_two(url, params=None, timeout=None):
            calls.append(url)
            code = url.rsplit("/stock/", 1)[-1].split("/")[0]
            per = {"000660": "4.2배", "042700": "18배", "240810": "N/A"}
            return {"totalInfos": [{"code": "per", "value": per.get(code, "N/A")}]}

        with patch.object(naver_finance, "_get_json", only_two):
            self.assertIsNone(naver_finance._peer_median(["000660", "042700", "240810"]))
        self.assertEqual(len(calls), 3)

    def test_industry_name_failure_is_none(self):
        with patch.object(naver_finance, "_get_json", lambda *a, **k: None):
            self.assertIsNone(naver_finance._industry_name("278"))


# --- 4. /api/screen 프롬프트·응답 -------------------------------------------

def flat_hist(days=130):
    close = np.linspace(100.0, 150.0, days)
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-20"), periods=days)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": [1_000_000.0] * days}, index=idx)


class _FakeModels:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)

        class Resp:
            text = "```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```"
        return Resp()


class _FakeGenai:
    def __init__(self, payload):
        self.models = _FakeModels(payload)


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ScreenSectorPromptTests(unittest.TestCase):
    def setUp(self):
        sector_pe.clear_cache()
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        api.clear_analysis_cache()
        api.realtime_quotes.clear_cache()
        self._patches = []
        self.patch_attr(api.realtime_quotes, "get_prices", lambda *a, **k: {})
        self.patch_attr(api, "_get_recent_news", lambda ticker, limit=3: [])
        self.patch_attr(stock_tools, "get_history", lambda ticker, period="6mo": flat_hist())
        self.fake = _FakeGenai({"market_note": "ok", "top_picks": []})
        self.patch_attr(api.genai, "Client", lambda **_: self.fake)

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()
        sector_pe.clear_cache()

    def patch_attr(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self._patches.append(patcher)
        return value

    def test_system_prompt_forbids_industry_claims_without_data(self):
        self.assertIn("industry_pe", api.SYSTEM_PROMPT)
        self.assertIn("업종 비교를 아예 언급하지 마라", api.SYSTEM_PROMPT)

    def test_prompt_and_rows_carry_sector_and_industry_pe(self):
        metrics = {
            "AAA": row(ticker="AAA", name="Alpha", sector="Technology", trailing_pe=18.0),
            "BBB": row(ticker="BBB", name="Beta", sector="Technology", trailing_pe=24.0),
            "CCC": row(ticker="CCC", name="Gamma", sector="Technology", trailing_pe=30.0),
            "DDD": row(ticker="DDD", name="Delta", sector="Technology", trailing_pe=21.0),
            "EEE": row(ticker="EEE", name="Epsilon", sector="Technology", trailing_pe=12.0),
            "000660.KS": row(ticker="000660.KS", name="SK하이닉스", currency="KRW",
                      sector="반도체와반도체장비", industry_pe=28.9, trailing_pe=4.2),
        }
        self.patch_attr(stock_tools, "get_stock_metrics", lambda ticker: dict(metrics[ticker]))

        body = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주",
                                 "tickers": ["AAA", "BBB", "CCC", "DDD", "EEE", "000660.KS"]}).json()

        rows = {r["ticker"]: r for r in body["all_stocks"]}
        self.assertEqual(rows["AAA"]["sector"], "Information Technology")
        # 섹터 표본으로 중앙값이 채워진다(자기 제외: 12·21·24·30 → 22.5).
        self.assertAlmostEqual(rows["AAA"]["industry_pe"], 22.5, places=2)
        # 네이버가 준 업종 PER은 그대로 유지된다.
        self.assertAlmostEqual(rows["000660.KS"]["industry_pe"], 28.9, places=2)

        prompt = self.fake.models.calls[0]["contents"]
        self.assertIn("industry_pe", prompt)
        self.assertIn("sector", prompt)
        self.assertIn("Information Technology", prompt)
        self.assertIn("업종대비저PER", prompt)     # 4.2 vs 업종 28.9 신호가 프롬프트에 실린다


if __name__ == "__main__":
    unittest.main()
