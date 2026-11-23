"""코스피 200 후보 재무 예열 테스트 — 사용자 요청 경로에서 네이버 재무 대기를 없앤다.

실행: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kospi200_scanner  # noqa: E402


class FundamentalPrewarmTests(unittest.TestCase):
    def test_warms_union_of_strategy_candidates_and_skips_cached(self):
        rows = [{"ticker": t} for t in ("A.KS", "B.KS", "C.KS")]

        with patch.object(kospi200_scanner.sp500_scanner, "rank_rows",
                          side_effect=lambda rows, strategy, top_n=15: [{"ticker": "A.KS"},
                                                                        {"ticker": "B.KS"}]), \
                patch.object(kospi200_scanner.naver_finance, "has_fundamentals",
                             side_effect=lambda t: t == "B.KS"), \
                patch.object(kospi200_scanner.naver_finance, "get_fundamentals",
                             return_value={}) as get_fundamentals:
            out = kospi200_scanner._warm_candidate_fundamentals(rows)

        self.assertEqual(out["targets"], 2)          # 전략이 달라도 같은 후보는 한 번만
        self.assertEqual(out["warmed"], 1)
        get_fundamentals.assert_called_once_with("A.KS")   # 캐시에 있는 B는 건너뛴다

    def test_strategy_failure_does_not_stop_the_rest(self):
        def rank(rows, strategy, top_n=15):
            if strategy == "value":
                raise RuntimeError("boom")
            return [{"ticker": "A.KS"}]

        with patch.object(kospi200_scanner.sp500_scanner, "rank_rows", side_effect=rank), \
                patch.object(kospi200_scanner.naver_finance, "has_fundamentals", return_value=False), \
                patch.object(kospi200_scanner.naver_finance, "get_fundamentals", return_value={}) as get_fundamentals:
            out = kospi200_scanner._warm_candidate_fundamentals([{"ticker": "A.KS"}])

        self.assertEqual(out["targets"], 1)
        get_fundamentals.assert_called_once_with("A.KS")

    def test_switch_off_starts_nothing(self):
        with patch.dict(os.environ, {"KOSPI_FUND_PREWARM": "0"}):
            self.assertFalse(kospi200_scanner._start_fundamental_prewarm([{"ticker": "A.KS"}]))

    def test_empty_rows_start_nothing(self):
        self.assertFalse(kospi200_scanner._start_fundamental_prewarm([]))

    def test_status_reports_state(self):
        status = kospi200_scanner.fundamental_prewarm_status()
        for key in ("enabled", "limit", "workers", "running", "warmed_last", "last_sec"):
            self.assertIn(key, status)


if __name__ == "__main__":
    unittest.main()


class Sp500MetricPrewarmTests(unittest.TestCase):
    """S&P 500도 같은 방식으로 후보 지표를 미리 받는다(한도·동시성은 더 보수적)."""

    def test_warms_union_and_skips_cached(self):
        rows = [{"ticker": t} for t in ("AAA", "BBB", "CCC")]
        with patch.object(kospi200_scanner.sp500_scanner, "rank_rows",
                          side_effect=lambda rows, strategy, top_n=15: [{"ticker": "AAA"},
                                                                        {"ticker": "BBB"}]), \
                patch.object(kospi200_scanner.sp500_scanner.stock_tools, "has_metrics",
                             side_effect=lambda t: t == "BBB"), \
                patch.object(kospi200_scanner.sp500_scanner.stock_tools, "get_stock_metrics",
                             return_value={}) as get_metrics:
            out = kospi200_scanner.sp500_scanner._warm_candidate_metrics(rows)

        self.assertEqual(out["targets"], 2)
        self.assertEqual(out["warmed"], 1)
        get_metrics.assert_called_once_with("AAA")

    def test_switch_off_starts_nothing(self):
        with patch.dict(os.environ, {"SP500_FUND_PREWARM": "0"}):
            self.assertFalse(kospi200_scanner.sp500_scanner._start_metric_prewarm([{"ticker": "AAA"}]))
