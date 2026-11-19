import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yfinance as yf

import finnhub_client
import sec_edgar

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

# SEC EDGAR가 대신 채울 수 있는 항목(선행 PER은 공시 데이터로 계산할 수 없다).
_SEC_FIELDS = ("trailing_pe", "revenue_growth", "operating_margins", "debt_to_equity")

# Finnhub(3순위)를 호출할지 판단하는 핵심 항목. 하나라도 Yahoo·SEC 모두에서 비면 호출한다.
# (SEC가 이미 다 채운 미국 종목은 Finnhub 무료 쿼터를 쓰지 않는다.)
_FINNHUB_TRIGGER_FIELDS = ("trailing_pe", "revenue_growth", "operating_margins", "debt_to_equity")

# t.info가 막힌 서버(Render 등)에서도 재무 지표를 유지하기 위한 SEC EDGAR 폴백 스위치.
# SEC_EDGAR_FALLBACK=0 으로 끌 수 있다.
SEC_FALLBACK_ENABLED = os.environ.get("SEC_EDGAR_FALLBACK", "1") not in ("0", "false", "False")

# 3순위 Finnhub 폴백 스위치. FINNHUB_FALLBACK=0 또는 FINNHUB_API_KEY 미설정 시 꺼진다.
FINNHUB_FALLBACK_ENABLED = os.environ.get("FINNHUB_FALLBACK", "1") not in ("0", "false", "False")

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


# --- 고급 기술적 지표 -------------------------------------------------------
# 모두 이미 받아 둔 6개월 일봉(get_history)만으로 계산한다 → 추가 네트워크 호출 0회.
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_CROSS_LOOKBACK = 3   # 크로스 판정은 최근 3봉 이내
BB_PERIOD = 20
BB_STD = 2.0
VOLUME_WINDOW = 20

# get_stock_metrics 응답에 추가되는 지표 키(기존 키는 그대로 둔다).
INDICATOR_FIELDS = (
    "macd", "macd_signal", "macd_hist", "macd_cross", "macd_state",
    "bb_upper", "bb_lower", "bb_pct_b",
    "volume_ratio", "drawdown_from_high_pct", "range_position_pct",
)


def _empty_indicators() -> dict:
    """지표를 계산할 수 없을 때의 기본값. 숫자는 None, 크로스/상태는 문자열로 둔다."""
    out = {field: None for field in INDICATOR_FIELDS}
    out["macd_cross"] = "none"
    return out


def _last_number(series_like):
    """Series의 마지막 값을 JSON 호환 float 또는 None으로 바꿉니다."""
    try:
        if series_like is None or len(series_like) == 0:
            return None
        return _to_number(series_like.iloc[-1])
    except (AttributeError, IndexError, TypeError):
        return None


def _macd_cross(macd_hist, lookback: int = MACD_CROSS_LOOKBACK) -> str:
    """최근 lookback봉 안에 MACD가 시그널선을 넘었는지 판정합니다.

    히스토그램 부호가 -/0 → +로 바뀌면 "bullish"(골든크로스), +/0 → -로 바뀌면
    "bearish"(데드크로스)다. 최근 구간에 크로스가 없으면 "none".
    (현재 MACD가 시그널 위/아래인지는 macd_state가 따로 알려준다.)
    """
    if macd_hist is None or len(macd_hist) < 2:
        return "none"
    tail = list(macd_hist.tail(max(2, lookback) + 1))
    for i in range(len(tail) - 1, 0, -1):
        prev, cur = tail[i - 1], tail[i]
        if pd.isna(prev) or pd.isna(cur):
            continue
        if prev <= 0 < cur:
            return "bullish"
        if prev >= 0 > cur:
            return "bearish"
    return "none"


def _calc_macd(close_series, fast: int = MACD_FAST, slow: int = MACD_SLOW,
               signal: int = MACD_SIGNAL) -> dict:
    """MACD(12,26,9): macd·macd_signal·macd_hist·macd_cross·macd_state."""
    out = {"macd": None, "macd_signal": None, "macd_hist": None,
           "macd_cross": "none", "macd_state": None}
    if close_series is None or len(close_series) < slow + signal:
        return out
    try:
        macd_line = (close_series.ewm(span=fast, adjust=False).mean()
                     - close_series.ewm(span=slow, adjust=False).mean())
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line
    except Exception:  # dtype 문제 등 — 지표만 비우고 나머지는 살린다
        return out
    macd_last = _last_number(macd_line)
    signal_last = _last_number(signal_line)
    hist_last = _last_number(hist)
    if macd_last is None or signal_last is None or hist_last is None:
        return out
    out.update({
        "macd": round(macd_last, 4),
        "macd_signal": round(signal_last, 4),
        "macd_hist": round(hist_last, 4),
        "macd_cross": _macd_cross(hist),
        "macd_state": "above" if hist_last > 0 else ("below" if hist_last < 0 else "equal"),
    })
    return out


def _calc_bollinger(close_series, period: int = BB_PERIOD, num_std: float = BB_STD) -> dict:
    """볼린저 밴드(20, 2σ): bb_upper·bb_lower·bb_pct_b.

    bb_pct_b = (현재가 - 하단) / (상단 - 하단). 0 미만이면 하단 이탈(과매도 후보),
    1 초과면 상단 이탈. 밴드 폭이 0이면(가격 완전 고정) None.
    """
    out = {"bb_upper": None, "bb_lower": None, "bb_pct_b": None}
    if close_series is None or len(close_series) < period:
        return out
    window = close_series.tail(period)
    if window.isna().any():
        return out
    try:
        mid = float(window.mean())
        sd = float(window.std(ddof=0))  # 표준 볼린저 밴드는 모집단 표준편차
        price = float(close_series.iloc[-1])
    except (TypeError, ValueError):
        return out
    if math.isnan(mid) or math.isnan(sd) or math.isnan(price):
        return out
    upper, lower = mid + num_std * sd, mid - num_std * sd
    span = upper - lower
    pct_b = None if span <= 0 else (price - lower) / span
    out.update({
        "bb_upper": round(upper, 2),
        "bb_lower": round(lower, 2),
        "bb_pct_b": None if pct_b is None else round(pct_b, 4),
    })
    return out


def _calc_volume_ratio(volume_series, window: int = VOLUME_WINDOW):
    """거래량 급증 배수 = 최근 거래량 / 직전 20거래일 평균 거래량.

    평균에서 당일을 뺀다(당일이 평균을 끌어올려 배수를 희석하는 것을 막는다).
    거래량이 없거나(NaN) 평균이 0이면 None.
    """
    if volume_series is None or len(volume_series) < 2:
        return None
    try:
        tail = volume_series.tail(window + 1).astype("float64")
    except (TypeError, ValueError):
        return None
    last = _to_number(tail.iloc[-1])
    base = _to_number(tail.iloc[:-1].mean())
    if last is None or base is None or base <= 0:
        return None
    return round(last / base, 2)


def _calc_range_position(close_series, high_series=None, low_series=None) -> dict:
    """6개월 고저 대비 위치.

    drawdown_from_high_pct: 6개월 최고가 대비 현재가 (예: -12.5 = 고점에서 12.5% 아래)
    range_position_pct: 6개월 최저~최고 구간에서 현재가의 위치 0~100%
    High/Low 컬럼이 있으면 장중 고저를, 없으면 종가로 계산한다.
    """
    out = {"drawdown_from_high_pct": None, "range_position_pct": None}
    if close_series is None or len(close_series) < 2:
        return out
    price = _last_number(close_series)
    if price is None or price <= 0:
        return out
    highs = high_series if high_series is not None and len(high_series) == len(close_series) else close_series
    lows = low_series if low_series is not None and len(low_series) == len(close_series) else close_series
    if highs is None or lows is None or len(highs.dropna()) == 0 or len(lows.dropna()) == 0:
        return out
    high = _to_number(highs.max())
    low = _to_number(lows.min())
    if high is None or low is None or high <= 0 or low <= 0 or high < low:
        return out
    out["drawdown_from_high_pct"] = round((price - high) / high * 100.0, 2)
    if high > low:
        position = (price - low) / (high - low) * 100.0
        out["range_position_pct"] = round(min(100.0, max(0.0, position)), 2)
    return out


def compute_advanced_indicators(hist) -> dict:
    """6개월 일봉으로 MACD·볼린저·거래량 배수·고점 대비·구간 위치를 한 번에 계산합니다.

    네트워크를 타지 않으며, 데이터가 짧거나 비어 있으면 None/"none"으로 채운다.
    반환 dict의 키는 INDICATOR_FIELDS와 같다.
    """
    out = _empty_indicators()
    if hist is None or getattr(hist, "empty", True) or "Close" not in getattr(hist, "columns", []):
        return out
    close = hist["Close"].astype("float64")
    high = hist["High"].astype("float64") if "High" in hist.columns else None
    low = hist["Low"].astype("float64") if "Low" in hist.columns else None
    volume = hist["Volume"].astype("float64") if "Volume" in hist.columns else None

    out.update(_calc_macd(close))
    out.update(_calc_bollinger(close))
    out["volume_ratio"] = _calc_volume_ratio(volume)
    out.update(_calc_range_position(close, high, low))
    return out


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
    indicators = compute_advanced_indicators(hist)

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

    # --- SEC EDGAR 폴백 ---
    # Yahoo t.info가 막히거나(429/401) 값을 비우면 SEC 공시(XBRL)에서 계산해 채운다.
    # current_price는 이미 차트에서 받은 값을 재사용하므로 추가 시세 호출이 없다.
    sec_financials: dict = {}
    if SEC_FALLBACK_ENABLED and price and any(fresh[key] is None for key in _SEC_FIELDS):
        try:
            sec_financials = sec_edgar.get_financial_metrics(sym, price) or {}
        except Exception as exc:  # 폴백 실패가 차트·시세를 망가뜨리지 않게
            logger.warning("%s: SEC EDGAR 폴백 실패 → %s: %s", sym, type(exc).__name__, str(exc)[:120])
            sec_financials = {}

    # --- Finnhub 폴백(3순위) ---
    # Yahoo도 SEC도 채우지 못한 핵심 지표가 남았을 때만 호출한다(예: us-gaap 태그가 없는
    # 외국 ADR TSM). 이미 SEC가 다 채운 미국 종목은 Finnhub 무료 쿼터를 쓰지 않는다.
    finnhub_financials: dict = {}
    still_missing = any(
        fresh[key] is None and sec_financials.get(key) is None
        for key in _FINNHUB_TRIGGER_FIELDS
    )
    if FINNHUB_FALLBACK_ENABLED and still_missing and finnhub_client.is_enabled():
        try:
            finnhub_financials = finnhub_client.get_financial_metrics(sym) or {}
        except Exception as exc:  # 폴백 실패가 차트·시세를 망가뜨리지 않게
            logger.warning("%s: Finnhub 폴백 실패 → %s: %s", sym, type(exc).__name__, str(exc)[:120])
            finnhub_financials = {}

    def _fill(key: str):
        """yfinance 최신값 > SEC EDGAR 계산값 > Finnhub 값 > 직전 캐시값 순으로 쓴다."""
        if fresh[key] is not None:
            return fresh[key]
        if sec_financials.get(key) is not None:
            return sec_financials[key]
        if finnhub_financials.get(key) is not None:
            return finnhub_financials[key]
        return previous[key]

    financials = {key: _fill(key) for key in _FIN_FIELDS}
    sec_filled = [key for key in _SEC_FIELDS
                  if fresh[key] is None and sec_financials.get(key) is not None]
    finnhub_filled = [key for key in _FIN_FIELDS
                      if fresh[key] is None and sec_financials.get(key) is None
                      and finnhub_financials.get(key) is not None]
    cache_filled = any(
        fresh[key] is None and sec_financials.get(key) is None
        and finnhub_financials.get(key) is None and previous[key] is not None
        for key in _FIN_FIELDS
    )
    financials_available = any(value is not None for value in financials.values())
    # t.info가 죽었는데 SEC·Finnhub도 못 채우고 직전 캐시만 남은 경우에만 stale로 표시한다.
    financials_stale = bool(cache_filled and not fresh_ok and not sec_filled and not finnhub_filled)

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
    # MACD·볼린저·거래량 배수·고점 대비·구간 위치(추가 필드, 기존 키는 그대로)
    payload.update(indicators)
    payload.update(financials)
    payload["financials_available"] = financials_available
    if financials_stale:
        payload["financials_stale"] = True
        payload["financials_note"] = "재무 지표 일시 미수신(Yahoo API 제한) — 직전 조회값 유지, 가격·이평선·RSI는 최신"
    elif sec_filled and finnhub_filled and not fresh_ok:
        payload["financials_note"] = "재무 지표는 SEC EDGAR 공시(XBRL)·Finnhub 기준으로 보완 — Yahoo API 제한 시 자동 대체, 가격·이평선·RSI는 최신"
    elif sec_filled and finnhub_filled:
        payload["financials_note"] = "일부 재무 지표는 SEC EDGAR 공시(XBRL)·Finnhub에서 보완"
    elif sec_filled and not fresh_ok:
        payload["financials_note"] = "재무 지표는 SEC EDGAR 공시(XBRL) 기준으로 보완 — Yahoo API 제한 시 자동 대체, 가격·이평선·RSI는 최신"
    elif sec_filled:
        payload["financials_note"] = "일부 재무 지표는 SEC EDGAR 공시(XBRL)에서 보완"
    elif finnhub_filled and not fresh_ok:
        payload["financials_note"] = "재무 지표는 Finnhub 데이터로 보완 — Yahoo·SEC 공시에 값이 없어 자동 대체, 가격·이평선·RSI는 최신"
    elif finnhub_filled:
        payload["financials_note"] = "일부 재무 지표는 Finnhub 데이터에서 보완"
    elif not financials_available:
        payload["financials_note"] = "재무 지표 일시 미수신(Yahoo API 제한) — 가격·이평선·RSI만 유효"
    if financials_available:
        # 어느 출처에서 채웠는지 남긴다(프론트엔드는 이 키를 몰라도 동작한다).
        if finnhub_filled:
            if not fresh_ok and not sec_filled:
                source = "finnhub"
            elif sec_filled and not fresh_ok:
                source = "sec_edgar+finnhub"
            elif sec_filled:
                source = "yfinance+sec_edgar+finnhub"
            else:
                source = "yfinance+finnhub"
        else:
            source = "sec_edgar" if (sec_filled and not fresh_ok) else "yfinance"
        payload["financials_source"] = source
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
