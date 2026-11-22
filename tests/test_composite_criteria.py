"""복합 조건(전략 + 세부 조건) 스크리닝 테스트.

무엇을 검증하나
--------------
1. `scoring.detect_intents` / `parse_criteria` — 문장에서 세부 조건(상승추세/정배열, 하락/눌림목,
   골든크로스, 거래량 급증, 저PER, 재무 안정)을 읽는다(상승·하락 동시 적중은 더 구체적인 쪽).
2. `scoring.trend_state` — trend 문구 → 이평선 값 순으로 정배열/역배열/혼조/판단불가.
3. `scoring.score_stock(intents=…)` — 정배열 조건이 붙으면 정배열 종목 가점·역배열 감점,
   signals에 `정배열일치` / `역배열(조건밖)`가 드러난다.
4. `scoring.rank_stocks` — **같은 후보군·같은 전략(value)인데 조건만 다르면 1등이 바뀐다**
   (2026-11 사용자 신고: "상승추세(정배열) 이면서 … 가치주"가 기본 PER 프리셋과 똑같던 문제).
5. `scoring.score_prescan` + `sp500_scanner.rank_rows` — 프리 스캔에서도 추세 조건이 후보를
   바꾸고, 조건 일치가 top_n보다 적으면 빈 결과 대신 나머지로 채운다(안전 폴백).
6. `/api/screen` — strategy는 value 그대로, `conditions`에 uptrend가 실리고, 프롬프트에 필수
   조건이 들어가며, 기본 프리셋과 all_stocks 순위가 달라진다.

실행: python -m unittest discover -s tests -v
"""
import json
import sys
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


# 사용자가 실제로 입력한 문장(프론트 프리셋 + 복합 조건).
VALUE_PRESET = ("Trailing PER 또는 Forward PER이 상대적으로 낮고 영업이익률과 부채비율이 "
                "안정적인 가치주 상위 3개를 추천해 줘.")
COMPOSITE_PRESET = ("상승추세(정배열) 이면서 Trailing PER 또는 Forward PER이 상대적으로 낮고 "
                    "영업이익률과 부채비율이 안정적인 가치주 상위 3개를 추천해 줘.")
MOMENTUM_PRESET = ("상승 추세(정배열)이면서 RSI 70 미만으로 과열되지 않았고, 매출 성장률과 "
                   "영업이익률이 탄탄한 상위 3개 종목을 추천해 줘.")

UP_TREND = "상승 추세(정배열: 현재가>20일선>60일선)"
DOWN_TREND = "하락 추세(역배열: 현재가<20일선<60일선)"


def row(**overrides):
    """지표·재무가 채워진 한 종목(테스트 기본형)."""
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


def up_row(**overrides):
    base = dict(price=120.0, ma20=115.0, ma60=100.0, trend=UP_TREND, rsi14=60.0,
                range_position_pct=85.0, drawdown_from_high_pct=-2.0)
    base.update(overrides)
    return row(**base)


def down_row(**overrides):
    base = dict(price=80.0, ma20=90.0, ma60=100.0, trend=DOWN_TREND, rsi14=35.0,
                range_position_pct=20.0, drawdown_from_high_pct=-25.0)
    base.update(overrides)
    return row(**base)


# --- 1. 세부 조건 해석 ------------------------------------------------------

class DetectIntentsTests(unittest.TestCase):
    def test_value_preset_has_no_trend_requirement(self):
        parsed = scoring.parse_criteria(VALUE_PRESET)
        self.assertEqual(parsed["strategy"], "value")
        self.assertEqual(parsed["intents"], ["low_pe", "quality"])

    def test_composite_preset_reads_uptrend_and_keeps_value_strategy(self):
        parsed = scoring.parse_criteria(COMPOSITE_PRESET)
        self.assertEqual(parsed["strategy"], "value")       # 점수 공식은 여전히 가치
        self.assertEqual(parsed["intents"], ["uptrend", "low_pe", "quality"])

    def test_momentum_preset_has_trend_intent(self):
        parsed = scoring.parse_criteria(MOMENTUM_PRESET)
        self.assertEqual(parsed["strategy"], "momentum")
        self.assertIn("uptrend", parsed["intents"])

    def test_downtrend_keywords(self):
        for criteria in ("단기 눌림목 반등", "과매도 종목", "역배열 하락 추세"):
            self.assertIn("downtrend", scoring.detect_intents(criteria), criteria)
            self.assertNotIn("uptrend", scoring.detect_intents(criteria), criteria)

    def test_golden_cross_and_volume_surge(self):
        self.assertIn("golden_cross", scoring.detect_intents("골든크로스가 나온 종목"))
        self.assertIn("volume_surge", scoring.detect_intents("거래량 급증 종목"))
        self.assertIn("golden_cross", scoring.detect_intents("Golden Cross"))

    def test_opposing_intents_keep_the_more_specific_one(self):
        # "상승추세" 1건 vs "눌림목·과매도" 2건 → 눌림목 쪽만 남는다.
        intents = scoring.detect_intents("상승추세 이후 눌림목 과매도")
        self.assertIn("downtrend", intents)
        self.assertNotIn("uptrend", intents)

    def test_empty_and_unknown(self):
        for criteria in ("", None, "   ", "미국 대형주 아무거나"):
            self.assertEqual(scoring.detect_intents(criteria), [], repr(criteria))


class TrendStateTests(unittest.TestCase):
    def test_trend_text_wins(self):
        self.assertEqual(scoring.trend_state({"trend": UP_TREND}), "up")
        self.assertEqual(scoring.trend_state({"trend": DOWN_TREND}), "down")

    def test_falls_back_to_moving_averages(self):
        self.assertEqual(scoring.trend_state({"price": 120, "ma20": 110, "ma60": 100}), "up")
        self.assertEqual(scoring.trend_state({"price": 90, "ma20": 100, "ma60": 110}), "down")
        self.assertEqual(scoring.trend_state({"price": 105, "ma20": 100, "ma60": 110}), "mixed")

    def test_missing_data_is_unknown(self):
        self.assertIsNone(scoring.trend_state({"price": 100.0}))
        self.assertIsNone(scoring.trend_state({}))
        self.assertIsNone(scoring.trend_state(None))


# --- 2. 조건 가감점 ---------------------------------------------------------

class IntentScoringTests(unittest.TestCase):
    def test_uptrend_intent_rewards_uptrend_and_punishes_downtrend(self):
        up, up_signals = scoring.score_stock(up_row(), "value", intents=["uptrend"])
        down, down_signals = scoring.score_stock(down_row(), "value", intents=["uptrend"])
        self.assertIn("정배열일치", up_signals)
        self.assertIn("역배열(조건밖)", down_signals)
        self.assertGreater(up - down, 30.0)   # 조건이 순위를 뒤집을 만큼 벌어진다

    def test_value_formula_alone_can_prefer_the_downtrend_stock(self):
        """기본 가치 공식은 (같은 재무라면) PER이 더 낮은 역배열 종목을 앞세운다 — 이게 원래 버그."""
        up, _ = scoring.score_stock(up_row(trailing_pe=9.0), "value")
        down, _ = scoring.score_stock(down_row(trailing_pe=7.0), "value")
        self.assertGreaterEqual(down, up)

    def test_no_intents_keeps_previous_scores(self):
        stock = up_row(trailing_pe=9.0)
        self.assertEqual(scoring.score_stock(stock, "value"),
                         scoring.score_stock(stock, "value", intents=None))

    def test_missing_trend_data_is_not_penalised(self):
        bare = {"ticker": "BARE", "trailing_pe": 10.0}
        plain, _ = scoring.score_stock(bare, "value")
        conditioned, signals = scoring.score_stock(bare, "value", intents=["uptrend"])
        self.assertEqual(plain, conditioned)
        self.assertFalse(any("정배열" in sig or "역배열" in sig for sig in signals))

    def test_golden_cross_and_volume_intents(self):
        good, signals = scoring.score_stock(up_row(macd_cross="bullish", volume_ratio=2.1),
                                            "momentum", intents=["golden_cross", "volume_surge"])
        bad, bad_signals = scoring.score_stock(up_row(macd_cross="bearish", volume_ratio=0.5),
                                               "momentum", intents=["golden_cross", "volume_surge"])
        self.assertGreater(good, bad)
        self.assertIn("골든크로스일치", signals)
        self.assertIn("데드크로스(조건밖)", bad_signals)
        self.assertTrue(any(sig.startswith("거래량급증일치") for sig in signals))


class FallbackAnalysisTests(unittest.TestCase):
    """AI 호출이 실패한 폴백 경로에서도 조건을 지키는지."""

    def test_fallback_prefers_condition_matching_rows(self):
        rows = [
            dict(down_row(ticker="DOWN"), quant_score=90.0, price=80.0, signals=["저PER(6.0)"]),
            dict(up_row(ticker="UP"), quant_score=70.0, price=120.0, signals=["정배열일치"]),
            dict(up_row(ticker="UP2"), quant_score=65.0, price=130.0, signals=["정배열일치"]),
        ]
        data = api._fallback_analysis(rows, "AI 실패", intents=["uptrend"])
        self.assertEqual([p["ticker"] for p in data["top_picks"]], ["UP", "UP2", "DOWN"])

    def test_fallback_says_so_when_no_row_satisfies_the_condition(self):
        rows = [dict(down_row(ticker="DOWN"), quant_score=90.0, price=80.0, signals=["저PER(6.0)"]),
                dict(down_row(ticker="DOWN2"), quant_score=80.0, price=70.0, signals=["저PER(7.0)"])]
        data = api._fallback_analysis(rows, "AI 실패", intents=["uptrend"])
        self.assertEqual([p["ticker"] for p in data["top_picks"]], ["DOWN", "DOWN2"])
        self.assertIn("충족하는 종목이 후보에 없어", data["market_note"])

    def test_fallback_without_conditions_keeps_score_order(self):
        rows = [dict(down_row(ticker="A"), quant_score=90.0, price=80.0, signals=[]),
                dict(up_row(ticker="B"), quant_score=70.0, price=120.0, signals=[])]
        data = api._fallback_analysis(rows, "AI 실패")
        self.assertEqual([p["ticker"] for p in data["top_picks"]], ["A", "B"])
        self.assertNotIn("충족하는 종목이 없어", data["market_note"])


class RankStocksCompositeTests(unittest.TestCase):
    ROWS = [
        # PER이 가장 낮지만 역배열 — 기본 가치 프리셋의 1등
        down_row(ticker="DOWN_CHEAP", trailing_pe=6.5),
        # PER은 조금 높지만 정배열 — 복합 조건의 1등
        up_row(ticker="UP_OK", trailing_pe=11.0),
        # 정배열이지만 비싼 종목
        up_row(ticker="UP_PRICEY", trailing_pe=31.0),
        # 역배열 + 보통 PER
        down_row(ticker="DOWN_MID", trailing_pe=14.0),
    ]

    def test_plain_value_and_composite_produce_different_order(self):
        plain, _ = scoring.rank_stocks(self.ROWS, criteria=VALUE_PRESET)
        composite, strategy = scoring.rank_stocks(self.ROWS, criteria=COMPOSITE_PRESET)
        self.assertEqual(strategy, "value")                       # 전략은 같다
        self.assertEqual(plain[0]["ticker"], "DOWN_CHEAP")
        self.assertEqual(composite[0]["ticker"], "UP_OK")
        self.assertNotEqual([r["ticker"] for r in plain], [r["ticker"] for r in composite])
        # 응답 필드는 그대로다(추가만).
        for item in composite:
            self.assertIn("quant_score", item)
            self.assertIn("signals", item)
        # 역배열 종목은 복합 조건에서 크게 밀린다.
        plain_scores = {r["ticker"]: r["quant_score"] for r in plain}
        composite_scores = {r["ticker"]: r["quant_score"] for r in composite}
        self.assertLess(composite_scores["DOWN_CHEAP"], plain_scores["DOWN_CHEAP"])


# --- 3. 프리 스캔 후보 선별 --------------------------------------------------

def prescan_row(ticker, trend, rsi=60.0, position=85.0, drawdown=-2.0, volume=1.2):
    base = dict(ticker=ticker, price=120.0, ma20=115.0, ma60=100.0, trend=UP_TREND,
                rsi14=rsi, range_position_pct=position, drawdown_from_high_pct=drawdown,
                volume_ratio=volume, macd_cross="none")
    if trend == "down":
        base.update(price=80.0, ma20=90.0, ma60=100.0, trend=DOWN_TREND)
    return row(**base)


class PrescanCompositeTests(unittest.TestCase):
    def setUp(self):
        self.rows = (
            [prescan_row(f"UP{i}", "up", rsi=58 + i, position=80 + i) for i in range(6)]
            + [prescan_row(f"DOWN{i}", "down", rsi=28 + i, position=12 + i, drawdown=-30 - i)
               for i in range(6)]
        )

    def test_value_preset_ranks_pullbacks_first(self):
        ranked = sp500_scanner.rank_rows(self.rows, "value", top_n=4)
        self.assertTrue(all(t["ticker"].startswith("DOWN") for t in ranked),
                        [t["ticker"] for t in ranked])

    def test_uptrend_intent_selects_uptrend_candidates(self):
        ranked = sp500_scanner.rank_rows(self.rows, "value", top_n=4, intents=["uptrend"])
        self.assertTrue(all(t["ticker"].startswith("UP") for t in ranked),
                        [t["ticker"] for t in ranked])
        self.assertTrue(all(any("정배열일치" in sig for sig in t["presignals"]) for t in ranked))

    def test_select_candidates_uses_the_condition(self):
        snapshot = {"rows": self.rows, "universe": 12, "skipped": 0, "cached": True, "stale": False}
        with patch.object(sp500_scanner, "prescan", lambda *a, **k: dict(snapshot)):
            plain = sp500_scanner.select_candidates(VALUE_PRESET, top_n=4)
            composite = sp500_scanner.select_candidates(COMPOSITE_PRESET, top_n=4)
        self.assertEqual(plain["strategy"], "value")
        self.assertEqual(composite["strategy"], "value")
        self.assertEqual(composite["intents"], ["uptrend", "low_pe", "quality"])
        self.assertNotEqual(plain["tickers"], composite["tickers"])
        self.assertTrue(all(t.startswith("UP") for t in composite["tickers"]))

    def test_not_enough_matches_falls_back_without_empty_result(self):
        # 정배열 조건을 만족하는 종목은 1개뿐(top_n=5) → 빈 결과 대신 나머지로 채운다.
        rows = self.rows[6:10] + [prescan_row("UPX", "up")]
        with patch.object(sp500_scanner, "prescan",
                          lambda *a, **k: {"rows": rows, "universe": 5, "skipped": 0,
                                           "cached": True, "stale": False}):
            result = sp500_scanner.select_candidates(COMPOSITE_PRESET, top_n=5)
        self.assertEqual(len(result["tickers"]), 5)          # 빈 결과가 아니다
        self.assertEqual(result["tickers"][0], "UPX")        # 일치 종목이 먼저
        self.assertEqual(len(set(result["tickers"])), 5)      # 중복 없음


# --- 4. /api/screen 통합 -----------------------------------------------------

def flat_hist(days=130):
    close = np.linspace(100.0, 150.0, days)
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-20"), periods=days)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": [1_000_000.0] * days}, index=idx)


class FakeModels:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)

        class Resp:
            text = "```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```"
        return Resp()


class FakeGenai:
    def __init__(self, payload):
        self.models = FakeModels(payload)


@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ScreenCompositeTests(unittest.TestCase):
    ROWS = {
        "DOWN_CHEAP": down_row(ticker="DOWN_CHEAP", name="Down Cheap", price=60.0,
                               trailing_pe=6.0, revenue_growth=0.2, operating_margins=0.3,
                               debt_to_equity=20.0),
        "UP_OK": up_row(ticker="UP_OK", name="Up Ok", price=120.0, trailing_pe=11.0,
                        revenue_growth=0.2, operating_margins=0.3, debt_to_equity=20.0),
        "UP_PRICEY": up_row(ticker="UP_PRICEY", name="Up Pricey", price=400.0,
                            trailing_pe=33.0, revenue_growth=0.05, operating_margins=0.1,
                            debt_to_equity=120.0),
    }

    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        api.clear_analysis_cache()
        api.realtime_quotes.clear_cache()
        self._patches = []
        self.patch_attr(api.realtime_quotes, "get_prices", lambda *a, **k: {})
        self.patch_attr(api, "_get_recent_news", lambda ticker, limit=3: [])
        self.patch_attr(stock_tools, "get_history", lambda ticker, period="6mo": flat_hist())
        self.patch_attr(stock_tools, "get_stock_metrics",
                        lambda ticker: dict(self.ROWS[ticker]))
        self.fake = FakeGenai({"market_note": "ok", "top_picks": []})
        self.patch_attr(api.genai, "Client", lambda **_: self.fake)

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()

    def patch_attr(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self._patches.append(patcher)
        return value

    def _screen(self, criteria, tickers=("DOWN_CHEAP", "UP_OK", "UP_PRICEY")):
        resp = TestClient(api.app).post("/api/screen", json={"criteria": criteria,
                                                            "tickers": list(tickers)})
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_composite_condition_changes_order_and_prompt(self):
        plain = self._screen(VALUE_PRESET)
        composite = self._screen(COMPOSITE_PRESET)

        self.assertEqual(plain["strategy"], "value")
        self.assertEqual(composite["strategy"], "value")          # 호환: 전략 이름 그대로
        self.assertEqual(plain["conditions"], ["low_pe", "quality"])
        self.assertEqual(composite["conditions"], ["uptrend", "low_pe", "quality"])

        self.assertEqual(plain["all_stocks"][0]["ticker"], "DOWN_CHEAP")
        self.assertEqual(composite["all_stocks"][0]["ticker"], "UP_OK")
        self.assertNotEqual([r["ticker"] for r in plain["all_stocks"]],
                            [r["ticker"] for r in composite["all_stocks"]])

        # 마지막 Gemini 프롬프트에 필수 조건이 실제로 전달된다.
        prompt = self.fake.models.calls[-1]["contents"]
        self.assertIn("필수 조건", prompt)
        self.assertIn("상승추세(정배열)", prompt)
        self.assertIn("required_conditions", prompt)

    def test_composite_row_keeps_contract_and_reveals_condition_signals(self):
        body = self._screen(COMPOSITE_PRESET)
        by_ticker = {r["ticker"]: r for r in body["all_stocks"]}
        for key in ("ticker", "name", "price", "ma20", "ma60", "rsi14", "trend",
                    "trailing_pe", "revenue_growth", "quant_score", "signals"):
            self.assertIn(key, by_ticker["UP_OK"])
        self.assertIn("정배열일치", by_ticker["UP_OK"]["signals"])
        self.assertIn("역배열(조건밖)", by_ticker["DOWN_CHEAP"]["signals"])
        self.assertEqual(body["analysis_cached"], False)
        self.assertEqual(body["analysis"]["top_picks"], [])


if __name__ == "__main__":
    unittest.main()
