"""코스피 200 자동 발굴 — 200종목 기술적 프리 스크리너 (S&P 500 파이프라인 재사용).

무엇을 하나
-----------
`/api/screen`에 `mode="kospi200"`이 오면 코스피 200 구성종목(약 200개)을 **6개월 일봉만으로**
빠르게 훑어 조건(전략)에 맞는 상위 15개를 추린다. 그 15개만 `api.py`의 기존 3단 재무
파이프라인(Yahoo → 네이버 금융) + 뉴스 + Gemini로 넘어간다.

S&P 500(`sp500_scanner`)과 같은 규칙을 쓴다
------------------------------------------
- **청크 배치 다운로드**: `sp500_scanner.scan_rows`를 그대로 호출한다(100종목씩, 청크 즉시 해제).
- **프리 스캔은 네트워크 추가 호출 0회**: `t.info`·재무·뉴스를 부르지 않고 일봉만 계산한다.
- **70분 스냅샷 캐시**(스레드 안전) + **50분 백그라운드 예열**: TTL보다 짧은 주기라 만료된
  캐시로 사용자를 맞지 않는다. 사용자는 보통 몇 초 만에 응답을 받는다.
- **끊겨도 죽지 않는다**: 구성종목은 네이버 라이브 → 직전 성공값 → 내장 201종목 폴백,
  개별 종목 데이터가 비면 그 종목만 건너뛴다.

메모리(512MB) 보호
------------------
S&P 500 스캔이 도는 중에는 시작하지 않는다(`sp500_scanner.is_scanning()`을 최대
`PREWARM_OVERLAP_WAIT`초 기다린다). 기동 직후 첫 예열도 60초 뒤로 밀어 S&P 500(8초 뒤 시작)와
겹치지 않게 한다.

환경변수
--------
- `KOSPI200_PREWARM`  이 시장 예열만 끈다(`0`/`false`/`no`/`off`). 기본 켜짐.
- `PREWARM_ENABLED`   백그라운드 예열 전체를 끄는 상위 스위치(기본 켜짐).
- `SP500_PREWARM`     기존 스위치. 꺼져 있으면(테스트·로컬 실행) 한국 예열도 함께 꺼진다.
"""
import logging
import os
import threading
import time

import naver_finance
import scoring
import sp500_scanner

logger = logging.getLogger("kospi200_scanner")

PRESCAN_TTL = 70 * 60            # 스냅샷 수명 70분 (예열 주기 50분보다 길게)
PREWARM_ENV = "KOSPI200_PREWARM"
PREWARM_MASTER_ENV = "PREWARM_ENABLED"
PREWARM_LEGACY_ENV = "SP500_PREWARM"   # 기존 스위치와 호환: 0이면 한국 예열도 끈다
PREWARM_FIRST_DELAY = 60         # 기동 후 첫 예열(초) — S&P 500(8초)과 겹치지 않게 늦게
PREWARM_INTERVAL = 50 * 60       # 재예열 주기(50분)
PREWARM_REFRESH_AGE = 45 * 60    # 스냅샷이 이보다 새로우면 예열은 건너뛴다
PREWARM_OVERLAP_WAIT = 180       # S&P 500 스캔이 끝나기를 기다리는 최대 시간(초)
TOP_N = 15                       # Gemini에 넘길 후보 수

_TECH_HINT = "코스피 200"

# --- 상태 -------------------------------------------------------------------
_SNAPSHOT_LOCK = threading.Lock()
_SCAN_LOCK = threading.Lock()
# {"ts": float, "rows": [지표 dict], "universe": int, "skipped": int, "source": str}
_SNAPSHOT: dict = {"ts": 0.0, "rows": [], "universe": 0, "skipped": 0, "source": ""}

_PREWARM_STATE_LOCK = threading.Lock()
_PREWARM: dict = {"thread": None, "stop_event": None,
                  "last_attempt": None, "last_ok": None, "last_error": ""}


# --- 유니버스 ---------------------------------------------------------------

def get_kospi200_tickers(force_refresh: bool = False) -> list[str]:
    """스캔할 코스피 200 티커(네이버 라이브 → 직전 값 → 내장 폴백)."""
    return naver_finance.get_kospi200_tickers(force_refresh=force_refresh)


def universe_source(force_refresh: bool = False) -> str:
    """구성종목 출처 — `naver`(라이브) / `fallback`(내장) / `cache`."""
    return naver_finance.get_kospi200_table(force_refresh=force_refresh)["source"]


def _scan_universe(tickers: list[str] | None = None) -> dict:
    """코스피 200 유니버스를 훑어 지표 스냅샷을 만든다(다운로드 로직은 S&P 500과 공용)."""
    symbols = list(tickers) if tickers is not None else get_kospi200_tickers()
    return sp500_scanner.scan_rows(symbols, label="코스피 200 프리 스캔")


# --- 스냅샷 캐시 -------------------------------------------------------------

def _fresh(snapshot: dict, now: float, ttl: float = PRESCAN_TTL) -> bool:
    return bool(snapshot.get("rows")) and (now - float(snapshot.get("ts") or 0)) < ttl


def prescan(force_refresh: bool = False, max_age: float | None = None) -> dict:
    """코스피 200 지표 스냅샷. 기본 70분 캐시, 동시 요청은 한 번만 스캔한다.

    반환: `{"rows", "universe", "skipped", "ts", "cached", "stale", "source"}`
    """
    ttl = PRESCAN_TTL if max_age is None else max_age
    now = time.time()
    with _SNAPSHOT_LOCK:
        snapshot = dict(_SNAPSHOT)
    if not force_refresh and _fresh(snapshot, now, ttl):
        return {**snapshot, "cached": True, "stale": False}

    with _SCAN_LOCK:
        with _SNAPSHOT_LOCK:
            snapshot = dict(_SNAPSHOT)
        now = time.time()
        if not force_refresh and _fresh(snapshot, now, ttl):
            return {**snapshot, "cached": True, "stale": False}

        try:
            source = universe_source()
        except Exception:  # 목록 조회가 죽어도 스캔은 내장 폴백으로 계속된다
            source = "fallback"
        fresh = _scan_universe()
        if fresh["rows"]:
            with _SNAPSHOT_LOCK:
                _SNAPSHOT.update(ts=time.time(), rows=fresh["rows"],
                                 universe=fresh["universe"], skipped=fresh["skipped"],
                                 source=source)
                stored = dict(_SNAPSHOT)
            return {**stored, "cached": False, "stale": False}

        if snapshot.get("rows"):  # 이번 스캔이 완전히 실패 → 직전 스냅샷 유지
            logger.warning("코스피 200 프리 스캔 실패 → 직전 스냅샷(%d종목) 사용",
                           len(snapshot["rows"]))
            return {**snapshot, "cached": True, "stale": True}
        return {"ts": now, "rows": [], "universe": fresh["universe"],
                "skipped": fresh["skipped"], "source": source,
                "cached": False, "stale": False}


def snapshot_age(now: float | None = None) -> float | None:
    """현재 스냅샷의 나이(초). 스냅샷이 없으면 None."""
    now = time.time() if now is None else now
    with _SNAPSHOT_LOCK:
        rows = _SNAPSHOT.get("rows") or []
        ts = float(_SNAPSHOT.get("ts") or 0.0)
    if not rows:
        return None
    return max(0.0, now - ts)


def cache_status() -> dict:
    """`GET /`용 `kospi200_cache` — 스캔 중에도 락은 dict 복사 순간만 잡는다.

    반환: `{"warm","fresh","tickers","age_sec","ttl_sec","universe_source",
            "prewarm_enabled","prewarm_running","prewarm_last_ok_sec"}`
    """
    now = time.time()
    with _SNAPSHOT_LOCK:
        rows = _SNAPSHOT.get("rows") or []
        ts = float(_SNAPSHOT.get("ts") or 0.0)
        source = _SNAPSHOT.get("source") or ""
    tickers = len(rows)
    age = max(0.0, now - ts) if tickers else None
    with _PREWARM_STATE_LOCK:
        thread = _PREWARM.get("thread")
        last_ok = _PREWARM.get("last_ok")
    last_ok_sec = None if last_ok is None else int(max(0.0, now - float(last_ok)))
    return {
        "warm": bool(tickers),
        "fresh": bool(tickers) and age is not None and age < PRESCAN_TTL,
        "tickers": tickers,
        "age_sec": None if age is None else int(age),
        "ttl_sec": int(PRESCAN_TTL),
        "universe_source": source,
        "prewarm_enabled": prewarm_enabled(),
        "prewarm_running": bool(thread is not None and thread.is_alive()),
        "prewarm_last_ok_sec": last_ok_sec,
    }


# --- 백그라운드 예열 ---------------------------------------------------------

def _flag_enabled(raw: str | None) -> bool:
    """미설정/빈 값은 기본 켜짐, `0`/`false`/`no`/`off`만 끔."""
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def prewarm_enabled() -> bool:
    """한국 예열이 켜져 있는지 — 세 스위치 모두 켜져야 한다(env는 호출 시점에 읽는다)."""
    return all(_flag_enabled(os.environ.get(name))
               for name in (PREWARM_ENV, PREWARM_MASTER_ENV, PREWARM_LEGACY_ENV))


def _wait_for_sp500(timeout: float | None = None) -> bool:
    """S&P 500 스캔이 끝나기를 기다린다. 시간 안에 안 끝나면 False(이번 예열은 건너뜀)."""
    wait = PREWARM_OVERLAP_WAIT if timeout is None else timeout
    deadline = time.time() + max(0.0, wait)
    while sp500_scanner.is_scanning():
        if time.time() >= deadline:
            return False
        time.sleep(2)
    return True


def prewarm_once(max_age: float | None = None, wait_for_sp500: bool = True) -> dict:
    """한 번 예열한다 = 스냅샷이 오래됐으면 새로 만든다.

    반환: `{"ok","scanned","tickers","stale","elapsed_sec","error"}`
    """
    limit = PREWARM_REFRESH_AGE if max_age is None else max_age
    started = time.time()
    with _PREWARM_STATE_LOCK:
        _PREWARM["last_attempt"] = started

    age = snapshot_age(started)
    if age is not None and age < limit:
        with _PREWARM_STATE_LOCK:
            _PREWARM["last_error"] = ""
        return {"ok": True, "scanned": False, "tickers": cache_status()["tickers"],
                "stale": False, "elapsed_sec": 0.0, "error": ""}

    if wait_for_sp500 and not _wait_for_sp500():
        logger.info("코스피 200 예열 보류: S&P 500 스캔이 아직 돌고 있다")
        with _PREWARM_STATE_LOCK:
            _PREWARM["last_error"] = "sp500_scanning"
        return {"ok": True, "scanned": False, "tickers": cache_status()["tickers"],
                "stale": False, "elapsed_sec": round(time.time() - started, 1),
                "error": "sp500_scanning"}

    try:
        result = prescan(max_age=limit)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        tickers = cache_status()["tickers"]
        logger.warning("코스피 200 예열 실패 → 직전 스냅샷 유지(%d종목): %s", tickers, error)
        with _PREWARM_STATE_LOCK:
            _PREWARM["last_error"] = error
        return {"ok": False, "scanned": False, "tickers": tickers,
                "stale": True, "elapsed_sec": round(time.time() - started, 1), "error": error}

    rows = result.get("rows") or []
    ok = bool(rows) and not result.get("stale")
    elapsed = round(time.time() - started, 1)
    with _PREWARM_STATE_LOCK:
        if ok:
            _PREWARM["last_ok"] = time.time()
            _PREWARM["last_error"] = ""
        else:
            _PREWARM["last_error"] = "empty_snapshot" if not rows else "stale_snapshot"
    if ok:
        logger.info("코스피 200 예열 완료: %d종목 캐시=%s %.1fs",
                    len(rows), result.get("cached"), elapsed)
    else:
        logger.warning("코스피 200 예열 실패 → 직전 스냅샷(%d종목) 유지", len(rows))
    return {"ok": ok, "scanned": not result.get("cached"), "tickers": len(rows),
            "stale": bool(result.get("stale")), "elapsed_sec": elapsed, "error": ""}


def _prewarm_loop(stop_event: threading.Event, first_delay: float, interval: float,
                  max_age: float | None = None) -> None:
    if stop_event.wait(first_delay):
        return
    while not stop_event.is_set():
        try:
            prewarm_once(max_age=max_age)
        except Exception as exc:  # 어떤 예외도 데몬 스레드를 죽이지 않는다
            logger.warning("코스피 200 예열 예외: %s: %s", type(exc).__name__, str(exc)[:160])
        if stop_event.wait(interval):
            return


def start_prewarm(first_delay: float | None = None, interval: float | None = None,
                  max_age: float | None = None, force: bool = False) -> bool:
    """백그라운드 예열 스레드를 띄운다(이미 살아 있으면 False)."""
    if not force and not prewarm_enabled():
        logger.info("코스피 200 캐시 예열 꺼짐(%s=%s)", PREWARM_ENV, os.environ.get(PREWARM_ENV))
        return False
    with _PREWARM_STATE_LOCK:
        current = _PREWARM.get("thread")
        if current is not None and current.is_alive():
            return False
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_prewarm_loop,
            name="kospi200-prewarm",
            daemon=True,
            args=(stop_event,
                  PREWARM_FIRST_DELAY if first_delay is None else first_delay,
                  PREWARM_INTERVAL if interval is None else interval,
                  PREWARM_REFRESH_AGE if max_age is None else max_age),
        )
        _PREWARM.update(thread=thread, stop_event=stop_event)
        thread.start()
    logger.info("코스피 200 캐시 예열 시작: 첫 예열 %.0fs 뒤, 이후 %.0f분마다",
                PREWARM_FIRST_DELAY if first_delay is None else first_delay,
                (PREWARM_INTERVAL if interval is None else interval) / 60.0)
    return True


def stop_prewarm(timeout: float = 1.0) -> bool:
    """예열 스레드에 정지를 알린다(앱 종료 훅·테스트 정리용)."""
    with _PREWARM_STATE_LOCK:
        stop_event = _PREWARM.get("stop_event")
        thread = _PREWARM.get("thread")
    if stop_event is None:
        return False
    stop_event.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout)
    with _PREWARM_STATE_LOCK:
        _PREWARM.update(thread=None, stop_event=None)
    return True


# --- 후보 선정 ---------------------------------------------------------------

def select_candidates(criteria: str | None = None, top_n: int = TOP_N,
                      force_refresh: bool = False) -> dict:
    """조건에 맞는 코스피 200 상위 후보를 고른다(S&P 500과 같은 반환 모양).

    반환: `{"tickers","candidates","strategy","universe_scanned","scanned","skipped",
           "cached","stale","universe_source"}`
    """
    strategy = scoring.detect_strategy(criteria)
    snapshot = prescan(force_refresh=force_refresh)
    ranking = sp500_scanner.rank_rows(snapshot["rows"], strategy, top_n=top_n)
    return {
        "tickers": [row["ticker"] for row in ranking],
        "candidates": ranking,
        "strategy": strategy,
        "universe_scanned": snapshot.get("universe", 0),
        "scanned": len(snapshot.get("rows") or []),
        "skipped": snapshot.get("skipped", 0),
        "cached": snapshot.get("cached", False),
        "stale": snapshot.get("stale", False),
        "universe_source": snapshot.get("source", ""),
    }


def reset_cache() -> None:
    """테스트·수동 초기화용. 스냅샷과 구성종목 목록 캐시를 모두 비운다."""
    with _SNAPSHOT_LOCK:
        _SNAPSHOT.update(ts=0.0, rows=[], universe=0, skipped=0, source="")
    naver_finance.reset_table_cache()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = select_candidates("단기 눌림목 반등")
    print(f"전략={result['strategy']} 유니버스={result['universe_scanned']} "
          f"산출={result['scanned']} 캐시={result['cached']} 출처={result['universe_source']}")
    for row in result["candidates"]:
        print(f"  {row['ticker']:12s} {row['prescore']:5.1f} {row['presignals']}")
