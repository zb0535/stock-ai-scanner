"""실시간 현재가 오버레이 — 한국은 네이버 금융, 미국은 Finnhub `/quote` + Yahoo 시간외.

왜 필요한가
-----------
지금까지 `price`는 야후 6개월 일봉의 마지막 종가(`close.iloc[-1]`)였다. 지표(MA20·MA60·
RSI·MACD·볼린저)와 재무 지표는 하루 단위로만 바뀌므로 30분 캐시로 충분하지만, **현재가**는
장중에 계속 움직인다. 그래서 이 모듈은 지표·재무는 건드리지 않고 `price`만 실시간으로 덮어쓴다.

미국 종목은 시간대에 따라 한 소스를 먼저 쓴다
- 정규장(평일 09:30–16:00 ET): Finnhub `/quote`(`finnhub_quote`)가 실시간이다.
- 프리·애프터마켓·주말: Finnhub는 직전 **정규장 종가**만 주므로 네이버 해외주식·Yahoo의
  시간외 마지막 체결가(`extended_quotes`)를 쓴다 — 토스증권처럼 한국 낮에도 시간외 가격을
  보여주기 위해서다.
- 먼저 부른 소스가 못 준 종목만 다른 소스로 보충하고, 둘 다 실패하면 호출부가 일봉 종가를
  유지한다(`daily_close`). 두 소스를 항상 동시에 부르면 15종목 배치 대기가 두 배가 되므로
  한 바퀴로 끝낸다.

속도·쿼터 설계
--------------
- S&P 500(500종목)·코스피 200(200종목) **프리 스캔 단계에는 이 모듈을 부르지 않는다**.
  호출부가 최종 스크리닝 15종목(`/api/screen`)과 성적표 종목(`/api/performance`)에만 부른다.
- 한국 종목은 네이버 polling이 쉼표 목록을 받으므로 15종목이 **요청 1회**로 끝난다.
- 미국 종목은 시간대에 맞는 한 소스로 15종목을 최대 8개씩 병렬 조회하고, 빈 종목만 다른
  소스로 보충한다.
- 이 모듈도 12초 인메모리 캐시를 둬서 같은 요청 안(또는 같은 순간의 여러 요청)의 중복 조회를 막는다.
- 실패(타임아웃·429·0·파싱 불가)는 조용히 빼고 돌려준다 → 호출부는 일봉 종가를 유지한다.

`REALTIME_PRICE=0`이면 이 모듈은 아무 호출도 하지 않는다(운영 스위치).
"""
import logging
import os
import threading
import time

import extended_quotes
import finnhub_client
import naver_finance

logger = logging.getLogger("realtime_quotes")

SOURCE_NAVER = "naver_realtime"
SOURCE_FINNHUB = "finnhub_quote"
SOURCE_DAILY = "daily_close"

REALTIME_ENABLED = os.environ.get("REALTIME_PRICE", "1") not in ("0", "false", "False")
REALTIME_TTL = 12          # 오버레이 캐시(초) — 10~15초 사이에서 고른 값
QUOTE_WORKERS = 8          # 미국 종목 병렬 조회 상한

# 티커 -> (저장 시각, 시세 dict 또는 None)
_CACHE: dict[str, tuple[float, dict | None]] = {}
_LOCK = threading.Lock()


def is_enabled() -> bool:
    """`REALTIME_PRICE=0`으로 끌 수 있다(매번 환경변수를 읽어 테스트·운영에서 바로 반영)."""
    return (os.environ.get("REALTIME_PRICE") or "1").strip() not in ("0", "false", "False")


def normalize(ticker) -> str:
    return str(ticker or "").strip().upper()


def get_prices(tickers, max_workers: int = QUOTE_WORKERS) -> dict[str, dict]:
    """티커 목록 → `{티커: {"price": float, "source": str, ...}}` (실패 종목은 키 없음).

    한국 티커(.KS/.KQ)는 네이버 한 번의 polling으로, 그 밖은 Finnhub 병렬 조회로 채운다.
    예외는 모두 흡수한다 — 실시간 시세 때문에 스크리닝이 500이 되면 안 된다.
    """
    if not is_enabled():
        return {}

    want: list[str] = []
    for raw in tickers or []:
        sym = normalize(raw)
        if sym and sym not in want:
            want.append(sym)
    if not want:
        return {}

    now = time.time()
    out: dict[str, dict] = {}
    missing: list[str] = []
    with _LOCK:
        for sym in want:
            item = _CACHE.get(sym)
            if item and now - item[0] < REALTIME_TTL:
                if item[1]:
                    out[sym] = dict(item[1])
            else:
                missing.append(sym)
    if not missing:
        return out

    fresh: dict[str, dict] = {}
    korean = [sym for sym in missing if naver_finance.is_korean_ticker(sym)]
    others = [sym for sym in missing if not naver_finance.is_korean_ticker(sym)]

    if korean:
        try:
            fresh.update(naver_finance.get_realtime_prices(korean))
        except Exception as exc:  # 네이버 장애가 스크리닝을 죽이지 않게
            logger.warning("네이버 실시간 시세 실패: %s: %s", type(exc).__name__, str(exc)[:160])

    if others and (finnhub_client.is_enabled() or extended_quotes.is_enabled()):
        fresh.update(_us_quotes(others, max_workers=max_workers))

    with _LOCK:
        for sym in missing:
            quote = fresh.get(sym)
            _CACHE[sym] = (now, dict(quote) if quote else None)

    out.update(fresh)
    return out


def get_price_map(tickers, max_workers: int = QUOTE_WORKERS) -> dict[str, float]:
    """티커 → 현재가(float)만 필요한 호출부용 간편형(실패 종목은 키 없음)."""
    return {sym: float(quote["price"]) for sym, quote in
            get_prices(tickers, max_workers=max_workers).items() if quote.get("price")}


def _us_quotes(symbols: list[str], max_workers: int = QUOTE_WORKERS) -> dict[str, dict]:
    """미국 종목 시세 — 시간대에 맞는 소스를 먼저 쓰고, 빈 종목만 다른 소스로 보충한다.

    - 정규장(평일 09:30–16:00 ET): Finnhub `/quote`(실시간 체결) → 빈 종목만 Yahoo 시간외
    - 그 밖(프리·애프터마켓·주말): 네이버 해외주식 시간외(Yahoo 폴백) → 빈 종목만 Finnhub

    두 소스를 항상 같이 부르면 15종목 배치에서 대기가 두 배로 늘었다(네이버 시간외 + Finnhub).
    그래서 **한 소스만 먼저** 부르고, 그 소스가 못 준 종목만 다른 소스로 보충한다. 이러면
    실시간 조회 구간이 한 바퀴로 끝나고, 각 소스의 예외는 그 소스만 비운다.
    """
    finnhub_ok = finnhub_client.is_enabled()
    extended_ok = extended_quotes.is_enabled()
    if not finnhub_ok and not extended_ok:
        return {}

    regular_session = extended_quotes.is_us_regular_session()
    if regular_session:
        order = [("finnhub", finnhub_ok), ("extended", extended_ok)]
    else:
        order = [("extended", extended_ok), ("finnhub", finnhub_ok)]

    out: dict[str, dict] = {}
    for name, available in order:
        if not available:
            continue
        pending = [sym for sym in symbols if sym not in out]
        if not pending:
            break
        try:
            if name == "finnhub":
                got = finnhub_client.get_quotes(pending, max_workers=max_workers)
            else:
                got = extended_quotes.get_quotes(pending, max_workers=max_workers)
        except Exception as exc:  # 한 소스 장애가 다른 소스·스크리닝을 막지 않는다
            logger.warning("%s 시세 실패: %s: %s", name, type(exc).__name__, str(exc)[:160])
            got = {}
        for sym, quote in (got or {}).items():
            if sym not in pending or not quote:
                continue
            # 정규장 중에는 어제 시간외 체결가를 끌어오지 않는다 — 네이버가 Finnhub 실패 종목을
            # 보충할 때는 정규장가(`session="regular"`)만 쓴다.
            if regular_session and name == "extended" and quote.get("session") == "extended":
                continue
            out[sym] = dict(quote)
    return out


def source_of(quote) -> str:
    """시세 dict의 출처 문자열(없으면 daily_close)."""
    if isinstance(quote, dict) and quote.get("source"):
        return str(quote["source"])
    return SOURCE_DAILY


def clear_cache() -> None:
    """테스트·수동 초기화용. 네이버·Finnhub의 자체 캐시는 각자 clear_cache로 지운다."""
    with _LOCK:
        _CACHE.clear()
