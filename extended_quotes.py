"""미국 주식 시간외(프리·애프터마켓) 시세 — Yahoo v8 chart 메타.

왜 필요한가
-----------
Finnhub `/quote`의 `c`는 **정규장 체결가**다. 한국 낮(미국 프리마켓·애프터마켓·주말)에는
정규장이 닫혀 있어 Finnhub가 직전 정규장 종가만 돌려주고, 토스증권처럼 시간외 마지막
체결가를 보여주는 앱과 값이 어긋난다(예: SKHY 103.57 vs 106.30, MNST 43.69 vs 43.27).

Yahoo v8 chart(`range=1d&includePrePost=true`)의 `meta`에는
- `regularMarketPrice`       … 정규장 종가
- `chartPreviousClose`       … 직전 거래일 종가
- `fulldayChangePercent`     … 프리·애프터까지 포함한 마지막 체결가 기준 등락률
- `hasPrePostMarketData`     … 시간외 데이터 유무

가 들어 있다. 그래서 **시간외 마지막 체결가 = previousClose × (1 + fulldayChangePercent/100)**
로 정확히 복원된다(2026-11-21 실측: AAPL·MSFT·MNST·SKHY·TXT·MLM·COST·NVDA 등 12종목 전부
실제 애프터마켓 종가와 일치, 네이버 해외주식 `overMarketPriceInfo.overPrice`와도 일치).
`meta.fulldayPrice`는 값이 어긋나서 쓰지 않는다. 응답은 interval=1d라 1.3KB·0.08초로 가볍다.

한계
----
토스증권의 '데이마켓'(블루오션 ATS, 한국 낮 시간) 시세는 Yahoo·네이버 무료 소스에 없다.
이 모듈이 맞출 수 있는 것은 **프리마켓·애프터마켓 마지막 체결가**까지다.

속도·안전
---------
- 종목당 요청 1회(최대 8개 병렬), 3초 타임아웃, 30초 캐시(실패는 90초만 기억).
- 3회 연속 실패하면 10분 동안 스스로 쉰다(데이터센터 IP 차단 시 스크리닝 지연 방지).
- `EXTENDED_QUOTES=0`이면 아무 호출도 하지 않는다(운영 스위치).
"""
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger("extended_quotes")

SOURCE_PREPOST = "yahoo_prepost"   # 프리·애프터마켓 마지막 체결가
SOURCE_YAHOO = "yahoo_quote"       # 정규장가(시간외 정보가 없을 때의 보조)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
REQUEST_TIMEOUT = 3.0     # 초 — 스크리닝 응답을 붙잡지 않는다
QUOTE_TTL = 30            # 시간외 체결은 분 단위로도 잘 바뀌지 않는다
ERROR_TTL = 90
WORKERS = 8
FAIL_THRESHOLD = 3        # 연속 실패 → 전역 휴식
COOLDOWN_SECONDS = 600

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockAIScanner/1.0",
    "Accept": "application/json",
}

try:  # 미국 동부시간(서머타임 자동). tz 데이터가 없으면 EST 고정으로 물러난다.
    from zoneinfo import ZoneInfo

    US_EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - 표준 라이브러리 tz가 없는 극단 환경
    US_EASTERN = timezone(timedelta(hours=-5))

# 티커 -> (저장 시각, 유지 시간, 시세 dict 또는 None)
_CACHE: dict[str, tuple[float, float, dict | None]] = {}
_LOCK = threading.Lock()

_FAIL_STREAK = 0
_COOLDOWN_UNTIL = 0.0
_LAST_ERROR = ""


class YahooChartError(RuntimeError):
    """HTTP 실패를 상태코드만으로 전달한다(쿼리·쿠키를 메시지에 남기지 않는다)."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


# --- 설정 -------------------------------------------------------------------

def is_enabled() -> bool:
    """`EXTENDED_QUOTES=0`으로 끌 수 있다(매번 환경변수를 읽어 테스트·운영에 바로 반영)."""
    return (os.environ.get("EXTENDED_QUOTES") or "1").strip() not in ("0", "false", "False")


def is_us_regular_session(now: datetime | None = None) -> bool:
    """미국 정규장(평일 09:30–16:00 ET)인지 — 이때만 Finnhub 실시간가를 우선한다.

    공휴일은 구분하지 않는다(휴일에는 Finnhub도 직전 종가를 주므로 값이 같다).
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    eastern = moment.astimezone(US_EASTERN)
    if eastern.weekday() >= 5:      # 토·일
        return False
    minutes = eastern.hour * 60 + eastern.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _cooldown_active(now: float | None = None) -> bool:
    with _LOCK:
        return (now or time.time()) < _COOLDOWN_UNTIL


def _record_failure(message: str) -> None:
    global _FAIL_STREAK, _COOLDOWN_UNTIL, _LAST_ERROR
    with _LOCK:
        _FAIL_STREAK += 1
        _LAST_ERROR = str(message or "")[:160]
        if _FAIL_STREAK >= FAIL_THRESHOLD:
            _COOLDOWN_UNTIL = time.time() + COOLDOWN_SECONDS
            _LAST_ERROR = f"{_LAST_ERROR} (연속 {_FAIL_STREAK}회 실패 → {COOLDOWN_SECONDS}초 휴식)"


def _record_success() -> None:
    global _FAIL_STREAK, _LAST_ERROR
    with _LOCK:
        _FAIL_STREAK = 0
        _LAST_ERROR = ""


def status() -> dict:
    """`GET /` 진단용 — 설정·차단 상태(가격 값은 노출하지 않는다)."""
    now = time.time()
    with _LOCK:
        return {
            "enabled": is_enabled(),
            "ttl_sec": QUOTE_TTL,
            "blocked": now < _COOLDOWN_UNTIL,
            "remaining_sec": max(0, int(_COOLDOWN_UNTIL - now)),
            "fail_streak": _FAIL_STREAK,
            "last_error": _LAST_ERROR,
        }


def clear_cache() -> None:
    """테스트·수동 초기화용(차단 상태까지 지운다)."""
    global _FAIL_STREAK, _COOLDOWN_UNTIL, _LAST_ERROR
    with _LOCK:
        _CACHE.clear()
        _FAIL_STREAK = 0
        _COOLDOWN_UNTIL = 0.0
        _LAST_ERROR = ""


# --- 파싱 -------------------------------------------------------------------

def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def parse_chart_meta(payload) -> dict | None:
    """Yahoo v8 chart 응답 → 시세 dict. 가격이 없으면 None.

    반환: `{"price", "source", "session", "regular_price", "change_pct", "currency", "as_of"}`
    - `session="extended"`면 `price`가 프리·애프터마켓 마지막 체결가다(`source=yahoo_prepost`).
    - `session="regular"`이면 정규장가(`source=yahoo_quote`) — 시간외 체결이 없거나 같을 때.
    """
    if not isinstance(payload, dict):
        return None
    result = ((payload.get("chart") or {}).get("result") or [])
    if not result or not isinstance(result[0], dict):
        return None
    meta = result[0].get("meta") or {}
    regular = _num(meta.get("regularMarketPrice"))
    if regular is None or regular <= 0:
        return None

    price = regular
    session = "regular"
    change_pct = _num(meta.get("fulldayChangePercent"))
    previous = _num(meta.get("chartPreviousClose")) or _num(meta.get("previousClose"))
    has_prepost = meta.get("hasPrePostMarketData")
    if previous and previous > 0 and change_pct is not None and has_prepost is not False:
        extended = round(previous * (1.0 + change_pct / 100.0), 2)
        # 말이 안 되는 값(50% 이상 괴리)은 버린다 — 메타가 다른 날짜와 섞인 경우 방어.
        if extended > 0 and 0.5 <= extended / regular <= 1.5 and abs(extended - regular) > 0.004:
            price, session = extended, "extended"

    return {
        "price": price,
        "source": SOURCE_PREPOST if session == "extended" else SOURCE_YAHOO,
        "session": session,
        "regular_price": regular,
        "change_pct": change_pct,
        "currency": meta.get("currency"),
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --- 조회 -------------------------------------------------------------------

def _http_json(symbol: str) -> dict:
    resp = requests.get(
        CHART_URL.format(symbol=symbol),
        params={"range": "1d", "interval": "1d", "includePrePost": "true"},
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise YahooChartError(resp.status_code)
    return resp.json()


def get_quote(ticker: str, fetch=None) -> dict | None:
    """한 종목의 시간외 포함 시세. 30초 캐시, 실패는 90초만 기억.

    `fetch`는 테스트·대체 경로 주입점(티커 → JSON dict).
    실패(타임아웃·HTTP·파싱)는 조용한 None — 호출부가 Finnhub/일봉 종가를 유지한다.
    """
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(sym)
    if cached and now - cached[0] < cached[1]:
        return dict(cached[2]) if cached[2] else None

    if _cooldown_active(now):
        return None

    try:
        payload = (fetch or _http_json)(sym)
        quote = parse_chart_meta(payload)
    except Exception as exc:  # 타임아웃·429·JSON 오류 모두 흡수
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("%s: Yahoo 시간외 시세 실패 → %s", sym, detail[:160])
        _record_failure(f"{sym} {detail}")
        with _LOCK:
            _CACHE[sym] = (now, ERROR_TTL, None)
        return None

    _record_success()
    with _LOCK:
        _CACHE[sym] = (now, QUOTE_TTL, dict(quote) if quote else None)
    return quote


def get_quotes(tickers, max_workers: int = WORKERS, fetch=None) -> dict[str, dict]:
    """여러 미국 종목의 시간외 포함 시세를 병렬로 모은다(실패 종목은 키 없음)."""
    if not is_enabled():
        return {}
    want: list[str] = []
    for raw in tickers or []:
        sym = str(raw or "").strip().upper()
        if sym and sym not in want:
            want.append(sym)
    if not want:
        return {}

    # 캐시에 있는 것은 네트워크 없이 바로 채운다(같은 요청 안의 중복·직전 요청 재사용).
    out: dict[str, dict] = {}
    pending: list[str] = []
    now = time.time()
    with _LOCK:
        for sym in want:
            cached = _CACHE.get(sym)
            if cached and now - cached[0] < cached[1]:
                if cached[2]:
                    out[sym] = dict(cached[2])
            else:
                pending.append(sym)
    if not pending:
        return out

    workers = max(1, min(int(max_workers or 1), len(pending)))
    if workers == 1:
        quotes = [get_quote(sym, fetch=fetch) for sym in pending]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extquote") as pool:
            quotes = list(pool.map(lambda s: get_quote(s, fetch=fetch), pending))
    for sym, quote in zip(pending, quotes):
        if quote:
            out[sym] = quote
    return out
