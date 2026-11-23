"""미국 주식 시간외(프리·애프터마켓) 시세·병합 정책 테스트.

다루는 것
1. `extended_quotes.parse_naver_quotes` — 여러 종목 응답에서 `symbolCode`로 되돌리고
   `overMarketPriceInfo.overPrice`(시간외)를 정규장가보다 우선
2. `extended_quotes.parse_chart_meta` — Yahoo 메타로 시간외 체결가 복원
   (previousClose × (1 + fulldayChangePercent)), 괴리가 큰 값은 버림
3. `extended_quotes.get_quotes` — 네이버 요청 1회 → 빈 종목만 Yahoo, 30초 캐시,
   실패 캐시, 소스별 연속 실패 휴식, 스위치
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


def naver_item(symbol="MNST", close=43.69, over=43.27, session="AFTER_MARKET", market="CLOSE"):
    item = {
        "symbolCode": symbol, "closePrice": str(close), "marketStatus": market,
        "localTradedAt": "2026-11-20T16:00:00-05:00",
    }
    if session is not None:
        item["overMarketPriceInfo"] = {
            "tradingSessionType": session, "overMarketStatus": "CLOSE",
            "overPrice": None if over is None else str(over),
            "localTradedAt": "2026-11-20T20:00:00-05:00",
        }
    return item


def naver_payload(*items):
    return {"pollingInterval": 70000, "datas": list(items)}


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


def naver_fetch_factory(mapping):
    """mapping: 심볼 → 그 종목의 항목 dict(없으면 응답에서 빠진다)."""
    def fetch(codes):
        symbols = {code[:-2] if code.endswith(".O") else code for code in codes}
        return naver_payload(*[mapping[s] for s in symbols if s in mapping])
    return fetch


class ParseNaverQuotesTests(unittest.TestCase):
    def test_many_symbols_are_keyed_by_symbol_code(self):
        payload = naver_payload(naver_item("AAPL", close=342.56, over=344.87),
                                naver_item("MLM", close=500.74, over=500.74))
        quotes = extended_quotes.parse_naver_quotes(payload)
        self.assertEqual(set(quotes), {"AAPL", "MLM"})
        self.assertEqual(quotes["AAPL"]["price"], 344.87)
        self.assertEqual(quotes["AAPL"]["source"], extended_quotes.SOURCE_NAVER_PREPOST)
        self.assertEqual(quotes["MLM"]["price"], 500.74)
        self.assertEqual(quotes["MLM"]["source"], extended_quotes.SOURCE_NAVER)

    def test_over_market_price_wins(self):
        quote = extended_quotes.parse_naver_quotes(naver_payload(naver_item()))["MNST"]
        self.assertEqual(quote["price"], 43.27)
        self.assertEqual(quote["session"], "extended")
        self.assertEqual(quote["regular_price"], 43.69)
        self.assertEqual(quote["session_type"], "AFTER_MARKET")

    def test_missing_over_market_info_uses_close(self):
        quote = extended_quotes.parse_naver_quotes(naver_payload(naver_item(session=None)))["MNST"]
        self.assertEqual(quote["session"], "regular")
        self.assertEqual(quote["price"], 43.69)

    def test_comma_price_and_broken_payloads(self):
        payload = naver_payload(naver_item(close="1,102.15", over="1,103.37"))
        self.assertEqual(extended_quotes.parse_naver_quotes(payload)["MNST"]["price"], 1103.37)
        self.assertEqual(extended_quotes.parse_naver_quotes(naver_payload()), {})
        self.assertEqual(extended_quotes.parse_naver_quotes(None), {})
        self.assertEqual(extended_quotes.parse_naver_quotes({"datas": ["bad"]}), {})


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

    def test_naver_batch_is_one_request_and_cached(self):
        calls = []

        def fetch_naver(codes):
            calls.append(list(codes))
            return naver_payload(naver_item("MNST"), naver_item("AAPL", close=342.56, over=344.87))

        out = extended_quotes.get_quotes(["MNST", "AAPL"], fetch_naver=fetch_naver)
        self.assertEqual(len(calls), 1)                  # 두 종목이 요청 1회
        self.assertEqual(sorted(calls[0]), ["AAPL", "AAPL.O", "MNST", "MNST.O"])  # 두 접미사 형태
        self.assertEqual(out["MNST"]["price"], 43.27)
        self.assertEqual(out["AAPL"]["price"], 344.87)

        calls.clear()
        extended_quotes.get_quotes(["MNST", "AAPL"], fetch_naver=fetch_naver)
        self.assertEqual(calls, [])                      # 30초 캐시

    def test_yahoo_is_used_when_naver_has_no_symbol(self):
        out = extended_quotes.get_quotes(
            ["ADR"], fetch_naver=naver_fetch_factory({}), fetch_yahoo=yahoo_fetch_factory())
        self.assertEqual(out["ADR"]["source"], extended_quotes.SOURCE_YAHOO_PREPOST)
        self.assertEqual(out["ADR"]["price"], 43.27)

    def test_failure_is_cached_and_broken_source_is_paused(self):
        calls = []

        def boom(codes):
            calls.append(codes)
            raise RuntimeError("timeout")

        no_yahoo = lambda s: {"chart": {"result": []}}
        self.assertIsNone(extended_quotes.get_quote("MNST", fetch_naver=boom, fetch_yahoo=no_yahoo))
        self.assertIsNone(extended_quotes.get_quote("NVDA", fetch_naver=boom, fetch_yahoo=no_yahoo))
        self.assertIsNone(extended_quotes.get_quote("AAPL", fetch_naver=boom, fetch_yahoo=no_yahoo))
        self.assertEqual(len(calls), 3)
        self.assertTrue(extended_quotes.status()["naver"]["blocked"])
        # 휴식 중에는 네이버를 부르지 않고 Yahoo로 간다
        quote = extended_quotes.get_quote(
            "MSFT", fetch_naver=boom,
            fetch_yahoo=yahoo_fetch_factory(regular=484.52, previous=498.3, pct=-1.769))
        self.assertEqual(quote["price"], 489.49)
        self.assertEqual(len(calls), 3)

    def test_switch_off_makes_no_calls(self):
        calls = []
        with patch.dict(os.environ, {"EXTENDED_QUOTES": "0"}):
            out = extended_quotes.get_quotes(
                ["MNST"], fetch_naver=lambda codes: calls.append(codes) or naver_payload(naver_item()))
        self.assertEqual(out, {})
        self.assertEqual(calls, [])

    def test_get_quotes_skips_missing_symbols(self):
        out = extended_quotes.get_quotes(
            ["MNST", "NOPE"], fetch_naver=naver_fetch_factory({"MNST": naver_item()}),
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

    def _run(self, regular_session, finnhub=None, extended=None, finnhub_enabled=True):
        finnhub = finnhub if finnhub is not None else {
            "MNST": {"price": 43.69, "source": "finnhub_quote"}}
        extended = extended if extended is not None else {
            "MNST": {"price": 43.27, "source": "naver_prepost",
                     "session": "extended", "regular_price": 43.69}}
        with patch.object(realtime_quotes.finnhub_client, "is_enabled", return_value=finnhub_enabled), \
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
        out = self._run(regular_session=True, finnhub={},
                        extended={"MNST": {"price": 43.27, "source": "naver_prepost", "session": "regular"}})
        self.assertEqual(out["MNST"]["price"], 43.27)

    def test_finnhub_failure_does_not_break_extended(self):
        with patch.object(realtime_quotes.finnhub_client, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.finnhub_client, "get_quotes",
                             side_effect=RuntimeError("quota")), \
                patch.object(realtime_quotes.extended_quotes, "is_enabled", return_value=True), \
                patch.object(realtime_quotes.extended_quotes, "get_quotes",
                             return_value={"MNST": {"price": 43.27, "source": "naver_prepost",
                                                    "session": "extended"}}), \
                patch.object(realtime_quotes.extended_quotes, "is_us_regular_session",
                             return_value=False):
            out = realtime_quotes.get_prices(["MNST"])
        self.assertEqual(out["MNST"]["price"], 43.27)


if __name__ == "__main__":
    unittest.main()
