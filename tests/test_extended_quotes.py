"""미국 주식 시간외(프리·애프터마켓) 시세·병합 정책 테스트.

다루는 것
1. `extended_quotes.parse_chart_meta` — 시간외 체결가 복원(previousClose × (1+fulldayChangePercent)),
   정규장과 같을 때는 정규장가, 말이 안 되는 괴리는 버림
2. `extended_quotes.get_quote/get_quotes` — 30초 캐시·실패 캐시·연속 실패 휴식·스위치
3. `extended_quotes.is_us_regular_session` — 평일 09:30–16:00 ET만 True(서머타임 포함)
4. `realtime_quotes` 병합 정책 — 정규장에는 Finnhub, 프리·애프터·주말에는 Yahoo 시간외 우선

실행: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extended_quotes  # noqa: E402
import finnhub_client  # noqa: E402
import realtime_quotes  # noqa: E402


def chart_payload(regular=43.69, previous=43.94, pct=-1.516, has_prepost=True, currency="USD"):
    """Yahoo v8 chart 응답 최소형(meta만 쓴다)."""
    meta = {
        "currency": currency,
        "symbol": "MNST",
        "regularMarketPrice": regular,
        "chartPreviousClose": previous,
        "fulldayChangePercent": pct,
    }
    if has_prepost is not None:
        meta["hasPrePostMarketData"] = has_prepost
    return {"chart": {"result": [{"meta": meta}], "error": None}}


class ParseChartMetaTests(unittest.TestCase):
    def test_extended_close_is_rebuilt_from_previous_close_and_percent(self):
        # 43.94 × (1 - 1.516/100) = 43.2739 → 43.27 (토스·네이버 애프터마켓 종가와 일치)
        quote = extended_quotes.parse_chart_meta(chart_payload())
        self.assertEqual(quote["price"], 43.27)
        self.assertEqual(quote["source"], extended_quotes.SOURCE_PREPOST)
        self.assertEqual(quote["session"], "extended")
        self.assertEqual(quote["regular_price"], 43.69)
        self.assertEqual(quote["currency"], "USD")

    def test_unchanged_extended_price_stays_regular(self):
        quote = extended_quotes.parse_chart_meta(chart_payload(regular=79.62, previous=81.12, pct=-1.849))
        # 81.12 × (1-0.01849) = 79.6197 → 79.62 == 정규장 종가
        self.assertEqual(quote["price"], 79.62)
        self.assertEqual(quote["source"], extended_quotes.SOURCE_YAHOO)
        self.assertEqual(quote["session"], "regular")

    def test_no_prepost_support_uses_regular_price(self):
        quote = extended_quotes.parse_chart_meta(chart_payload(has_prepost=False))
        self.assertEqual(quote["session"], "regular")
        self.assertEqual(quote["price"], 43.69)

    def test_absurd_percent_is_ignored(self):
        # 메타가 다른 날짜와 섞여 50% 넘게 벌어지는 값은 신뢰하지 않는다.
        quote = extended_quotes.parse_chart_meta(chart_payload(regular=100.0, previous=100.0, pct=80.0))
        self.assertEqual(quote["price"], 100.0)
        self.assertEqual(quote["session"], "regular")

    def test_missing_or_broken_payload_is_none(self):
        self.assertIsNone(extended_quotes.parse_chart_meta(None))
        self.assertIsNone(extended_quotes.parse_chart_meta({}))
        self.assertIsNone(extended_quotes.parse_chart_meta({"chart": {"result": []}}))
        self.assertIsNone(extended_quotes.parse_chart_meta(chart_payload(regular=0)))


class ExtendedQuoteCacheTests(unittest.TestCase):
    def setUp(self):
        extended_quotes.clear_cache()
        for key, value in (("EXTENDED_QUOTES", "1"),):
            self.addCleanup(patch.dict(os.environ, {key: value}).stop)
            patch.dict(os.environ, {key: value}).start()

    def test_cached_for_thirty_seconds(self):
        calls = []

        def fetch(sym):
            calls.append(sym)
            return chart_payload()

        first = extended_quotes.get_quote("MNST", fetch=fetch)
        second = extended_quotes.get_quote("MNST", fetch=fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)

    def test_failure_is_cached_and_reported(self):
        calls = []

        def boom(sym):
            calls.append(sym)
            raise RuntimeError("timeout")

        self.assertIsNone(extended_quotes.get_quote("MNST", fetch=boom))
        self.assertIsNone(extended_quotes.get_quote("MNST", fetch=boom))
        self.assertEqual(len(calls), 1)          # 90초 실패 캐시

    def test_consecutive_failures_pause_the_layer(self):
        def boom(sym):
            raise RuntimeError("429")

        for sym in ("A", "B", "C"):
            extended_quotes.get_quote(sym, fetch=boom)
        self.assertTrue(extended_quotes.status()["blocked"])
        calls = []

        def fetch(sym):
            calls.append(sym)
            return chart_payload()

        self.assertIsNone(extended_quotes.get_quote("D", fetch=fetch))
        self.assertEqual(calls, [])              # 휴식 중에는 호출하지 않는다

    def test_switch_off_makes_no_calls(self):
        calls = []
        with patch.dict(os.environ, {"EXTENDED_QUOTES": "0"}):
            self.assertEqual(extended_quotes.get_quotes(["MNST"], fetch=lambda s: calls.append(s) or chart_payload()), {})
        self.assertEqual(calls, [])

    def test_get_quotes_skips_missing_symbols(self):
        def fetch(sym):
            return chart_payload() if sym == "MNST" else {"chart": {"result": []}}

        out = extended_quotes.get_quotes(["MNST", "NOPE"], fetch=fetch)
        self.assertEqual(list(out), ["MNST"])


class RegularSessionTests(unittest.TestCase):
    def test_weekday_regular_hours(self):
        # 2026-11-20(금) 15:00 ET = 20:00 UTC
        self.assertTrue(extended_quotes.is_us_regular_session(datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)))
        # 09:30 ET 개장 직전(09:29 ET = 14:29 UTC)은 프리마켓
        self.assertFalse(extended_quotes.is_us_regular_session(datetime(2026, 11, 20, 14, 29, tzinfo=timezone.utc)))
        # 16:00 ET 마감 = 애프터마켓
        self.assertFalse(extended_quotes.is_us_regular_session(datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)))

    def test_weekend_is_not_regular(self):
        # 2026-11-22(일) 12:00 ET
        self.assertFalse(extended_quotes.is_us_regular_session(datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)))


class MergePolicyTests(unittest.TestCase):
    """정규장에는 Finnhub, 프리·애프터·주말에는 Yahoo 시간외가 값이 된다."""

    def setUp(self):
        realtime_quotes.clear_cache()
        self.addCleanup(extended_quotes.clear_cache)

    def _run(self, regular_session):
        finnhub = {"MNST": {"price": 43.69, "source": "finnhub_quote"}}
        extended = {"MNST": {"price": 43.27, "source": "yahoo_prepost",
                             "session": "extended", "regular_price": 43.69}}
        with patch.object(realtime_quotes.finnhub_client, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.finnhub_client, "get_quotes", return_value=finnhub), \
                patch.object(realtime_quotes.extended_quotes, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.extended_quotes, "get_quotes", return_value=extended), \
                patch.object(realtime_quotes.extended_quotes, "is_us_regular_session",
                             return_value=regular_session):
            return realtime_quotes.get_prices(["MNST"])

    def test_after_hours_prefers_extended_price(self):
        out = self._run(regular_session=False)
        self.assertEqual(out["MNST"]["price"], 43.27)
        self.assertEqual(out["MNST"]["source"], "yahoo_prepost")

    def test_regular_session_prefers_finnhub(self):
        out = self._run(regular_session=True)
        self.assertEqual(out["MNST"]["price"], 43.69)
        self.assertEqual(out["MNST"]["source"], "finnhub_quote")

    def test_extended_fills_when_finnhub_missing(self):
        extended = {"MNST": {"price": 43.27, "source": "yahoo_prepost", "session": "regular"}}
        with patch.object(realtime_quotes.finnhub_client, "is_enabled", return_value=False), \
                patch.object(realtime_quotes.extended_quotes, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.extended_quotes, "get_quotes", return_value=extended), \
                patch.object(realtime_quotes.extended_quotes, "is_us_regular_session",
                             return_value=True):
            out = realtime_quotes.get_prices(["MNST"])
        self.assertEqual(out["MNST"]["price"], 43.27)

    def test_finnhub_failure_does_not_break_extended(self):
        extended = {"MNST": {"price": 43.27, "source": "yahoo_prepost", "session": "extended"}}
        with patch.object(realtime_quotes.finnhub_client, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.finnhub_client, "get_quotes",
                             side_effect=RuntimeError("quota")), \
                patch.object(realtime_quotes.extended_quotes, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.extended_quotes, "get_quotes", return_value=extended), \
                patch.object(realtime_quotes.extended_quotes, "is_us_regular_session",
                             return_value=False):
            out = realtime_quotes.get_prices(["MNST"])
        self.assertEqual(out["MNST"]["price"], 43.27)


if __name__ == "__main__":
    unittest.main()
