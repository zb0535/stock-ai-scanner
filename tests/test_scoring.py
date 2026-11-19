"""고급 기술적 지표(MACD·볼린저·거래량 배수·고점 대비·구간 위치)와 정량 스코어링 엔진 테스트.

검증 대상
---------
1. `stock_tools.compute_advanced_indicators` — 6개월 일봉만으로 계산, 추가 네트워크 호출 없음,
   짧은 이력/거래량 없음/NaN에서 예외 없이 None, 기존 필드 보존
2. `scoring.detect_strategy` — criteria → value/oversold/momentum/balanced
3. `scoring.score_stock` — 전략별 결정적 점수(0~100)와 signals
4. `scoring.rank_stocks` — quant_score 내림차순, 동점은 입력 순서 유지, 입력 불변
5. `/api/screen` — 정렬·필드 부착·기존 응답 키 유지·strategy 포함

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
import stock_tools  # noqa: E402

try:  # TestClient는 httpx에 의존한다.
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


# --- 데이터 도우미 -----------------------------------------------------------

def flat_hist(close, volume=1_000_000.0, days=None):
    """종가 리스트(또는 배열)로 High/Low/Volume이 붙은 일봉 DataFrame을 만든다."""
    close = np.asarray(close, dtype="float64")
    days = len(close) if days is None else days
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-18"), periods=days)
    vol = volume if isinstance(volume, (list, tuple, np.ndarray)) else [volume] * days
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close,
         "Volume": np.asarray(vol, dtype="float64")},
        index=idx,
    )


def v_shape_hist(down_days=60, up_days=1):
    """60일 하락 뒤 급반등 → MACD 히스토그램이 최근 봉에서 +로 전환하는 모양."""
    close = np.concatenate([np.linspace(200.0, 120.0, down_days),
                            np.linspace(120.0, 120.0 + 6.0 * up_days, up_days)])
    return flat_hist(close)


def arc_hist(up_days=60, down_days=1):
    """상승 뒤 고점에서 꺾임 → MACD 히스토그램이 최근 봉에서 -로 전환하는 모양."""
    close = np.concatenate([np.linspace(100.0, 200.0, up_days),
                            np.linspace(200.0, 200.0 - 6.0 * down_days, down_days)])
    return flat_hist(close)


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


class FakeTicker:
    """_build_metrics 경로 검증용 yfinance.Ticker 대역(정보는 전부 채워 폴백을 막는다)."""

    def __init__(self, symbol, hist=None):
        self.symbol = symbol
        self._hist = hist

    @property
    def info(self):
        return {
            "shortName": f"{self.symbol} Inc.",
            "trailingPE": 14.8,
            "forwardPE": 13.1,
            "revenueGrowth": 0.12,
            "operatingMargins": 0.21,
            "debtToEquity": 42.0,
        }

    def history(self, period="6mo"):
        return self._hist if self._hist is not None else flat_hist(np.linspace(100, 150, 130))


# --- 1. 기술적 지표 ----------------------------------------------------------

class MacdTests(unittest.TestCase):
    def test_macd_matches_manual_ema_formula(self):
        hist = flat_hist(np.linspace(100.0, 150.0, 130))
        close = hist["Close"]
        got = stock_tools._calc_macd(close)

        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=9, adjust=False).mean()
        self.assertAlmostEqual(got["macd"], round(float(macd.iloc[-1]), 4), places=4)
        self.assertAlmostEqual(got["macd_signal"], round(float(signal.iloc[-1]), 4), places=4)
        self.assertAlmostEqual(got["macd_hist"],
                               round(float(macd.iloc[-1] - signal.iloc[-1]), 4), places=4)

    def test_bullish_cross_when_hist_turns_positive_within_3_bars(self):
        got = stock_tools.compute_advanced_indicators(v_shape_hist())
        self.assertGreater(got["macd_hist"], 0)
        self.assertEqual(got["macd_cross"], "bullish")
        self.assertEqual(got["macd_state"], "above")

    def test_bearish_cross_when_hist_turns_negative_within_3_bars(self):
        got = stock_tools.compute_advanced_indicators(arc_hist())
        self.assertLess(got["macd_hist"], 0)
        self.assertEqual(got["macd_cross"], "bearish")
        self.assertEqual(got["macd_state"], "below")

    def test_steady_uptrend_has_no_recent_cross(self):
        # 완만한 단조 상승은 MACD가 시그널 위에 머물러 '최근 3봉 크로스'가 없다.
        got = stock_tools.compute_advanced_indicators(flat_hist(np.linspace(100.0, 150.0, 130)))
        self.assertEqual(got["macd_cross"], "none")
        self.assertEqual(got["macd_state"], "above")


class BollingerTests(unittest.TestCase):
    def test_band_and_pct_b_math(self):
        # 19일 100, 마지막 110 → 평균 100.5, 모집단 표준편차 ≈ 2.1794
        close = np.array([100.0] * 19 + [110.0])
        got = stock_tools._calc_bollinger(pd.Series(close))
        self.assertAlmostEqual(got["bb_upper"], round(100.5 + 2 * 2.179449, 2), places=2)
        self.assertAlmostEqual(got["bb_lower"], round(100.5 - 2 * 2.179449, 2), places=2)
        # 상단(≈104.86)보다 위 → %B > 1
        self.assertGreater(got["bb_pct_b"], 1.0)

    def test_price_at_lower_band_gives_zero_pct_b(self):
        close = np.array([102.0] * 19 + [98.0])
        got = stock_tools._calc_bollinger(pd.Series(close))
        self.assertLess(got["bb_pct_b"], 0.0)   # 하단 이탈이면 음수

    def test_flat_prices_guarded(self):
        got = stock_tools._calc_bollinger(pd.Series([100.0] * 30))
        self.assertEqual(got["bb_upper"], 100.0)
        self.assertEqual(got["bb_lower"], 100.0)
        self.assertIsNone(got["bb_pct_b"])      # 밴드 폭 0 → 0으로 나누지 않는다

    def test_short_history_returns_none(self):
        got = stock_tools._calc_bollinger(pd.Series([100.0] * 5))
        self.assertEqual(got, {"bb_upper": None, "bb_lower": None, "bb_pct_b": None})


class VolumeRatioTests(unittest.TestCase):
    def test_surge_ratio_excludes_latest_day_from_average(self):
        volumes = [1_000.0] * 20 + [2_000.0]
        self.assertEqual(stock_tools._calc_volume_ratio(pd.Series(volumes)), 2.0)

    def test_zero_average_guarded(self):
        self.assertIsNone(stock_tools._calc_volume_ratio(pd.Series([0.0] * 21)))

    def test_nan_latest_volume_returns_none(self):
        volumes = [1_000.0] * 20 + [float("nan")]
        self.assertIsNone(stock_tools._calc_volume_ratio(pd.Series(volumes)))

    def test_missing_volume_column_is_none(self):
        close = np.linspace(100.0, 150.0, 130)
        hist = flat_hist(close).drop(columns=["Volume"])
        self.assertIsNone(stock_tools.compute_advanced_indicators(hist)["volume_ratio"])


class RangePositionTests(unittest.TestCase):
    def test_drawdown_and_position(self):
        # 최고 200(중간) → 최저 100 → 현재 150
        close = np.concatenate([np.linspace(200.0, 100.0, 60), np.linspace(120.0, 150.0, 10)])
        got = stock_tools._calc_range_position(pd.Series(close))
        self.assertAlmostEqual(got["drawdown_from_high_pct"], -25.0, places=2)
        self.assertAlmostEqual(got["range_position_pct"], 50.0, places=2)

    def test_at_high_gives_zero_drawdown_and_full_position(self):
        close = np.linspace(100.0, 200.0, 60)
        got = stock_tools._calc_range_position(pd.Series(close))
        self.assertAlmostEqual(got["drawdown_from_high_pct"], 0.0, places=2)
        self.assertAlmostEqual(got["range_position_pct"], 100.0, places=2)

    def test_uses_intraday_high_low_when_available(self):
        close = pd.Series([100.0, 120.0, 110.0])
        high = pd.Series([101.0, 130.0, 111.0])
        low = pd.Series([99.0, 119.0, 90.0])
        got = stock_tools._calc_range_position(close, high, low)
        self.assertAlmostEqual(got["drawdown_from_high_pct"], round((110 - 130) / 130 * 100, 2))
        self.assertEqual(got["range_position_pct"], 50.0)   # (110-90)/(130-90)


class IndicatorSafetyTests(unittest.TestCase):
    def test_empty_or_short_history_is_all_none(self):
        for hist in (None, pd.DataFrame()):
            got = stock_tools.compute_advanced_indicators(hist)
            self.assertIsNone(got["macd"])
            self.assertIsNone(got["bb_upper"])
            self.assertIsNone(got["drawdown_from_high_pct"])
            self.assertEqual(got["macd_cross"], "none")
            self.assertEqual(set(got), set(stock_tools.INDICATOR_FIELDS))

        # 짧은 이력(3봉): MACD·볼린저는 워밍업이 모자라 None, 나머지도 예외 없이 채워진다.
        short = stock_tools.compute_advanced_indicators(flat_hist([100.0, 101.0, 102.0]))
        self.assertIsNone(short["macd"])
        self.assertIsNone(short["macd_hist"])
        self.assertIsNone(short["bb_upper"])
        self.assertIsNone(short["bb_pct_b"])
        self.assertEqual(short["macd_cross"], "none")
        self.assertAlmostEqual(short["drawdown_from_high_pct"], 0.0)   # 3봉으로도 계산은 된다

    def test_nan_in_close_does_not_raise(self):
        close = np.linspace(100.0, 150.0, 130).astype(float)
        close[0] = np.nan
        got = stock_tools.compute_advanced_indicators(flat_hist(close))
        self.assertEqual(set(got), set(stock_tools.INDICATOR_FIELDS))  # 예외 없이 dict 반환

    def test_metrics_payload_keeps_old_keys_and_adds_indicators(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        hist = v_shape_hist()
        with patch.object(stock_tools.yf, "Ticker", lambda sym: FakeTicker(sym, hist)):
            payload = stock_tools.get_stock_metrics("AAPL")

        for key in ("ticker", "name", "price", "ma20", "ma60", "rsi14", "trend",
                    "trailing_pe", "forward_pe", "revenue_growth", "operating_margins",
                    "debt_to_equity", "financials_available"):
            self.assertIn(key, payload)                       # 기존 응답 키 보존
        for key in stock_tools.INDICATOR_FIELDS:
            self.assertIn(key, payload)                       # 신규 지표 추가
        self.assertEqual(payload["macd_cross"], "bullish")
        self.assertGreater(payload["macd_hist"], 0)
        self.assertLess(payload["drawdown_from_high_pct"], 0)
        # JSON 직렬화 가능해야 한다(NaN/inf 없음)
        json.dumps(payload, allow_nan=False)


# --- 2. 전략 감지 ------------------------------------------------------------

class DetectStrategyTests(unittest.TestCase):
    def test_value_keywords(self):
        for criteria in ("저PER 가치주", "PER 낮은 종목", "저평가 대형주", "배당주"):
            self.assertEqual(scoring.detect_strategy(criteria), "value", criteria)

    def test_oversold_keywords(self):
        for criteria in ("단기 눌림목 반등", "과매도 종목", "급락 후 반등", "RSI 낮은 볼린저 하단"):
            self.assertEqual(scoring.detect_strategy(criteria), "oversold", criteria)

    def test_momentum_keywords(self):
        for criteria in ("모멘텀 돌파 종목", "골든크로스 상승 추세", "매출 성장주"):
            self.assertEqual(scoring.detect_strategy(criteria), "momentum", criteria)

    def test_english_and_case_insensitive(self):
        self.assertEqual(scoring.detect_strategy("low PER value stocks"), "value")
        self.assertEqual(scoring.detect_strategy("oversold RSI reversal"), "oversold")
        self.assertEqual(scoring.detect_strategy("Momentum breakout"), "momentum")

    def test_balanced_fallback(self):
        for criteria in ("", None, "   ", "미국 대형주 아무거나"):
            self.assertEqual(scoring.detect_strategy(criteria), "balanced", repr(criteria))

    def test_per_inside_another_word_is_not_a_value_hit(self):
        # "Superior" 같은 단어 안의 per를 PER 지표로 오인하지 않는다.
        self.assertEqual(scoring.detect_strategy("Superior quality names"), "balanced")

    def test_tie_prefers_value_then_oversold(self):
        # 배당(value) + 성장(momentum) 1:1 → value 우선
        self.assertEqual(scoring.detect_strategy("배당 성장주"), "value")
        # 가치(value) + 눌림목/반등(oversold) 1:2 → oversold
        self.assertEqual(scoring.detect_strategy("가치주 눌림목 반등"), "oversold")


# --- 3. 점수 계산 ------------------------------------------------------------

def stock(**overrides):
    base = {
        "ticker": "TEST", "price": 100.0, "ma20": 100.0, "ma60": 100.0, "rsi14": 50.0,
        "trend": "횡보/혼조", "trailing_pe": 20.0, "forward_pe": 18.0,
        "revenue_growth": 0.05, "operating_margins": 0.12, "debt_to_equity": 80.0,
        "macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0, "macd_cross": "none",
        "macd_state": "equal", "bb_upper": 105.0, "bb_lower": 95.0, "bb_pct_b": 0.5,
        "volume_ratio": 1.0, "drawdown_from_high_pct": -5.0, "range_position_pct": 50.0,
    }
    base.update(overrides)
    return base


class ScoreStockTests(unittest.TestCase):
    def test_score_is_bounded_and_deterministic(self):
        s = stock()
        first = scoring.score_stock(s, "balanced")
        second = scoring.score_stock(s, "balanced")
        self.assertEqual(first, second)
        for strategy in scoring.STRATEGIES:
            score, signals = scoring.score_stock(s, strategy)
            self.assertIsInstance(score, float)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 100.0)
            self.assertIsInstance(signals, list)

    def test_missing_data_is_neutral_not_penalised(self):
        score, signals = scoring.score_stock({"ticker": "EMPTY"}, "value")
        self.assertEqual(score, 50.0)
        self.assertEqual(signals, [])

    def test_value_strategy_prefers_cheap_profitable_stock(self):
        cheap, cheap_signals = scoring.score_stock(
            stock(trailing_pe=8.4, revenue_growth=0.24, operating_margins=0.31,
                  debt_to_equity=30.0), "value")
        pricey, _ = scoring.score_stock(
            stock(trailing_pe=48.0, revenue_growth=-0.05, operating_margins=0.04,
                  debt_to_equity=220.0), "value")
        self.assertGreater(cheap, pricey)
        self.assertIn("저PER(8.4)", cheap_signals)
        self.assertTrue(any(sig.startswith("매출성장") for sig in cheap_signals))

    def test_oversold_strategy_prefers_washed_out_stock(self):
        washed, signals = scoring.score_stock(
            stock(rsi14=22.4, bb_pct_b=-0.12, drawdown_from_high_pct=-32.0,
                  macd_cross="bullish", trend="하락 추세(역배열: 현재가<20일선<60일선)"),
            "oversold")
        hot, _ = scoring.score_stock(
            stock(rsi14=78.0, bb_pct_b=1.2, drawdown_from_high_pct=-0.5,
                  macd_cross="bearish", trend="상승 추세(정배열: 현재가>20일선>60일선)"),
            "oversold")
        self.assertGreater(washed, hot)
        self.assertTrue(any(sig.startswith("RSI22.4") for sig in signals))
        self.assertIn("볼린저하단이탈", signals)

    def test_momentum_strategy_prefers_breakout_stock(self):
        breakout, signals = scoring.score_stock(
            stock(price=120.0, ma20=115.0, ma60=105.0, rsi14=62.0, macd_cross="bullish",
                  volume_ratio=2.6, range_position_pct=96.0, revenue_growth=0.18,
                  trend="상승 추세(정배열: 현재가>20일선>60일선)"), "momentum")
        falling, _ = scoring.score_stock(
            stock(price=90.0, ma20=95.0, ma60=110.0, rsi14=33.0, macd_cross="bearish",
                  volume_ratio=0.5, range_position_pct=12.0,
                  trend="하락 추세(역배열: 현재가<20일선<60일선)"), "momentum")
        self.assertGreater(breakout, falling)
        self.assertIn("MACD골든크로스", signals)
        self.assertIn("정배열", signals)
        self.assertTrue(any(sig.startswith("거래량") for sig in signals))

    def test_same_data_ranks_differently_by_strategy(self):
        cheap_value = stock(ticker="CHEAP", trailing_pe=7.0, rsi14=45.0, volume_ratio=0.9)
        breakout = stock(ticker="BREAK", trailing_pe=45.0, rsi14=63.0, macd_cross="bullish",
                         volume_ratio=2.8, price=120.0, ma20=115.0, ma60=105.0,
                         range_position_pct=95.0)
        value_scores = {t: scoring.score_stock(s, "value")[0]
                        for t, s in (("CHEAP", cheap_value), ("BREAK", breakout))}
        momentum_scores = {t: scoring.score_stock(s, "momentum")[0]
                           for t, s in (("CHEAP", cheap_value), ("BREAK", breakout))}
        self.assertGreater(value_scores["CHEAP"], value_scores["BREAK"])
        self.assertGreater(momentum_scores["BREAK"], momentum_scores["CHEAP"])

    def test_signals_are_capped_and_people_readable(self):
        _, signals = scoring.score_stock(
            stock(trailing_pe=8.0, revenue_growth=0.3, operating_margins=0.35,
                  debt_to_equity=20.0, rsi14=30.0, bb_pct_b=-0.1,
                  drawdown_from_high_pct=-30.0, macd_cross="bullish", volume_ratio=2.5),
            "oversold")
        self.assertLessEqual(len(signals), scoring.MAX_SIGNALS)
        self.assertTrue(all(isinstance(sig, str) and sig for sig in signals))


# --- 4. 정렬 ----------------------------------------------------------------

class RankStocksTests(unittest.TestCase):
    def test_sorted_by_quant_score_descending(self):
        rows = [
            {"ticker": "A", "trailing_pe": 45.0, "revenue_growth": -0.1},
            {"ticker": "B", "trailing_pe": 8.0, "revenue_growth": 0.25,
             "operating_margins": 0.3, "debt_to_equity": 20.0},
            {"ticker": "C", "trailing_pe": 16.0, "revenue_growth": 0.1},
        ]
        ranked, strategy = scoring.rank_stocks(rows, criteria="저PER 가치주")
        self.assertEqual(strategy, "value")
        self.assertEqual([r["ticker"] for r in ranked], ["B", "C", "A"])
        scores = [r["quant_score"] for r in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for row in ranked:
            self.assertIn("signals", row)

    def test_ties_keep_input_order(self):
        rows = [{"ticker": t} for t in ("T00", "T01", "T02", "T03")]
        ranked, _ = scoring.rank_stocks(rows, criteria="아무거나")
        self.assertEqual([r["ticker"] for r in ranked], ["T00", "T01", "T02", "T03"])

    def test_input_rows_are_not_mutated(self):
        rows = [{"ticker": "A", "trailing_pe": 8.0}]
        scoring.rank_stocks(rows, criteria="저PER")
        self.assertNotIn("quant_score", rows[0])
        self.assertNotIn("signals", rows[0])

    def test_error_rows_go_last(self):
        rows = [{"ticker": "ERR", "error": "주가 데이터를 찾을 수 없습니다."},
                {"ticker": "OK", "trailing_pe": 8.0, "revenue_growth": 0.3}]
        ranked, _ = scoring.rank_stocks(rows, criteria="저PER 가치주")
        self.assertEqual(ranked[0]["ticker"], "OK")
        self.assertEqual(ranked[-1]["ticker"], "ERR")

    def test_empty_input(self):
        ranked, strategy = scoring.rank_stocks([], criteria="")
        self.assertEqual(ranked, [])
        self.assertEqual(strategy, "balanced")

    def test_each_strategy_orders_its_own_winner_first(self):
        """같은 후보군이라도 전략이 바뀌면 1등이 바뀐다(가치/과매도/모멘텀)."""
        rows = [
            # 싸고 탄탄한데 이미 많이 올라 추세는 약한 종목
            {"ticker": "CHEAP", "price": 100.0, "ma20": 90.0, "ma60": 110.0,
             "trailing_pe": 7.2, "revenue_growth": 0.24, "operating_margins": 0.3,
             "debt_to_equity": 20.0, "rsi14": 62.0, "bb_pct_b": 0.9,
             "volume_ratio": 0.8, "drawdown_from_high_pct": -2.0, "range_position_pct": 70.0},
            # 비싸지만 정배열·거래량 급증·신고가권 돌파
            {"ticker": "BREAK", "price": 120.0, "ma20": 115.0, "ma60": 100.0,
             "trailing_pe": 55.0, "revenue_growth": 0.05, "operating_margins": 0.1,
             "debt_to_equity": 90.0, "rsi14": 63.0, "bb_pct_b": 0.85,
             "macd_cross": "bullish", "volume_ratio": 2.8,
             "drawdown_from_high_pct": -1.0, "range_position_pct": 96.0},
            # 깊게 빠지고 과매도
            {"ticker": "WASHED", "price": 60.0, "ma20": 75.0, "ma60": 90.0,
             "trailing_pe": 30.0, "revenue_growth": -0.1, "operating_margins": 0.06,
             "debt_to_equity": 120.0, "rsi14": 22.0, "bb_pct_b": -0.18,
             "volume_ratio": 2.1, "drawdown_from_high_pct": -38.0, "range_position_pct": 4.0},
        ]
        for criteria, expected_first in (("저PER 가치주", "CHEAP"),
                                         ("단기 눌림목 반등", "WASHED"),
                                         ("모멘텀 돌파", "BREAK")):
            ranked, strategy = scoring.rank_stocks(rows, criteria=criteria)
            self.assertEqual(ranked[0]["ticker"], expected_first, (criteria, strategy))
            scores = [r["quant_score"] for r in ranked]
            self.assertEqual(scores, sorted(scores, reverse=True))


# --- 5. /api/screen 통합 -----------------------------------------------------

@unittest.skipIf(TestClient is None, "fastapi.testclient 사용에 httpx가 필요합니다")
class ScreenIntegrationTests(unittest.TestCase):
    ROWS = {
        # 가치 조건에서 1등이어야 하는 종목
        "CHEAP": stock(ticker="CHEAP", name="Cheap Co", price=50.0, trailing_pe=7.4,
                       revenue_growth=0.22, operating_margins=0.28, debt_to_equity=25.0),
        # 가치 조건에서 꼴찌
        "PRICEY": stock(ticker="PRICEY", name="Pricey Co", price=400.0, trailing_pe=62.0,
                        revenue_growth=-0.08, operating_margins=0.03, debt_to_equity=210.0),
        "MID": stock(ticker="MID", name="Mid Co", price=120.0, trailing_pe=18.0),
    }

    def setUp(self):
        stock_tools._METRICS_CACHE.clear()
        stock_tools._HISTORY_CACHE.clear()
        api._NEWS_CACHE.clear()
        self._patches = []
        self.patch_attr(api, "_get_recent_news", lambda ticker, limit=3: [f"{ticker} headline"])
        self.patch_attr(stock_tools, "get_history",
                        lambda ticker, period="6mo": flat_hist(np.linspace(100, 150, 130)))

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def patch_attr(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self._patches.append(patcher)
        return value

    def _rows(self, ticker):
        row = dict(self.ROWS[ticker])
        row["news"] = [f"{ticker} headline"]
        return row

    def test_screen_sorts_scores_and_keeps_existing_keys(self):
        self.patch_attr(stock_tools, "get_stock_metrics", self._rows)
        fake = FakeGenai({"market_note": "ok", "top_picks": [
            {"ticker": "CHEAP", "price": 50.0, "reason": "싸다", "risk": "변동성"},
            {"ticker": "GHOST", "price": 1.0, "reason": "없는 종목", "risk": "-"},
        ]})
        self.patch_attr(api.genai, "Client", lambda: fake)

        resp = TestClient(api.app).post(
            "/api/screen", json={"criteria": "저PER 가치주", "tickers": ["PRICEY", "CHEAP", "MID"]})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["strategy"], "value")
        self.assertEqual([r["ticker"] for r in body["all_stocks"]], ["CHEAP", "MID", "PRICEY"])
        for row in body["all_stocks"]:
            # 기존 필드는 그대로, 점수·시그널은 추가
            for key in ("ticker", "name", "price", "ma20", "ma60", "rsi14", "trend",
                        "trailing_pe", "revenue_growth", "news"):
                self.assertIn(key, row)
            self.assertGreaterEqual(row["quant_score"], 0.0)
            self.assertLessEqual(row["quant_score"], 100.0)
            self.assertIsInstance(row["signals"], list)
        # top_picks의 기존 키 유지 + 점수 부착, 후보에 없는 티커에는 점수를 지어내지 않는다
        picks = body["analysis"]["top_picks"]
        self.assertEqual([p["ticker"] for p in picks], ["CHEAP", "GHOST"])
        self.assertEqual(picks[0]["quant_score"], body["all_stocks"][0]["quant_score"])
        self.assertEqual(picks[0]["signals"], body["all_stocks"][0]["signals"])
        self.assertNotIn("quant_score", picks[1])
        for pick in picks:
            for key in ("ticker", "price", "reason", "risk", "chart", "news"):
                self.assertIn(key, pick)
        # Gemini 프롬프트에 정량 점수와 전략이 실제로 전달된다
        prompt = fake.models.calls[0]["contents"]
        self.assertIn("quant_score", prompt)
        self.assertIn("strategy: value", prompt)

    def test_oversold_criteria_uses_oversold_strategy(self):
        oversold_rows = {
            "WASHED": stock(ticker="WASHED", rsi14=24.0, bb_pct_b=-0.2,
                            drawdown_from_high_pct=-35.0),
            "HOT": stock(ticker="HOT", rsi14=80.0, bb_pct_b=1.3, drawdown_from_high_pct=-0.5),
        }
        self.patch_attr(stock_tools, "get_stock_metrics", oversold_rows.__getitem__)
        self.patch_attr(api.genai, "Client",
                        lambda: FakeGenai({"market_note": "ok", "top_picks": []}))

        body = TestClient(api.app).post(
            "/api/screen", json={"criteria": "단기 눌림목 반등", "tickers": ["HOT", "WASHED"]}).json()

        self.assertEqual(body["strategy"], "oversold")
        self.assertEqual([r["ticker"] for r in body["all_stocks"]], ["WASHED", "HOT"])
        self.assertGreater(body["all_stocks"][0]["quant_score"],
                           body["all_stocks"][1]["quant_score"])

    def test_response_keeps_analysis_and_all_stocks_contracts(self):
        self.patch_attr(stock_tools, "get_stock_metrics", self._rows)
        self.patch_attr(api.genai, "Client", lambda: FakeGenai(
            {"market_note": "요약", "top_picks": [{"ticker": "MID", "price": 120.0,
                                                  "reason": "근거", "risk": "위험"}]}))
        body = TestClient(api.app).post(
            "/api/screen", json={"criteria": "모멘텀 돌파", "tickers": ["MID"]}).json()

        self.assertEqual(sorted(body),
                         ["all_stocks", "analysis", "analysis_note", "analysis_source",
                          "mode", "strategy", "universe_scanned"])
        self.assertEqual(body["analysis_source"], "gemini")  # 정상 경로는 모델 해설
        self.assertEqual(body["analysis_note"], "")
        self.assertEqual(body["mode"], "watchlist")  # 기본값은 기존과 같은 관심 종목 모드
        self.assertEqual(body["analysis"]["market_note"], "요약")
        pick = body["analysis"]["top_picks"][0]
        self.assertEqual(len(pick["chart"]), 60)
        self.assertEqual(set(pick["chart"][0]), {"date", "price", "ma20", "ma60"})


if __name__ == "__main__":
    unittest.main()
