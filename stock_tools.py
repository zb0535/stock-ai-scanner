import logging
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("stock_tools")

DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "AVGO", "AMD", "TSM",
    "NFLX", "JPM", "LLY", "COST", "PLTR",
]

# --- 캐시 TTL ----------------------------------------------------------------
# Yahoo Finance는 짧은 시간에 crumb을 반복 요청하면 429(rate limited)나
# 401(Invalid Crumb)로 응답을 막는다. 같은 종목을 30분 동안 다시 조회하지 않는
# 것만으로 호출량이 크게 줄어 제한에 걸릴 확률이 낮아진다.
METRICS_TTL = 1800      # 30분: 차트 지표 + 재무 지표 통합 결과
HISTORY_TTL = 1800      # 30분: 6개월 일봉(이평선·RSI·차트 계산용)
STALE_TTL = 300         # 5분: 재무 지표를 직전 성공값으로 보전 중일 때 재시도 간격
PARTIAL_TTL = 60        # 60초: 재무 지표가 비어 있는 결과 (제한이 풀리면 바로 재조회)

# 여러 종목을 동시에 조회할 때의 최대 병렬도. Yahoo가 짧은 시간에 동시 요청을 많이
# 받으면 429(rate limited)로 막으므로 5~6개로 묶어 둔다.
MAX_FETCH_WORKERS = 6

# ticker -> (저장 시각, 결과 dict)
_METRICS_CACHE: dict[str, tuple[float, dict]] = {}
# "ticker:period" -> (저장 시각, 일봉 DataFrame)
_HISTORY_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()

# t.info에서 뽑아 쓰는 핵심 재무 지표 (응답 키 -> yfinance info 키)
_FIN_FIELDS = {
    "trailing_pe": "trailingPE",
    "forward_pe": "forwardPE",
    "revenue_growth": "revenueGrowth",
    "operating_margins": "operatingMargins",
    "debt_to_equity": "debtToEquity",
}

# t.info가 연속으로 실패하면 잠시 호출을 멈춘다(429 폭풍 방지).
_INFO_FAIL_THRESHOLD = 5
_INFO_COOLDOWN_SECONDS = 300
_info_fail_streak = 0
_info_cooldown_until = 0.0


def get_watchlist() -> list[str]:
    """스크리닝 대상 미국 주식 티커 목록을 반환합니다."""
    return list(DEFAULT_WATCHLIST)


def bounded_map(fn, items, max_workers: int = MAX_FETCH_WORKERS) -> list:
    """여러 종목 조회를 제한된 병렬도로 실행하고 입력 순서를 그대로 유지합니다.

    Yahoo 429 폭풍을 피하려고 동시 호출 수를 max_workers(기본 6)로 묶는다.
    ThreadPoolExecutor.map은 결과를 입력 순서대로 돌려주므로 종목 순서가 보존된다.
    fn에서 예외가 나오면 순차 실행 때와 똑같이 그 예외가 그대로 전파된다.
    """
    items = list(items)
    if not items:
        return []
    workers = max(1, min(int(max_workers or 1), len(items)))
    if workers == 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
        return list(pool.map(fn, items))


# --- 내부 도우미 -------------------------------------------------------------

def _to_number(value):
    """NaN/inf/numpy 스칼라 같은 JSON 비호환 값을 안전한 float 또는 None으로 바꿉니다."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _calc_rsi(close_series, period: int = 14) -> float | None:
    if len(close_series) < period + 1:
        return None
    delta = close_series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def _note_info_failure() -> None:
    global _info_fail_streak, _info_cooldown_until
    with _CACHE_LOCK:
        _info_fail_streak += 1
        if _info_fail_streak >= _INFO_FAIL_THRESHOLD:
            _info_cooldown_until = time.time() + _INFO_COOLDOWN_SECONDS
            _info_fail_streak = 0
            logger.warning(
                "t.info 연속 실패 %d회 → %d초 동안 재호출을 건너뜁니다.",
                _INFO_FAIL_THRESHOLD, _INFO_COOLDOWN_SECONDS,
            )


def _note_info_success() -> None:
    global _info_fail_streak
    with _CACHE_LOCK:
        _info_fail_streak = 0


def _info_in_cooldown() -> bool:
    with _CACHE_LOCK:
        return time.time() < _info_cooldown_until


def get_history(ticker: str, period: str = "6mo"):
    """일봉 데이터를 캐시에서 반환합니다(HISTORY_TTL 30분).

    조회에 실패하면 만료된 직전 성공값이라도 돌려주고, 그것도 없으면 빈 DataFrame을
    돌려줍니다. 반환된 DataFrame은 읽기 전용으로만 쓰세요.

    캐시 읽기·쓰기는 _CACHE_LOCK으로 보호하므로 bounded_map으로 동시에 불러도 안전하다.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    key = f"{sym}:{period}"
    now = time.time()
    with _CACHE_LOCK:
        cached = _HISTORY_CACHE.get(key)
    if cached and now - cached[0] < HISTORY_TTL:
        return cached[1]

    try:
        hist = yf.Ticker(sym).history(period=period)
    except Exception as exc:  # 429/401/네트워크 오류 모두 여기서 흡수
        logger.warning("%s: history(%s) 실패 → %s: %s", sym, period, type(exc).__name__, str(exc)[:200])
        hist = None

    if hist is not None and not getattr(hist, "empty", True):
        with _CACHE_LOCK:
            _HISTORY_CACHE[key] = (now, hist)
        return hist
    if cached:  # 만료됐지만 이번 조회도 실패 → 직전 성공값 사용
        return cached[1]
    return hist


def _fetch_info(sym: str):
    """t.info를 조회합니다. 반환: (info 또는 None, 실패 사유 문자열 또는 None)."""
    if _info_in_cooldown():
        return None, "t.info 쿨다운(연속 실패) 중"
    try:
        info = yf.Ticker(sym).info
    except Exception as exc:
        _note_info_failure()
        reason = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("%s: t.info 실패 → %s", sym, reason)
        return None, reason
    if not isinstance(info, dict) or not info:
        _note_info_failure()
        logger.warning("%s: t.info 빈 응답(재무 지표 미수신)", sym)
        return None, "t.info 빈 응답"
    _note_info_success()
    return info, None


def _metrics_ttl(payload: dict) -> float:
    """결과 성격에 따라 캐시 유지 시간을 정합니다."""
    if payload.get("financials_available") and not payload.get("financials_stale"):
        return METRICS_TTL
    if payload.get("financials_available"):
        return STALE_TTL
    return PARTIAL_TTL


def _build_metrics(sym: str, cached_payload: dict | None) -> dict:
    hist = get_history(sym, "6mo")
    if hist is None or getattr(hist, "empty", True):
        # 차트 데이터도 못 받았고, 직전 성공값이 있으면 그대로 내보낸다.
        if cached_payload and cached_payload.get("price") is not None:
            return dict(cached_payload)
        return {"ticker": sym, "error": "주가 데이터를 찾을 수 없습니다."}

    close = hist["Close"]
    price = round(float(close.iloc[-1]), 2)
    ma20 = round(float(close.rolling(20).mean().iloc[-1]), 2) if len(close) >= 20 else None
    ma60 = round(float(close.rolling(60).mean().iloc[-1]), 2) if len(close) >= 60 else None
    rsi14 = _calc_rsi(close, 14)

    trend = "판단불가"
    if ma20 and ma60:
        if price > ma20 > ma60:
            trend = "상승 추세(정배열: 현재가>20일선>60일선)"
        elif price < ma20 < ma60:
            trend = "하락 추세(역배열: 현재가<20일선<60일선)"
        else:
            trend = "횡보/혼조"

    # --- 재무 지표: 실패해도 차트 지표는 그대로 살린다 ---
    info, info_error = _fetch_info(sym)
    fresh = {key: _to_number((info or {}).get(src)) for key, src in _FIN_FIELDS.items()}
    fresh_ok = any(value is not None for value in fresh.values())
    cached_payload = cached_payload or {}
    previous = {key: _to_number(cached_payload.get(key)) for key in _FIN_FIELDS}
    previous_ok = any(value is not None for value in previous.values())

    if fresh_ok:
        # 이번 응답에서 빠진 필드는 직전 성공값으로 채운다(필드 단위 보전).
        financials = {key: fresh[key] if fresh[key] is not None else previous[key] for key in _FIN_FIELDS}
        financials_available, financials_stale = True, False
    elif previous_ok:
        # stale-while-error: t.info가 401/429로 죽어도 직전 성공값 유지
        financials = dict(previous)
        financials_available, financials_stale = True, True
    else:
        financials = {key: None for key in _FIN_FIELDS}
        financials_available, financials_stale = False, False

    name = None
    if info:
        name = info.get("shortName") or info.get("longName")
    name = name or cached_payload.get("name") or sym

    payload = {
        "ticker": sym,
        "name": name,
        "price": price,
        "ma20": ma20,
        "ma60": ma60,
        "rsi14": rsi14,
        "trend": trend,
    }
    payload.update(financials)
    payload["financials_available"] = financials_available
    if financials_stale:
        payload["financials_stale"] = True
        payload["financials_note"] = "재무 지표 일시 미수신(Yahoo API 제한) — 직전 조회값 유지, 가격·이평선·RSI는 최신"
    elif not financials_available:
        payload["financials_note"] = "재무 지표 일시 미수신(Yahoo API 제한) — 가격·이평선·RSI만 유효"
    if info_error and not financials_available:
        payload["financials_error"] = info_error
    return payload


def get_stock_metrics(ticker: str) -> dict:
    """특정 종목의 최근 차트 지표(이동평균·RSI)와 핵심 재무제표 지표를 반환합니다.

    30분 캐시를 먼저 확인하고, t.info가 429/401 등으로 실패하더라도
    가격·이평선·RSI는 정상 반환하며 재무 지표만 직전 성공값 또는 null로 둡니다.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return {"ticker": "", "error": "주가 데이터를 찾을 수 없습니다."}

    now = time.time()
    with _CACHE_LOCK:
        cached = _METRICS_CACHE.get(sym)
    if cached and now - cached[0] < _metrics_ttl(cached[1]):
        return dict(cached[1])

    cached_payload = cached[1] if cached else None
    try:
        payload = _build_metrics(sym, cached_payload)
    except Exception as exc:  # 어떤 이유로든 API가 500으로 죽지 않게 마지막 방어
        logger.exception("%s: 지표 계산 실패 → %s", sym, exc)
        if cached_payload:
            return dict(cached_payload)
        return {"ticker": sym, "error": "주가 데이터를 찾을 수 없습니다."}

    with _CACHE_LOCK:
        _METRICS_CACHE[sym] = (now, payload)
    return dict(payload)


if __name__ == "__main__":
    for sym in ["NVDA", "AAPL"]:
        print(get_stock_metrics(sym))
