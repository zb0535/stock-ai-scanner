"""미국 주식 시간외(프리·애프터마켓) 시세·병합 정책 테스트.

다루는 것
1. `extended_quotes.parse_naver_quote` — 네이버 `overMarketPriceInfo.overPrice`(시간외) 우선,
   같으면 정규장가, 종목 없음(빈 datas)은 None
2. `extended_quotes.parse_chart_meta` — Yahoo 메타로 시간외 체결가 복원
   (previousClose × (1 + fulldayChangePercent)), 괴리가 큰 값은 버림
3. `extended_quotes.get_quote/get_quotes` — 네이버 → Yahoo 순서, 30초 캐시, 실패 캐시,
   소스별 연속 실패 휴식, 스위치
4. `extended_quotes.is_us_regular_session` — 평일 09:30–16:00 ET만 True
5. `realtime_quotes` 병합 정책 — 정규장에는 Finnhub, 프리·애프터·주말에는 시간외 우선

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
import realtime_quotes  # noqa: E402


def naver_payload(close=43.69, over=43.27, session="AFTER_MARKET", market="CLOSE"):
    return {"datas": [{
        "itemCode": "MNST", "closePrice": str(close), "marketStatus": market,
        "localTradedAt": "2026-11-20T16:00:00-05:00",
        "overMarketPriceInfo": {
            "tradingSessionType": session, "overMarketStatus": "CLOSE",
            "overPrice": None if over is None else str(over),
            "localTradedAt": "2026-11-20T20:00:00-05:00",
        },
    }]}


def empty_naver_payload():
    return {"pollingInterval": 70000, "datas": []}


def chart_payload(regular=43.69, previous=43.94, pct=-1.516, has_prepost=True):
    meta = {"currency": "USD", "regularMarketPrice": regular,
            "chartPreviousClose": previous, "fulldayChangePercent": pct}
    if has_prepost is not None:
        meta["hasPrePostMarketData"] = has_prepost
    return {"chart": {"result": [{"meta": meta}], "error": None}}


def yahoo_fetch_factory(**kwargs):
    def fetch(sym):
        if sym.startswith("NOPE"):
            return {"chart": {"result": []}}
        return chart_payload(**kwargs)
    return fetch


def naver_fetch_factory(payload=None, missing=()):
    def fetch(code):
        if code in missing:
            return empty_naver_payload()
        return payload if payload is not None else naver_payload()
    return fetch


class ParseNaverQuoteTests(unittest.TestCase):
    def test_over_market_price_wins(self):
        quote = extended_quotes.parse_naver_quote(naver_payload())
        self.assertEqual(quote["price"], 43.27)
        self.assertEqual(quote["source"], extended_quotes.SOURCE_NAVER_PREPOST)
        self.assertEqual(quote["session"], "extended")
        self.assertEqual(quote["regular_price"], 43.69)
        self.assertEqual(quote["session_type"], "AFTER_MARKET")

    def test_same_price_stays_regular(self):
        quote = extended_quotes.parse_naver_quote(naver_payload(close=79.62, over=79.62))
        self.assertEqual(quote["price"], 79.62)
        self.assertEqual(quote["source"], extended_quotes.SOURCE_NAVER)
        self.assertEqual(quote["session"], "regular")

    def test_missing_over_market_info_uses_close(self):
        payload = naver_payload()
        payload["datas"][0].pop("overMarketPriceInfo")
        quote = extended_quotes.parse_naver_quote(payload)
        self.assertEqual(quote["session"], "regular")
        self.assertEqual(quote["price"], 43.69)

    def test_comma_price_and_empty_datas(self):
        payload = naver_payload(close="1,102.15", over="1,103.37")
        self.assertEqual(extended_quotes.parse_naver_quote(payload)["price"], 1103.37)
        self.assertIsNone(extended_quotes.parse_naver_quote(empty_naver_payload()))
        self.assertIsNone(extended_quotes.parse_naver_quote(None))


class ParseChartMetaTests(unittest.TestCase):
    def test_extended_close_is_rebuilt_from_previous_close_and_percent(self):
        quote = extended_quotes.parse_chart_meta(chart_payload())
        self.assertEqual(quote["price"], 43.27)          # 43.94 × (1-1.516%) = 43.2739
        self.assertEqual(quote["source"], extended_quotes.SOURCE_YAHOO_PREPOST)
        self.assertEqual(quote["regular_price"], 43.69)

    def test_unchanged_extended_price_stays_regular(self):
        quote = extended_quotes.parse_chart_meta(chart_payload(regular=79.62, previous=81.12, pct=-1.849))
        self.assertEqual(quote["price"], 79.62)
        self.assertEqual(quote["session"], "regular")

    def test_no_prepost_support_uses_regular_price(self):
        quote = extended_quotes.parse_chart_meta(chart_payload(has_prepost=False))
        self.assertEqual(quote["session"], "regular")
        self.assertEqual(quote["price"], 43.69)

    def test_absurd_percent_is_ignored(self):
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
        env = patch.dict(os.environ, {"EXTENDED_QUOTES": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(extended_quotes.clear_cache)

    def test_cached_for_thirty_seconds(self):
        calls = []

        def fetch_naver(code):
            calls.append(code)
            return naver_payload()

        first = extended_quotes.get_quote("MNST", fetch_naver=fetch_naver)
        second = extended_quotes.get_quote("MNST", fetch_naver=fetch_naver)
        self.assertEqual(len(calls), 1)              # 두 번째는 30초 캐시
        self.assertEqual(first, second)

    def test_nasdaq_suffix_then_plain_for_nyse(self):
        tried = []

        def fetch_naver(code):
            tried.append(code)
            return empty_naver_payload() if code.endswith(".O") else naver_payload(close=500.74, over=500.74)

        quote = extended_quotes.get_quote("MLM", fetch_naver=fetch_naver)
        self.assertEqual(tried, ["MLM.O", "MLM"])    # 나스닥 접미사 → 뉴욕(접미사 없음)
        self.assertEqual(quote["price"], 500.74)
        # 코드를 기억해 다음 조회는 한 번만
        tried.clear()
        extended_quotes.clear_cache()
        extended_quotes._remember_code("MLM", "MLM")
        extended_quotes.get_quote("MLM", fetch_naver=fetch_naver)
        self.assertEqual(tried, ["MLM"])

    def test_yahoo_is_used_when_naver_has_no_symbol(self):
        quote = extended_quotes.get_quote(
            "ADR", fetch_naver=lambda code: empty_naver_payload(), fetch_yahoo=yahoo_fetch_factory())
        self.assertEqual(quote["source"], extended_quotes.SOURCE_YAHOO_PREPOST)
        self.assertEqual(quote["price"], 43.27)

    def test_failure_is_cached_and_broken_source_is_paused(self):
        calls = []

        def boom(code):
            calls.append(code)
            raise RuntimeError("timeout")

        no_yahoo = lambda s: {"chart": {"result": []}}
        self.assertIsNone(extended_quotes.get_quote("MNST", fetch_naver=boom, fetch_yahoo=no_yahoo))
        self.assertIsNone(extended_quotes.get_quote("NVDA", fetch_naver=boom, fetch_yahoo=no_yahoo))
        self.assertIsNone(extended_quotes.get_quote("AAPL", fetch_naver=boom, fetch_yahoo=no_yahoo))
        self.assertEqual(len(calls), 3)
        self.assertTrue(extended_quotes.status()["naver"]["blocked"])
        # 휴식 중에는 네이버를 부르지 않고 Yahoo로 간다
        quote = extended_quotes.get_quote("MSFT", fetch_naver=boom,
                                          fetch_yahoo=yahoo_fetch_factory(regular=484.52, previous=498.3, pct=-1.769))
        self.assertEqual(quote["price"], 489.49)
        self.assertEqual(len(calls), 3)

    def test_switch_off_makes_no_calls(self):
        calls = []
        with patch.dict(os.environ, {"EXTENDED_QUOTES": "0"}):
            out = extended_quotes.get_quotes(
                ["MNST"], fetch_naver=lambda c: calls.append(c) or naver_payload())
        self.assertEqual(out, {})
        self.assertEqual(calls, [])

    def test_get_quotes_skips_missing_symbols(self):
        out = extended_quotes.get_quotes(
            ["MNST", "NOPE"], fetch_naver=lambda code: empty_naver_payload(),
            fetch_yahoo=yahoo_fetch_factory())
        self.assertEqual(list(out), ["MNST"])


class RegularSessionTests(unittest.TestCase):
    def test_weekday_regular_hours(self):
        # 2026-11-20(금) 15:00 ET = 20:00 UTC
        self.assertTrue(extended_quotes.is_us_regular_session(datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)))
        # 09:29 ET는 프리마켓
        self.assertFalse(extended_quotes.is_us_regular_session(datetime(2026, 11, 20, 14, 29, tzinfo=timezone.utc)))
        # 16:00 ET 마감 = 애프터마켓
        self.assertFalse(extended_quotes.is_us_regular_session(datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)))

    def test_weekend_is_not_regular(self):
        # 2026-11-22(일) 12:00 ET
        self.assertFalse(extended_quotes.is_us_regular_session(datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)))


class MergePolicyTests(unittest.TestCase):
    """정규장에는 Finnhub, 프리·애프터·주말에는 시간외 체결가가 값이 된다."""

    def setUp(self):
        realtime_quotes.clear_cache()
        self.addCleanup(extended_quotes.clear_cache)

    def _run(self, regular_session):
        finnhub = {"MNST": {"price": 43.69, "source": "finnhub_quote"}}
        extended = {"MNST": {"price": 43.27, "source": "naver_prepost",
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
        self.assertEqual(out["MNST"]["source"], "naver_prepost")

    def test_regular_session_prefers_finnhub(self):
        out = self._run(regular_session=True)
        self.assertEqual(out["MNST"]["price"], 43.69)
        self.assertEqual(out["MNST"]["source"], "finnhub_quote")

    def test_extended_fills_when_finnhub_missing(self):
        extended = {"MNST": {"price": 43.27, "source": "naver_prepost", "session": "regular"}}
        with patch.object(realtime_quotes.finnhub_client, "is_enabled", return_value=False), \
                patch.object(realtime_quotes.extended_quotes, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.extended_quotes, "get_quotes", return_value=extended), \
                patch.object(realtime_quotes.extended_quotes, "is_us_regular_session",
                             return_value=True):
            out = realtime_quotes.get_prices(["MNST"])
        self.assertEqual(out["MNST"]["price"], 43.27)

    def test_finnhub_failure_does_not_break_extended(self):
        extended = {"MNST": {"price": 43.27, "source": "naver_prepost", "session": "extended"}}
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
