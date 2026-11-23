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
import naver_finance
import realtime_quotes
import sec_edgar
import sector_pe

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
# 받으면 429(rate limited)로 막으므로 8개까지만 묶어 둔다(예전 6 → 지연의 절반이 병렬도였다).
MAX_FETCH_WORKERS = 8

# 스크리닝 최종 15종목 조회의 병렬도. 한국 종목은 네이버 JSON(종목당 5~10KB)만 오가서
# 15개를 한 번에 띄워도 부담이 없지만, 미국 종목은 SEC companyfacts 원본이 커서
# (Render 무료 512MB) 8개를 유지한다 — 그래서 티커 구성에 따라 병렬도를 고른다.
SCREEN_FETCH_WORKERS = max(1, int(os.environ.get("SCREEN_FETCH_WORKERS", "15")))


def screen_workers_for(tickers) -> int:
    """스크리닝 15종목 조회에 쓸 병렬도 — 전부 한국 종목이면 더 넓게.

    코스피 200 발굴은 후보가 모두 `.KS`/`.KQ`라 네이버 재무(종목당 약 2초, 캐시 전)
    조회를 8개씩 두 바퀴 도는 대신 15개를 한 바퀴에 끝낸다. 미국 종목이 섞이면 SEC 원본
    다운로드가 몰리지 않게 8개로 돌아간다.
    """
    items = [str(t or "").strip() for t in tickers or [] if str(t or "").strip()]
    if items and all(naver_finance.is_korean_ticker(t) for t in items):
        return max(MAX_FETCH_WORKERS, min(SCREEN_FETCH_WORKERS, len(items)))
    return MAX_FETCH_WORKERS

# ticker -> (저장 시각, 결과 dict)
_METRICS_CACHE: dict[str, tuple[float, dict]] = {}
# "ticker:period" -> (저장 시각, 일봉 DataFrame, 유지 시간)
# 유지 시간을 항목마다 두는 이유: 프리 스캔(yf.download)이 미리 받아 둔 일봉은 스냅샷 수명(70분)만큼
# 살아 있어야 다음 사용자 요청이 같은 6개월 일봉을 다시 받지 않는다.
_HISTORY_CACHE: dict[str, tuple[float, object, float]] = {}
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

# --- Yahoo 호출 가드(전역 회로 차단기) --------------------------------------
# 왜 필요한가: Render 같은 데이터센터 IP에서는 yfinance `t.info`가 429/401(Invalid Crumb)로
# 막히는데, 그 실패는 **한 번에 30초씩** 걸린다(quoteSummary + additional + complementary를
# 각각 30초 타임아웃으로 시도). 15종목 배치면 30~80초가 통째로 '헛수고 대기'로 날아갔다.
# 그래서 (1) 한 호출에 벽시계 타임아웃을 걸고, (2) 차단성 실패가 **두 번 연속**이면 종목과
# 무관하게 **전역으로** 30분 동안 Yahoo t.info를 건너뛰고 바로 SEC EDGAR → Finnhub로 간다.
# (한국 종목은 애초에 t.info를 부르지 않는다 — 네이버 금융이 1순위다.)
YAHOO_INFO_TIMEOUT = 6.0        # t.info 한 번에 허용하는 최대 대기(초) — 이걸로는 차단을 못 뚫는다
YAHOO_NEWS_TIMEOUT = 5.0        # 종목 뉴스 한 번에 허용하는 최대 대기(초)
HISTORY_TIMEOUT = 8.0           # 일봉(차트) 조회 상한(초) — yfinance 기본 10초보다 조금 짧게
YAHOO_HISTORY_TIMEOUT = HISTORY_TIMEOUT

_INFO_FAIL_THRESHOLD = 2        # 429/401/crumb/타임아웃 같은 '차단성' 실패 2회 → 전역 우회
_INFO_OTHER_FAIL_THRESHOLD = 3  # 빈 응답 등 종목 문제일 수 있는 실패는 3회
_INFO_COOLDOWN_SECONDS = 1800   # 전역 우회 유지 시간(30분)
_NEWS_FAIL_THRESHOLD = 2        # 뉴스도 같은 규칙(차단성 실패 2회 → 30분 우회)
_NEWS_OTHER_FAIL_THRESHOLD = 3
_NEWS_COOLDOWN_SECONDS = 1800

_info_fail_streak = 0
_info_cooldown_until = 0.0
_info_last_error = ""
_news_fail_streak = 0
_news_cooldown_until = 0.0
_news_last_error = ""


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
    last_loss = _to_number(loss.iloc[-1])
    last_gain = _to_number(gain.iloc[-1])
    if last_loss is None or last_gain is None:
        return None  # 값이 비었거나(NaN) 무한대 → 지표 없음
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    value = 100 - (100 / (1 + rs))
    return round(value, 2) if math.isfinite(value) else None


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


# --- 벽시계 타임아웃 ---------------------------------------------------------
# yfinance는 HTTP 타임아웃이 요청마다 걸려 있을 뿐(t.info는 요청 3~4개, 각 30초), 전체 호출의
# 상한이 없다. 그래서 별도 스레드에서 돌리고 결과를 timeout초까지만 기다린다.
# 시간이 지나면 그 호출은 '버린다' — 서버 응답은 그대로 진행하고, 남은 스레드는
# 브레이커가 막아 더 이상 늘지 않는다. 스레드는 데몬이라 프로세스 종료를 볼잡지도 않는다.
class CallTimeout(Exception):
    """정해진 시간 안에 호출이 끝나지 않음."""


def call_with_timeout(fn, timeout: float):
    """fn()을 제한 시간 안에서 실행하고 결과를 돌려줍니다(넘기면 CallTimeout)."""
    limit = max(0.1, float(timeout))
    box: dict = {}
    done = threading.Event()

    def _run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # 호출한 쪽으로 그대로 전달한다
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_run, name="yahoo-call", daemon=True).start()
    if not done.wait(limit):
        raise CallTimeout(f"{limit:.1f}초 안에 응답 없음")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# 차단(레이트 리밋·crumb·인증) 신호. 이런 실패는 종목과 무관하게 서버 IP 문제다.
_BLOCK_HINTS = ("429", "401", "403", "crumb", "too many requests", "rate limit",
                "ratelimit", "unauthorized", "forbidden")


def _looks_blocked(reason: str) -> bool:
    """실패 사유가 'Yahoo가 이 서버를 막았다'는 신호인지 판단합니다."""
    text = str(reason or "").lower()
    return any(hint in text for hint in _BLOCK_HINTS)


def _note_info_failure(kind: str = "other", reason: str = "") -> None:
    """t.info 실패를 기록하고, 임계치를 넘으면 전역 우회(30분)를 켭니다."""
    global _info_fail_streak, _info_cooldown_until, _info_last_error
    threshold = _INFO_FAIL_THRESHOLD if kind == "block" else _INFO_OTHER_FAIL_THRESHOLD
    with _CACHE_LOCK:
        _info_fail_streak += 1
        _info_last_error = str(reason or "")[:160]
        if _info_fail_streak >= threshold:
            _info_cooldown_until = time.time() + _INFO_COOLDOWN_SECONDS
            _info_fail_streak = 0
            logger.warning(
                "t.info 연속 실패 %d회(%s) → %d초 동안 전역으로 Yahoo t.info를 건너뜁니다.",
                threshold, _info_last_error or kind, _INFO_COOLDOWN_SECONDS,
            )


def _note_info_success() -> None:
    global _info_fail_streak
    with _CACHE_LOCK:
        _info_fail_streak = 0


def _info_in_cooldown() -> bool:
    with _CACHE_LOCK:
        return time.time() < _info_cooldown_until


def _info_cooldown_left() -> int:
    with _CACHE_LOCK:
        return max(0, int(_info_cooldown_until - time.time()))


def info_breaker_status() -> dict:
    """Yahoo t.info 전역 차단 상태(`GET /`의 `yahoo_guard`)."""
    with _CACHE_LOCK:
        left = max(0, int(_info_cooldown_until - time.time()))
        return {"blocked": left > 0, "remaining_sec": left, "fail_streak": _info_fail_streak,
                "threshold": _INFO_FAIL_THRESHOLD, "cooldown_sec": _INFO_COOLDOWN_SECONDS,
                "last_error": _info_last_error}


def reset_info_breaker() -> None:
    """테스트·수동 초기화용 — t.info 차단 상태를 초기화한다."""
    global _info_fail_streak, _info_cooldown_until, _info_last_error
    with _CACHE_LOCK:
        _info_fail_streak = 0
        _info_cooldown_until = 0.0
        _info_last_error = ""


def _note_news_failure(kind: str = "other", reason: str = "") -> None:
    """Yahoo 뉴스 실패 기록 — 뉴스도 같은 429/타임아웃 문에 막히므로 전역으로 우회한다."""
    global _news_fail_streak, _news_cooldown_until, _news_last_error
    threshold = _NEWS_FAIL_THRESHOLD if kind == "block" else _NEWS_OTHER_FAIL_THRESHOLD
    with _CACHE_LOCK:
        _news_fail_streak += 1
        _news_last_error = str(reason or "")[:160]
        if _news_fail_streak >= threshold:
            _news_cooldown_until = time.time() + _NEWS_COOLDOWN_SECONDS
            _news_fail_streak = 0
            logger.warning("Yahoo 뉴스 연속 실패 %d회(%s) → %d초 동안 건너뜁니다.",
                           threshold, _news_last_error, _NEWS_COOLDOWN_SECONDS)


def _note_news_success() -> None:
    global _news_fail_streak
    with _CACHE_LOCK:
        _news_fail_streak = 0


def _news_in_cooldown() -> bool:
    with _CACHE_LOCK:
        return time.time() < _news_cooldown_until


def news_breaker_status() -> dict:
    with _CACHE_LOCK:
        left = max(0, int(_news_cooldown_until - time.time()))
        return {"blocked": left > 0, "remaining_sec": left, "fail_streak": _news_fail_streak,
                "threshold": _NEWS_FAIL_THRESHOLD, "cooldown_sec": _NEWS_COOLDOWN_SECONDS,
                "last_error": _news_last_error}


def reset_news_breaker() -> None:
    global _news_fail_streak, _news_cooldown_until, _news_last_error
    with _CACHE_LOCK:
        _news_fail_streak = 0
        _news_cooldown_until = 0.0
        _news_last_error = ""


def clean_history(hist):
    """일봉에서 종가가 없는 행을 걸러 냅니다.

    야후 파이낸스는 한국 종목(.KS/.KQ)의 최신 세션을 **OHLC 전부 NaN, 거래량만 있는**
    자리표시자 행으로 내려줄 때가 있다(예: 005930.KS 2026-11-19행). 그 행을 그대로 쓰면
    `close.iloc[-1]`이 NaN이라 현재가·20일선·60일선·RSI가 모두 NaN이 되고, Starlette의
    JSONResponse는 `allow_nan=False`로 직렬화하므로 NaN 하나만 섞여도 **500 Internal Server Error**가
    난다(2026-11 한국 종목 500 사고의 원인). 종가가 없는 행은 지표 계산에 쓸 수 없으므로 버린다.

    반환: 종가가 있는 행만 남긴 DataFrame(바뀐 게 없으면 받은 객체 그대로).
    """
    if hist is None or getattr(hist, "empty", True):
        return hist
    if "Close" not in getattr(hist, "columns", []):
        return hist
    try:
        close = hist["Close"]
        if not close.isna().any():
            return hist  # 정상 데이터면 복사하지 않는다
        cleaned = hist[close.notna()]
    except Exception as exc:  # dtype 등 예상 밖 문제 — 원본을 그대로 넘기고 호출부가 방어한다
        logger.warning("일봉 정리 실패 → %s: %s", type(exc).__name__, str(exc)[:120])
        return hist
    return cleaned


def _history_ttl(entry) -> float:
    """캐시 항목의 유지 시간(예전 2-튜플 형식도 받아 준다)."""
    return float(entry[2]) if len(entry) > 2 else HISTORY_TTL


def get_history(ticker: str, period: str = "6mo"):
    """일봉 데이터를 캐시에서 반환합니다(HISTORY_TTL 30분, 프리 스캔분은 더 길게).

    조회에 실패하면 만료된 직전 성공값이라도 돌려주고, 그것도 없으면 빈 DataFrame을
    돌려줍니다. 반환된 DataFrame은 읽기 전용으로만 쓰세요.
    종가가 비어 있는 행(야후의 한국 종목 자리표시자 행)은 `clean_history`로 걸러 캐시한다.

    프리 스캔(`sp500_scanner.scan_rows`)이 `yf.download`로 이미 받아 둔 6개월 일봉은
    `prime_history`로 여기에 미리 넣어 둔다. 그러면 sp500·kospi200 발굴 뒤 15종목의
    차트 지표·차트 부착(`_attach_chart`)이 **네트워크 0회**로 끝난다.

    캐시 읽기·쓰기는 _CACHE_LOCK으로 보호하므로 bounded_map으로 동시에 불러도 안전하다.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    key = f"{sym}:{period}"
    now = time.time()
    with _CACHE_LOCK:
        cached = _HISTORY_CACHE.get(key)
    if cached and now - cached[0] < _history_ttl(cached):
        return clean_history(cached[1])

    try:
        hist = call_with_timeout(lambda: yf.Ticker(sym).history(period=period), YAHOO_HISTORY_TIMEOUT)
    except CallTimeout:
        logger.warning("%s: history(%s) 타임아웃(%.0f초) → 직전 값 사용", sym, period,
                       YAHOO_HISTORY_TIMEOUT)
        hist = None
    except Exception as exc:  # 429/401/네트워크 오류 모두 여기서 흡수
        logger.warning("%s: history(%s) 실패 → %s: %s", sym, period, type(exc).__name__, str(exc)[:200])
        hist = None

    hist = clean_history(hist)
    if hist is not None and not getattr(hist, "empty", True):
        with _CACHE_LOCK:
            _HISTORY_CACHE[key] = (now, hist, HISTORY_TTL)
        return hist
    if cached:  # 만료됐지만 이번 조회도 실패 → 직전 성공값 사용
        return clean_history(cached[1])
    return hist


# 차트에 필요한 기둥만 남긴다(Open은 어디에도 안 쓴다 — 500종목 일봉 메모리를 아긴다).
_HISTORY_KEEP_COLUMNS = ("High", "Low", "Close", "Volume")


def prime_history(ticker: str, hist, period: str = "6mo", ttl: float | None = None) -> bool:
    """프리 스캔이 이미 받아 둔 일봉을 히스토리 캐시에 미리 넣는다(네트워크 0회 재사용).

    `sp500_scanner.scan_rows`가 `yf.download`로 500종목 일봉을 이미 받아 놓고도 버리고
    있었기 때문에, 발굴된 15종목의 `get_stock_metrics`·`_attach_chart`가 같은 6개월 일봉을
    다시 받느라 시간을 썼다. 여기에 넣어 두면 그 재다운로드가 사라진다.

    ttl 기본값은 스냅샷과 같은 `HISTORY_TTL`(30분)이고, 프리 스캔은 스냅샷 수명(70분)을 넘긴다.
    반환: 실제로 캐시에 넣었으면 True(빈 프레임·종가 없음은 False).
    """
    sym = (ticker or "").strip().upper()
    if not sym or hist is None or getattr(hist, "empty", True):
        return False
    try:
        columns = [c for c in _HISTORY_KEEP_COLUMNS if c in hist.columns]
        if "Close" not in columns:
            return False
        slim = hist[columns].copy()
        slim = slim[slim["Close"].notna()]
        if slim.empty:
            return False
        slim["Close"] = slim["Close"].astype("float64")
    except Exception as exc:  # pandas dtype 등 예상 밖 문제 — 프리 스캔을 죽이지 않는다
        logger.warning("%s: 일봉 캐시 적재 실패 → %s: %s", sym, type(exc).__name__, str(exc)[:120])
        return False
    with _CACHE_LOCK:
        _HISTORY_CACHE[f"{sym}:{period}"] = (time.time(), slim, HISTORY_TTL if ttl is None else float(ttl))
    return True


def history_cache_status() -> dict:
    """히스토리 캐시 상태(개수·프리 스캔 적재분) — `GET /` 진단용."""
    now = time.time()
    with _CACHE_LOCK:
        entries = list(_HISTORY_CACHE.values())
    alive = [e for e in entries if now - e[0] < _history_ttl(e)]
    return {"entries": len(alive), "stale_entries": len(entries) - len(alive)}


def clear_history_cache() -> None:
    with _CACHE_LOCK:
        _HISTORY_CACHE.clear()


def _fetch_info(sym: str):
    """t.info를 조회합니다(벽시계 타임아웃 + 전역 회로 차단기).

    반환: (info 또는 None, 실패 사유 문자열 또는 None).
    차단성 실패(429/401/crumb/타임아웃)가 두 번 연속이면 `_info_cooldown_until`이 켜져
    이후 호출은 곧바로 실패로 돌아간다 — 종목마다 30초씩 기다리는 일이 없다.
    """
    if _info_in_cooldown():
        return None, f"t.info 전역 우회 중({_info_cooldown_left()}초 남음)"
    try:
        info = call_with_timeout(lambda: yf.Ticker(sym).info, YAHOO_INFO_TIMEOUT)
    except CallTimeout:
        _note_info_failure("block", "timeout")
        logger.warning("%s: t.info 타임아웃(%.1f초)", sym, YAHOO_INFO_TIMEOUT)
        return None, f"t.info 타임아웃({YAHOO_INFO_TIMEOUT:.0f}초)"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:120]}"
        _note_info_failure("block" if _looks_blocked(reason) else "other", reason)
        logger.warning("%s: t.info 실패 → %s", sym, reason)
        return None, reason
    if not isinstance(info, dict) or not info:
        _note_info_failure("other", "빈 응답")
        logger.warning("%s: t.info 빈 응답(재무 지표 미수신)", sym)
        return None, "t.info 빈 응답"
    _note_info_success()
    return info, None


def fetch_yahoo_news(sym: str, limit: int = 5) -> list[str]:
    """Yahoo 종목 뉴스 제목(벽시계 타임아웃 + 전역 회로 차단기).

    뉴스는 없어도 스크리닝은 성립하므로, 막히면 빈 목록을 돌려주고 30분 동안
    전역으로 건너뛴다(뉴스 하나 때문에 배치가 30초씩 느려지지 않게).
    """
    sym = (sym or "").strip().upper()
    if not sym or _news_in_cooldown():
        return []
    try:
        items = call_with_timeout(lambda: yf.Ticker(sym).news, YAHOO_NEWS_TIMEOUT) or []
    except CallTimeout:
        _note_news_failure("block", "timeout")
        return []
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:120]}"
        _note_news_failure("block" if _looks_blocked(reason) else "other", reason)
        return []
    _note_news_success()
    titles: list[str] = []
    for item in items[:max(1, limit)]:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or (item.get("content") or {}).get("title")
        if title:
            titles.append(title)
    return titles

def _metrics_ttl(payload: dict) -> float:
    """결과 성격에 따라 캐시 유지 시간을 정합니다."""
    if payload.get("financials_available") and not payload.get("financials_stale"):
        return METRICS_TTL
    if payload.get("financials_available"):
        return STALE_TTL
    return PARTIAL_TTL


def _trend_text(price, ma20, ma60) -> str:
    """현재가·20일선·60일선 배열로 추세 문구를 만든다(실시간가 재판정에도 같은 문구를 쓴다)."""
    if ma20 is None or ma60 is None or price is None:
        return "판단불가"
    if price > ma20 > ma60:
        return "상승 추세(정배열: 현재가>20일선>60일선)"
    if price < ma20 < ma60:
        return "하락 추세(역배열: 현재가<20일선<60일선)"
    return "횡보/혼조"


def _build_metrics(sym: str, cached_payload: dict | None) -> dict:
    hist = get_history(sym, "6mo")
    if hist is None or getattr(hist, "empty", True):
        # 차트 데이터도 못 받았고, 직전 성공값이 있으면 그대로 내보낸다.
        if cached_payload and cached_payload.get("price") is not None:
            return dict(cached_payload)
        return {"ticker": sym, "error": "주가 데이터를 찾을 수 없습니다."}

    close = hist["Close"]
    # 종가가 비어 있는 행은 get_history가 이미 걸러 내지만, 지표는 한 번 더 방어한다
    # (NaN이 응답에 남으면 JSON 직렬화 단계에서 500이 된다).
    price = _to_number(round(float(close.iloc[-1]), 2)) if len(close) else None
    if price is None:
        if cached_payload and cached_payload.get("price") is not None:
            return dict(cached_payload)
        return {"ticker": sym, "error": "주가 데이터를 찾을 수 없습니다."}
    ma20 = _to_number(round(float(close.rolling(20).mean().iloc[-1]), 2)) if len(close) >= 20 else None
    ma60 = _to_number(round(float(close.rolling(60).mean().iloc[-1]), 2)) if len(close) >= 60 else None
    rsi14 = _calc_rsi(close, 14)
    indicators = compute_advanced_indicators(hist)

    trend = _trend_text(price, ma20, ma60)

    # --- 재무 지표: 실패해도 차트 지표는 그대로 살린다 ---
    # 한국 종목(.KS/.KQ)은 Yahoo `t.info`를 **아예 부르지 않는다**. Render 같은 데이터센터
    # IP에서 t.info가 막혀 종목마다 30초씩 기다린 뒤에야 네이버로 넘어가던 것이
    # 30~100초 지연의 주범이었다. 한국 종목 재무는 1순위가 네이버 금융이다(§아래 `_fill`).
    is_korean = naver_finance.is_korean_ticker(sym)
    if is_korean:
        info, info_error = None, "한국 종목: Yahoo t.info 생략(네이버 금융 우선)"
    else:
        info, info_error = _fetch_info(sym)
    fresh = {key: _to_number((info or {}).get(src)) for key, src in _FIN_FIELDS.items()}
    fresh_ok = any(value is not None for value in fresh.values())
    cached_payload = cached_payload or {}
    previous = {key: _to_number(cached_payload.get(key)) for key in _FIN_FIELDS}

    # --- 네이버 금융 폴백(한국 종목 전용) ---
    # yfinance는 `.KS`/`.KQ` 시세·차트는 주지만 Render 같은 데이터센터 IP에서는 `t.info`가
    # 막히거나 비고(삼성전자 trailingPE=None), 종목명도 영문이다. 한국 종목이면 네이버 금융에서
    # PER·영업이익률·부채비율·매출증가율과 한국어 이름을 받아 쓴다(6시간 캐시).
    naver_fin: dict = {}
    if is_korean:
        try:
            naver_fin = naver_finance.get_fundamentals(sym) or {}
        except Exception as exc:  # 폴백 실패가 차트·시세를 망가뜨리지 않게
            logger.warning("%s: 네이버 금융 폴백 실패 → %s: %s", sym, type(exc).__name__, str(exc)[:120])
            naver_fin = {}

    # --- SEC EDGAR 폴백 ---
    # Yahoo t.info가 막히거나(429/401) 값을 비우면 SEC 공시(XBRL)에서 계산해 채운다.
    # current_price는 이미 차트에서 받은 값을 재사용하므로 추가 시세 호출이 없다.
    # 한국 종목은 SEC 공시(XBRL)에 재무가 없으므로 건너뛴다(네이버가 그 자리를 맡는다).
    sec_financials: dict = {}
    if SEC_FALLBACK_ENABLED and not is_korean and price and any(fresh[key] is None for key in _SEC_FIELDS):
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
        and naver_fin.get(key) is None
        for key in _FINNHUB_TRIGGER_FIELDS
    )
    if FINNHUB_FALLBACK_ENABLED and not is_korean and still_missing and finnhub_client.is_enabled():
        try:
            finnhub_financials = finnhub_client.get_financial_metrics(sym) or {}
        except Exception as exc:  # 폴백 실패가 차트·시세를 망가뜨리지 않게
            logger.warning("%s: Finnhub 폴백 실패 → %s: %s", sym, type(exc).__name__, str(exc)[:120])
            finnhub_financials = {}

    def _fill(key: str):
        """한국 종목은 네이버 > yfinance, 그 밖은 yfinance > SEC EDGAR > Finnhub > 직전 캐시.

        한국 종목에서 네이버를 앞세우는 이유: yfinance의 한국 종목 재무는 기간이 어긋나
        영업이익률·매출증가율이 국내 공시와 크게 벌어지는 경우가 있다(삼성전자 영업이익률
        46% vs 연간 보고서 13%). 현지 공시(네이버 연간 재무제표)를 우선하고, 없을 때만
        Yahoo 값을 쓴다.
        """
        if is_korean and naver_fin.get(key) is not None:
            return naver_fin[key]
        if fresh[key] is not None:
            return fresh[key]
        if naver_fin.get(key) is not None:
            return naver_fin[key]
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
    naver_used = [key for key in _FIN_FIELDS if naver_fin.get(key) is not None]
    cache_filled = any(
        fresh[key] is None and sec_financials.get(key) is None
        and finnhub_financials.get(key) is None and naver_fin.get(key) is None
        and previous[key] is not None
        for key in _FIN_FIELDS
    )
    financials_available = any(value is not None for value in financials.values())
    # t.info가 죽었는데 네이버·SEC·Finnhub도 못 채우고 직전 캐시만 남은 경우에만 stale로 표시한다.
    financials_stale = bool(cache_filled and not fresh_ok and not sec_filled
                            and not finnhub_filled and not naver_used)

    name = None
    if info:
        name = info.get("shortName") or info.get("longName")
    if is_korean:
        # 한국 종목은 영어 shortName(SamsungElec)보다 네이버의 한글 종목명을 쓴다.
        name = naver_fin.get("name") or naver_finance.static_display_name(sym) or name
    name = name or cached_payload.get("name") or sym

    # --- 동종업계 비교 기준(2026-11 추가) ---
    # 한국: 네이버가 같은 업종 종목 PER로 만든 업종명·업종 기준 PER.
    # 미국: GICS 섹터(Yahoo info 우선, 없으면 S&P 500 표)를 쓰고, 업종 PER은 스크리닝이 지나가며
    #       모은 섹터 표본의 중앙값을 api 단계에서 붙인다(500종목 PER을 새로 받지 않는다).
    sector = None
    industry_pe = None
    if is_korean:
        sector = sector_pe.normalize_sector(naver_fin.get("sector")) or cached_payload.get("sector")
        industry_pe = _to_number(naver_fin.get("industry_pe"))
        if industry_pe is None:
            industry_pe = _to_number(cached_payload.get("industry_pe"))
    else:
        sector = (sector_pe.normalize_sector((info or {}).get("sector"))
                  or sector_pe.us_sector(sym) or cached_payload.get("sector"))
        # 섹터 표본에 이 종목의 PER을 남긴다 → 다음 스크리닝의 업종 중앙값이 두터워진다.
        sector_pe.record_pe(sector, sym, financials.get("trailing_pe"))

    payload = {
        "ticker": sym,
        "name": name,
        "currency": naver_finance.currency_for(sym),
        "price": price,
        "ma20": ma20,
        "ma60": ma60,
        "rsi14": rsi14,
        "trend": trend,
    }
    # MACD·볼린저·거래량 배수·고점 대비·구간 위치(추가 필드, 기존 키는 그대로)
    payload.update(indicators)
    payload.update(financials)
    if sector:
        payload["sector"] = sector
    if industry_pe is not None:
        payload["industry_pe"] = industry_pe
    payload["financials_available"] = financials_available
    if financials_stale:
        payload["financials_stale"] = True
        payload["financials_note"] = "재무 지표 일시 미수신(Yahoo API 제한) — 직전 조회값 유지, 가격·이평선·RSI는 최신"
    elif naver_used and is_korean:
        payload["financials_note"] = "재무 지표는 네이버 금융(연간 재무제표) 기준 — 한국 종목은 국내 공시 값을 우선한다"
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
        if naver_used:
            # 한국 종목: 재무 지표는 네이버 금융 연간 재무제표에서 왔다(SEC·Finnhub는 호출하지 않는다).
            source = "yfinance+naver" if fresh_ok else "naver"
        elif finnhub_filled:
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


# --- 실시간 현재가 오버레이 --------------------------------------------------
# 지표·재무는 30분 캐시(METRICS_TTL) 그대로 두고 **현재가만** 실시간으로 덮어쓴다.
# S&P 500/코스피 200 프리 스캔에는 쓰지 않고, 최종 15종목·성적표처럼 종목 수가 적은
# 응답 직전에만 부른다(한국은 네이버 요청 1회, 미국은 Finnhub 병렬 8개).


def apply_realtime_prices(rows: list, quotes: dict | None = None,
                         max_workers: int = realtime_quotes.QUOTE_WORKERS) -> list:
    """종목 dict 목록의 `price`를 실시간 현재가로 덮어쓰고 `price_source`를 붙인다.

    - 한국 종목: 네이버 polling → `price_source="naver_realtime"`
    - 미국 종목: Finnhub `/quote` → `price_source="finnhub_quote"`
    - 조회 실패·미지원:
        기존 일봉 종가 유지 → `price_source="daily_close"`
    `price`가 바뀌면 추세 문구(`trend`)도 새 가격으로 다시 판정해, 화면에서 "현재가보다
    20일선이 낮은데 역배열"처럼 앞뒤가 맞지 않는 일이 없게 한다.

    `quotes`를 주면 그 시세를 그대로 쓴다 — 호출부가 지표 조회와 **병렬로** 미리 받아 둔
    경우(스크리닝) 추가 네트워크 없이 적용할 수 있다.

    반환: 같은 리스트(항목 dict를 제자리에서 바꾼다). rows에 `ticker`/`price`가 없으면
    그대로 두고 건드리지 않는다.
    """
    if not isinstance(rows, list):
        return rows
    targets = [row for row in rows
               if isinstance(row, dict) and row.get("ticker") and not row.get("error")]
    if not targets:
        return rows

    if quotes is None:
        try:
            quotes = realtime_quotes.get_prices([row.get("ticker") for row in targets],
                                                max_workers=max_workers)
        except Exception as exc:  # 실시간 시세 계층 전체 실패 → 전부 일봉 종가 유지
            logger.warning("실시간 시세 오버레이 실패: %s: %s", type(exc).__name__, str(exc)[:160])
            quotes = {}

    for row in targets:
        sym = str(row.get("ticker") or "").strip().upper()
        quote = quotes.get(sym) or {}
        price = _to_number(quote.get("price"))
        if price is not None and price > 0:
            row["price"] = round(float(price), 2)
            row["price_source"] = realtime_quotes.source_of(quote)
            row["trend"] = _trend_text(row["price"], _to_number(row.get("ma20")),
                                       _to_number(row.get("ma60")))
            if quote.get("market_status"):
                row["market_status"] = quote["market_status"]
        else:
            row["price_source"] = realtime_quotes.SOURCE_DAILY
    return rows


if __name__ == "__main__":
    for sym in ["NVDA", "AAPL"]:
        print(get_stock_metrics(sym))
