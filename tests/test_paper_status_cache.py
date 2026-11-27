"""AI 모의투자 상태 조회 속도 — 연결 1회·캐시·무효화·병렬 조회 테스트.

다루는 것
1. `paper_store.snapshot`이 두 계좌 데이터를 **DB 연결 1회**로 모으고, 개별 조회 함수와
   같은 모양·같은 값을 돌려준다(예전엔 `/api/paper/status` 한 번에 연결을 14번 열었다).
2. 두 번째 계좌 초기화(`init_store`)와 같은 결과(계좌·시드 스냅샷 생성).
3. `status` 캐시: TTL 안 재사용 · 만료 후 재계산 · 3분 안 만료는 옛 값 + 백그라운드 갱신.
4. 매매(`run`)·설정(`configure`) 직후 캐시 무효화 → 항상 최신 값.
5. 환율·시세 병렬 조회(순차 합보다 짧다)와 `timings`/`cache` 응답 필드.
6. `PAPER_STATUS_CACHE=0` 스위치, 주입한 `price_fn` 결과는 캐시에 남기지 않는다.

실행: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paper_auth  # noqa: E402
import paper_store  # noqa: E402
import paper_trading  # noqa: E402

SEED = 1_000_000.0


def admin_headers(db_path=None):
    """기본 관리자(user 1) 세션 토큰 헤더 — 보호된 API는 토큰 없는 요청을 401로 막는다."""
    user = paper_auth.ensure_admin(db_path)
    token, _ = paper_auth.sign_token(user, db_path)
    return {"X-Paper-User-Token": token}


class StatusCase(unittest.TestCase):
    """테스트마다 빈 SQLite 파일 + 캐시 초기화."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "paper.db")
        patcher = patch.dict(os.environ, {"HISTORY_DB_PATH": self.db_path, "PAPER_AUTO": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        paper_trading.clear_caches()

    def tearDown(self):
        paper_trading.clear_caches()

    def prices(self, mapping):
        def _fn(tickers):
            prices = {sym: mapping[sym] for sym in tickers if sym in mapping}
            return prices, {sym: "test" for sym in prices}
        return _fn

    def fx(self, rate=1300.0):
        return lambda: {"rate": rate, "source": "test"}


# ---------------------------------------------------------------------------
# 1. 한 번의 연결로 모으는 스냅샷
# ---------------------------------------------------------------------------


class SnapshotTests(StatusCase):
    def test_snapshot_uses_one_connection_and_matches_individual_reads(self):
        paper_store.add_position("AAPL", "US", "USD", 2.0, 100.0, 1300.0, 260_000.0, account_id=1)
        paper_store.add_position("005930.KS", "KR", "KRW", 3.0, 70_000.0, 1.0, 210_000.0,
                                 account_id=2)
        opened = sum(1 for _ in paper_store.ACCOUNTS)  # 계좌 두 개를 만든 뒤 측정한다

        opened_connections = []
        original = paper_store._connection

        def counting(*args, **kwargs):
            opened_connections.append(1)
            return original(*args, **kwargs)

        with patch.object(paper_store, "_connection", counting):
            snap = paper_store.snapshot(limit_trades=5, limit_equity=10, db_path=self.db_path)
        self.assertEqual(len(opened_connections), 1, "DB 연결은 한 번만 열어야 한다")
        self.assertEqual(list(snap["positions"]), list(paper_store.ACCOUNTS))
        self.assertEqual([row["ticker"] for row in snap["positions"][1]], ["AAPL"])
        self.assertEqual([row["ticker"] for row in snap["positions"][2]], ["005930.KS"])
        # 개별 조회 함수와 같은 값
        self.assertEqual(snap["accounts"][1], paper_store.get_account(1, self.db_path))
        self.assertEqual(snap["accounts"][2], paper_store.get_account(2, self.db_path))
        self.assertEqual(snap["positions"][1],
                         paper_store.list_positions(self.db_path, 1))
        self.assertEqual(snap["realized"][1], paper_store.realized_pnl(self.db_path, 1))
        self.assertEqual(snap["trades"][2], paper_store.recent_trades(5, self.db_path, 2))
        self.assertEqual(snap["equity"][2], paper_store.equity_history(10, self.db_path, 2))
        self.assertEqual(snap["storage_backend"], "sqlite")
        self.assertIn("queried_at", snap)
        self.assertEqual(opened, 2)

    def test_snapshot_creates_accounts_and_seed_equity_like_init_store(self):
        snap = paper_store.snapshot(db_path=self.db_path)
        for aid in paper_store.ACCOUNTS:
            self.assertEqual(snap["accounts"][aid]["initial_cash_krw"], SEED)
            self.assertEqual(len(snap["equity"][aid]), 1)      # 시드 스냅샷 한 행
            self.assertEqual(snap["equity"][aid][0]["total_value_krw"], SEED)
            self.assertEqual(snap["trades"][aid], [])
            self.assertEqual(snap["positions"][aid], [])
        # 두 번째 호출은 스냅샷을 더 만들지 않는다(멱등)
        again = paper_store.snapshot(db_path=self.db_path)
        self.assertEqual(len(again["equity"][1]), 1)
        self.assertEqual(len(again["equity"][2]), 1)

    def test_snapshot_keeps_account_values(self):
        paper_store.update_account({"cash_krw": 750_000.0, "strategy": "value"},
                                   self.db_path, 2)
        snap = paper_store.snapshot(db_path=self.db_path)
        self.assertEqual(snap["accounts"][2]["cash_krw"], 750_000.0)
        self.assertEqual(snap["accounts"][2]["strategy"], "value")
        self.assertEqual(snap["accounts"][1]["cash_krw"], SEED)


class SetupConnectionTests(StatusCase):
    """계좌 준비·초기화도 연결을 한 번만 연다(`configure`·`초기화` 클릭이 느리지 않게)."""

    def test_ensure_accounts_uses_one_connection(self):
        opened = []
        original = paper_store._connection

        def counting(*args, **kwargs):
            opened.append(1)
            return original(*args, **kwargs)

        with patch.object(paper_store, "_connection", counting):
            accounts = paper_store.ensure_accounts(self.db_path)
        self.assertEqual(len(opened), 1)
        self.assertEqual(accounts[1]["initial_cash_krw"], SEED)
        self.assertEqual(accounts[2]["market_scope"], "KR")

    def test_reset_uses_one_connection_and_clears_one_account(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0, account_id=1)
        paper_store.add_position("005930.KS", "KR", "KRW", 2.0, 70_000.0, 1.0, 140_000.0,
                                account_id=2)
        opened = []
        original = paper_store._connection

        def counting(*args, **kwargs):
            opened.append(1)
            return original(*args, **kwargs)

        with patch.object(paper_store, "_connection", counting):
            account = paper_store.reset(initial_cash_krw=2_000_000, db_path=self.db_path,
                                       account_id=1)
        self.assertEqual(len(opened), 1, "초기화는 연결 한 번으로 끝낸다")
        self.assertEqual(account["cash_krw"], 2_000_000)
        self.assertIsNone(account["last_run_us_date"])
        self.assertEqual(paper_store.list_positions(self.db_path, 1), [])
        self.assertEqual(paper_store.recent_trades(5, self.db_path, 1), [])
        equity = paper_store.equity_history(10, self.db_path, 1)
        self.assertEqual([row["total_value_krw"] for row in equity], [2_000_000.0])
        # 다른 계좌(한국)는 그대로
        self.assertEqual(len(paper_store.list_positions(self.db_path, 2)), 1)
        self.assertEqual(paper_store.get_account(2, self.db_path)["cash_krw"], SEED)

    def test_reset_keeps_settings(self):
        paper_store.update_account({"strategy": "momentum", "auto_enabled": 0},
                                   self.db_path, 2)
        account = paper_store.reset(db_path=self.db_path, account_id=2)
        self.assertEqual(account["strategy"], "momentum")
        self.assertFalse(account["auto_enabled"])


# ---------------------------------------------------------------------------
# 2. 캐시 — 재사용·만료·백그라운드 갱신
# ---------------------------------------------------------------------------


class CacheTests(StatusCase):
    def setUp(self):
        super().setUp()
        self.builds = []

        def fake_build(db_path=None, price_fn=None, fx_fn=None, limit_trades=40,
                       limit_equity=180):
            self.builds.append(time.time())
            return {
                "rows": {mkt: {"market": mkt, "positions_count": 0,
                               "initial_cash_krw": SEED, "cash_krw": SEED,
                               "stock_value_krw": 0.0, "total_value_krw": SEED,
                               "total_pnl_krw": 0.0, "total_return_pct": 0.0,
                               "account_id": paper_store.MARKET_ACCOUNTS[mkt],
                               "label": mkt, "realized_pnl_krw": 0.0,
                               "strategy": "balanced", "strategy_label": "균형",
                               "auto_enabled": True, "last_run_date": None}
                       for mkt in ("US", "KR")},
                "views": {aid: {"positions": [], "stock_value_krw": 0.0}
                          for aid in paper_store.ACCOUNTS},
                "accounts": {aid: paper_store._default_account(aid)
                             for aid in paper_store.ACCOUNTS},
                "positions": {aid: [] for aid in paper_store.ACCOUNTS},
                "trades": {aid: [] for aid in paper_store.ACCOUNTS},
                "equity": {aid: [] for aid in paper_store.ACCOUNTS},
                "fx": {"rate": 1300.0, "source": "test"},
                "fx_rate": 1300.0,
                "today": paper_store.kst_today_str(),
                "storage_backend": "sqlite",
                "as_of": paper_store._now(),
                "timings": {"db_ms": 1.0, "fx_ms": 1.0, "prices_ms": 0.0, "total_ms": 3.0},
            }

        self.fake_build = fake_build
        patcher = patch.object(paper_trading, "_build_snapshot", fake_build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_call_reuses_snapshot_within_ttl(self):
        first = paper_trading.status(db_path=self.db_path, use_cache=True)
        second = paper_trading.status(db_path=self.db_path, market="KR", use_cache=True)
        self.assertEqual(len(self.builds), 1, "TTL 안에서는 두 계좌 모두 같은 스냅샷을 쓴다")
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["market"], "KR")
        self.assertIn("ttl_sec", second["cache"])
        self.assertIn("total_ms", second["timings"])

    def test_expired_cache_rebuilds(self):
        paper_trading.status(db_path=self.db_path, use_cache=True)
        with patch.object(paper_trading, "STATUS_TTL", -1.0), \
             patch.object(paper_trading, "STATUS_STALE_SEC", -1.0):
            paper_trading.status(db_path=self.db_path, use_cache=True)
        self.assertEqual(len(self.builds), 2)

    def test_stale_snapshot_is_served_while_refreshing_in_background(self):
        paper_trading.status(db_path=self.db_path, use_cache=True)
        done = threading.Event()
        calls = {"n": 0}
        original = self.fake_build

        def slow_build(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                time.sleep(0.05)
            value = original(*args, **kwargs)
            done.set()
            return value

        with patch.object(paper_trading, "_build_snapshot", slow_build), \
             patch.object(paper_trading, "STATUS_TTL", -1.0), \
             patch.object(paper_trading, "STATUS_STALE_SEC", 600.0):
            started = time.perf_counter()
            body = paper_trading.status(db_path=self.db_path, use_cache=True)
            elapsed = time.perf_counter() - started
            self.assertTrue(body["cache"]["hit"], "만료돼도 옛 스냅샷을 먼저 준다")
            self.assertLess(elapsed, 0.04, "응답은 백그라운드 갱신을 기다리지 않는다")
            self.assertTrue(done.wait(2.0), "백그라운드 갱신이 돈다")
        self.assertEqual(calls["n"], 1, "백그라운드에서 한 번 새로 만든다")

    def test_use_cache_false_forces_rebuild(self):
        paper_trading.status(db_path=self.db_path, use_cache=True)
        paper_trading.status(db_path=self.db_path, use_cache=False)
        self.assertEqual(len(self.builds), 2)

    def test_cache_switch_off(self):
        with patch.dict(os.environ, {"PAPER_STATUS_CACHE": "0"}):
            paper_trading.status(db_path=self.db_path)
            paper_trading.status(db_path=self.db_path)
        self.assertEqual(len(self.builds), 2)

    def test_injected_price_fn_is_not_cached(self):
        paper_trading.status(db_path=self.db_path, price_fn=self.prices({}),
                            fx_fn=self.fx(), use_cache=True)
        self.assertIsNone(paper_trading._cached_snapshot(self.db_path))

    def test_two_tabs_at_once_build_only_once(self):
        started = threading.Barrier(3)
        results = []

        def slow_build(*args, **kwargs):
            time.sleep(0.1)
            return self.fake_build(*args, **kwargs)

        def call(market):
            started.wait(2.0)
            results.append(paper_trading.status(db_path=self.db_path, market=market,
                                                use_cache=True))

        threads = [threading.Thread(target=call, args=(mkt,)) for mkt in ("US", "KR")]
        with patch.object(paper_trading, "_build_snapshot", slow_build):
            for thread in threads:
                thread.start()
            started.wait(2.0)  # 두 요청을 동시에 출발시킨다
            for thread in threads:
                thread.join(5.0)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.builds), 1, "동시 요청도 한 번만 계산한다")
        self.assertEqual({row["market"] for row in results}, {"US", "KR"})
        self.assertTrue(any(row["cache"]["hit"] for row in results))

    def test_building_snapshot_does_not_overwrite_newer_state(self):
        """계산 중에 매매·설정이 끝났다면 그 옛 스냅샷을 캐시에 남기지 않는다."""
        def build_then_invalidate(*args, **kwargs):
            value = self.fake_build(*args, **kwargs)
            paper_trading.invalidate_status_cache(self.db_path)   # 계산 도중 초기화·매매 발생
            return value

        with patch.object(paper_trading, "_build_snapshot", build_then_invalidate):
            paper_trading.status(db_path=self.db_path, use_cache=True)
        self.assertIsNone(paper_trading._cached_snapshot(self.db_path))

    def test_invalidate_clears_snapshot(self):
        paper_trading.status(db_path=self.db_path, use_cache=True)
        self.assertIsNotNone(paper_trading._cached_snapshot(self.db_path))
        paper_trading.invalidate_status_cache(self.db_path)
        self.assertIsNone(paper_trading._cached_snapshot(self.db_path))
        paper_trading.status(db_path=self.db_path, use_cache=True)
        paper_trading.clear_caches()
        self.assertIsNone(paper_trading._cached_snapshot(self.db_path))


# ---------------------------------------------------------------------------
# 3. 매매·설정 직후엔 캐시를 즉시 버린다
# ---------------------------------------------------------------------------


class InvalidationTests(StatusCase):
    def test_configure_invalidates_status_cache(self):
        paper_trading.status(db_path=self.db_path)
        self.assertIsNotNone(paper_trading._cached_snapshot(self.db_path))
        paper_trading.configure(account="KR", initial_cash_krw=2_000_000, db_path=self.db_path)
        self.assertIsNone(paper_trading._cached_snapshot(self.db_path))
        body = paper_trading.status(db_path=self.db_path, market="KR")
        self.assertEqual(body["account"]["cash_krw"], 2_000_000)

    def test_reset_invalidates_status_cache(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0, account_id=1)
        with patch.object(paper_trading, "default_prices", self.prices({"AAPL": 110})), \
             patch.object(paper_trading, "get_usd_krw", self.fx()):
            paper_trading.status(db_path=self.db_path, use_cache=True)
            self.assertIsNotNone(paper_trading._cached_snapshot(self.db_path))
            paper_trading.configure(account="US", reset=True, initial_cash_krw=SEED,
                                    db_path=self.db_path)
        self.assertIsNone(paper_trading._cached_snapshot(self.db_path))
        body = paper_trading.status(db_path=self.db_path, use_cache=False)
        self.assertEqual(body["positions"], [])

    def test_run_invalidates_status_cache(self):
        paper_trading.status(db_path=self.db_path, use_cache=True)
        self.assertIsNotNone(paper_trading._cached_snapshot(self.db_path))

        def candidates(market, criteria, top_n=paper_trading.CANDIDATES_PER_MARKET):
            return []

        paper_trading.run(market="US", force=True, db_path=self.db_path,
                          ai_call=lambda prompt: json.dumps({"sells": [], "buys": []}),
                          candidate_fn=candidates, price_fn=self.prices({}), fx_fn=self.fx(),
                          push_fn=lambda *args: {"sent": 0, "failed": 0})
        self.assertIsNone(paper_trading._cached_snapshot(self.db_path))


# ---------------------------------------------------------------------------
# 4. 병렬 조회 — 환율과 시세를 같이 부른다
# ---------------------------------------------------------------------------


class ParallelFetchTests(StatusCase):
    def test_fx_and_prices_are_fetched_in_parallel(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0, account_id=1)

        def slow_fx():
            time.sleep(0.25)
            return {"rate": 1300.0, "source": "test"}

        def slow_prices(tickers):
            time.sleep(0.25)
            return {sym: 110.0 for sym in tickers}, {sym: "test" for sym in tickers}

        started = time.perf_counter()
        snapshot = paper_trading._build_snapshot(self.db_path, price_fn=slow_prices,
                                                 fx_fn=slow_fx)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.45, "환율·시세를 순서대로 부르면 0.5초가 넘는다")
        self.assertGreaterEqual(snapshot["timings"]["fx_ms"], 240)
        self.assertGreaterEqual(snapshot["timings"]["prices_ms"], 240)
        # 값은 그대로 반영된다
        position = snapshot["views"][1]["positions"][0]
        self.assertEqual(position["ticker"], "AAPL")
        self.assertEqual(position["current_price"], 110.0)
        self.assertEqual(snapshot["fx_rate"], 1300.0)

    def test_price_failure_falls_back_to_cost_and_keeps_response(self):
        paper_store.add_position("AAPL", "US", "USD", 1.0, 100.0, 1300.0, 130_000.0, account_id=1)

        def broken(_tickers):
            raise RuntimeError("quote down")

        snapshot = paper_trading._build_snapshot(self.db_path, price_fn=broken,
                                                 fx_fn=self.fx())
        position = snapshot["views"][1]["positions"][0]
        self.assertEqual(position["price_source"], "avg_price_fallback")
        self.assertEqual(position["current_price"], 100.0)
        # 현금은 시드 그대로(포지션 추가는 현금을 건드리지 않는다) + 원가 평가 13만원
        self.assertEqual(snapshot["rows"]["US"]["total_value_krw"], SEED + 130_000.0)

    def test_fx_failure_falls_back_to_fixed_rate(self):
        def broken():
            raise RuntimeError("fx down")

        with patch.object(paper_trading, "DEFAULT_USD_KRW", 1350.0):
            snapshot = paper_trading._build_snapshot(self.db_path, price_fn=self.prices({}),
                                                     fx_fn=broken)
        self.assertEqual(snapshot["fx_rate"], 1350.0)
        self.assertEqual(snapshot["fx"]["source"], "fixed_fallback")


# ---------------------------------------------------------------------------
# 5. HTTP — 두 계좌 탭을 오가도 한 벌만 계산한다
# ---------------------------------------------------------------------------


try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - httpx 미설치 환경
    TestClient = None


@unittest.skipIf(TestClient is None, "httpx 미설치")
class StatusEndpointTests(StatusCase):
    def setUp(self):
        super().setUp()
        import api
        self.api = api
        self.client = TestClient(api.app)
        self.client.headers.update(admin_headers(self.db_path))

    def test_tab_switch_reuses_snapshot(self):
        builds = []
        original = paper_trading._build_snapshot

        def counting(*args, **kwargs):
            builds.append(1)
            return original(*args, **kwargs)

        with patch.object(paper_trading, "_build_snapshot", counting), \
             patch.object(paper_trading, "get_usd_krw", self.fx()):
            us = self.client.get("/api/paper/status?market=US").json()
            kr = self.client.get("/api/paper/status?market=KR").json()
        self.assertEqual(len(builds), 1)
        self.assertFalse(us["cache"]["hit"])
        self.assertTrue(kr["cache"]["hit"])
        self.assertEqual(us["comparison"], kr["comparison"])
        self.assertEqual(kr["market"], "KR")

    def test_status_after_configure_shows_new_value(self):
        with patch.object(paper_trading, "get_usd_krw", self.fx()):
            self.client.get("/api/paper/status?market=KR")
            response = self.client.post("/api/paper/configure",
                                        json={"market": "KR", "initial_cash_krw": 3_000_000})
            self.assertEqual(response.status_code, 200)
            body = self.client.get("/api/paper/status?market=KR").json()
        self.assertEqual(body["account"]["cash_krw"], 3_000_000)
        self.assertEqual(response.json()["status"]["account"]["cash_krw"], 3_000_000)

# ---------------------------------------------------------------------------
# 6. 기동 예열 — 사용자가 앱을 열 때 캐시가 이미 차 있게
# ---------------------------------------------------------------------------


class PrewarmTests(StatusCase):
    def setUp(self):
        super().setUp()
        self.patches = [
            patch.object(paper_trading, "default_prices", self.prices({})),
            patch.object(paper_trading, "get_usd_krw", self.fx()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_prewarm_fills_cache_before_first_request(self):
        paper_trading.warm_status_cache(delay=0)
        deadline = time.time() + 2.0
        while time.time() < deadline and paper_trading._cached_snapshot(None) is None:
            time.sleep(0.02)
        self.assertIsNotNone(paper_trading._cached_snapshot(None), "예열이 스냅샷을 만든다")
        body = paper_trading.status(market="KR")
        self.assertTrue(body["cache"]["hit"], "예열된 스냅샷을 바로 쓴다")

    def test_prewarm_switch_off(self):
        with patch.dict(os.environ, {"PAPER_STATUS_PREWARM": "0"}):
            paper_trading.warm_status_cache(delay=0)
            self.assertFalse(paper_trading.start_status_warm(delay=0, interval=5))
            time.sleep(0.2)
        self.assertIsNone(paper_trading._cached_snapshot(None))

    def test_periodic_warm_builds_at_start(self):
        builds = []
        original = paper_trading._build_snapshot

        def counting(*args, **kwargs):
            builds.append(1)
            return original(*args, **kwargs)

        with patch.object(paper_trading, "_build_snapshot", counting):
            self.assertTrue(paper_trading.start_status_warm(delay=0, interval=5))
            deadline = time.time() + 2.0
            while time.time() < deadline and paper_trading._cached_snapshot(None) is None:
                time.sleep(0.02)
        self.assertTrue(builds, "예열 루프가 스냅샷을 만든다")
        self.assertIsNotNone(paper_trading._cached_snapshot(None))

    def test_warm_once_rebuilds_expired_snapshot(self):
        builds = []
        original = paper_trading._build_snapshot

        def counting(*args, **kwargs):
            builds.append(1)
            return original(*args, **kwargs)

        with patch.object(paper_trading, "_build_snapshot", counting):
            paper_trading.status(use_cache=True)          # 1번째
            with patch.object(paper_trading, "STATUS_TTL", -1.0):
                self.assertTrue(paper_trading._warm_once())   # 만료 → 2번째
        self.assertEqual(len(builds), 2)

    def test_warm_skips_when_snapshot_is_fresh(self):
        builds = []
        original = paper_trading._build_snapshot

        def counting(*args, **kwargs):
            builds.append(1)
            return original(*args, **kwargs)

        with patch.object(paper_trading, "_build_snapshot", counting):
            paper_trading.status(use_cache=True)
            paper_trading._warm_once()
        self.assertEqual(len(builds), 1, "방금 만든 스냅샷은 다시 만들지 않는다")


if __name__ == "__main__":
    unittest.main()
