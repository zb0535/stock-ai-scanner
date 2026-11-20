"""실시간 현재가 오버레이 — 한국은 네이버 금융, 미국은 Finnhub `/quote`.

왜 필요한가
-----------
지금까지 `price`는 야후 6개월 일봉의 마지막 종가(`close.iloc[-1]`)였다. 지표(MA20·MA60·
RSI·MACD·볼린저)와 재무 지표는 하루 단위로만 바뀌므로 30분 캐시로 충분하지만, **현재가**는
장중에 계속 움직인다. 그래서 이 모듈은 지표·재무는 건드리지 않고 `price`만 실시간으로 덮어쓴다.

속도·쿼터 설계
--------------
- S&P 500(500종목)·코스피 200(200종목) **프리 스캔 단계에는 이 모듈을 부르지 않는다**.
  호출부가 최종 스크리닝 15종목(`/api/screen`)과 성적표 종목(`/api/performance`)에만 부른다.
- 한국 종목은 네이버 polling이 쉼표 목록을 받으므로 15종목이 **요청 1회**로 끝난다.
- 미국 종목은 Finnhub를 최대 8개 병렬로 부르되, 10초 캐시와 분당 50회 상한으로 쿼터를 지킨다.
- 이 모듈도 12초 인메모리 캐시를 둬서 같은 요청 안(또는 같은 순간의 여러 요청)의 중복 조회를 막는다.
- 실패(타임아웃·429·0·파싱 불가)는 조용히 빼고 돌려준다 → 호출부는 일봉 종가를 유지한다.

`REALTIME_PRICE=0`이면 이 모듈은 아무 호출도 하지 않는다(운영 스위치).
"""
import logging
import os
import threading
import time

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

    if others and finnhub_client.is_enabled():
        try:
            fresh.update(finnhub_client.get_quotes(others, max_workers=max_workers))
        except Exception as exc:  # Finnhub 장애도 마찬가지
            logger.warning("Finnhub 실시간 시세 실패: %s: %s", type(exc).__name__, str(exc)[:160])

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


def source_of(quote) -> str:
    """시세 dict의 출처 문자열(없으면 daily_close)."""
    if isinstance(quote, dict) and quote.get("source"):
        return str(quote["source"])
    return SOURCE_DAILY


def clear_cache() -> None:
    """테스트·수동 초기화용. 네이버·Finnhub의 자체 캐시는 각자 clear_cache로 지운다."""
    with _LOCK:
        _CACHE.clear()
