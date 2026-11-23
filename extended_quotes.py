"""미국 주식 시간외(프리·애프터마켓) 시세 — 네이버 해외주식 → Yahoo chart 메타.

왜 필요한가
-----------
Finnhub `/quote`의 `c`는 **정규장 체결가**다. 한국 낮(미국 프리마켓·애프터마켓·주말)에는
정규장이 닫혀 있어 Finnhub가 직전 정규장 종가만 돌려주고, 토스증권처럼 시간외 마지막
체결가를 보여주는 앱과 값이 어긋난다(예: SKHY 103.57 vs 106.30, MNST 43.69 vs 43.27).

두 소스를 순서대로 본다
-----------------------
1. **네이버 해외주식** `polling.finance.naver.com/api/realtime/worldstock/stock/{code}`
   — `overMarketPriceInfo.overPrice`가 프리·애프터마켓 마지막 체결가다(명시 필드). 국내
   증권사 화면과 기준이 같다. 코드는 나스닥이 `AAPL.O`, 뉴욕이 `MLM`(접미사 없음)이라
   한 번 확인해 기억한다(`_CODE_CACHE`).
2. **Yahoo v8 chart 메타** `range=1d&includePrePost=true`
   — `meta.chartPreviousClose × (1 + meta.fulldayChangePercent/100)`이 시간외 마지막
   체결가다. 1.3KB·0.08초로 가볍고 네이버에 없는 종목(ADR·ETF 등)을 덮는다.
   `meta.fulldayPrice`는 값이 어긋나서 쓰지 않는다.
두 경로 모두 실측에서 같은 값이 나왔다(2026-11-20 애프터마켓 종가 12종목 일치).

한계
----
토스증권의 '데이마켓'(블루오션 ATS, 한국 낮 시간) 시세는 두 소스에 없다. 이 모듈이 맞출 수
있는 것은 **프리마켓·애프터마켓 마지막 체결가**까지다.

속도·안전
---------
- 종목당 30초 캐시, 소스별 3초 타임아웃·최대 8개 병렬.
- 소스마다 따로 3회 연속 실패하면 그 소스만 10분 쉰다(데이터센터 IP 차단 시 지연 방지).
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

SOURCE_NAVER_PREPOST = "naver_prepost"   # 네이버 해외주식 시간외 체결가
SOURCE_NAVER = "naver_quote"            # 네이버 정규장가(시간외 정보가 없을 때)
SOURCE_YAHOO_PREPOST = "yahoo_prepost"  # Yahoo 시간외 체결가
SOURCE_YAHOO = "yahoo_quote"            # Yahoo 정규장가

NAVER_URL = "https://polling.finance.naver.com/api/realtime/worldstock/stock/{code}"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

REQUEST_TIMEOUT = 3.0     # 초 — 스크리닝 응답을 붙잡지 않는다
QUOTE_TTL = 30            # 시간외 체결은 분 단위로도 잘 바뀌지 않는다
ERROR_TTL = 90
CODE_TTL = 7 * 24 * 3600  # 종목 → 네이버 코드(거래소 접미사)는 거의 바뀌지 않는다
CODE_MISS_TTL = 3600      # 네이버에 없는 종목은 1시간만 기억하고 다시 본다
WORKERS = 8
FAIL_THRESHOLD = 3        # 소스별 연속 실패 → 그 소스만 휴식
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
# 티커 -> (저장 시각, 네이버 코드 | None)
_CODE_CACHE: dict[str, tuple[float, str | None]] = {}
_LOCK = threading.Lock()


class _Breaker:
    """소스 하나의 연속 실패·휴식 상태(락은 모듈 _LOCK을 쓴다)."""

    def __init__(self, name: str):
        self.name = name
        self.fail_streak = 0
        self.until = 0.0
        self.last_error = ""

    def blocked(self, now: float) -> bool:
        return now < self.until

    def ok(self) -> None:
        self.fail_streak = 0
        self.last_error = ""

    def fail(self, message: str) -> None:
        self.fail_streak += 1
        self.last_error = str(message or "")[:160]
        if self.fail_streak >= FAIL_THRESHOLD:
            self.until = time.time() + COOLDOWN_SECONDS
            self.last_error = f"{self.last_error} (연속 {self.fail_streak}회 실패 → {COOLDOWN_SECONDS}초 휴식)"

    def status(self, now: float) -> dict:
        return {"blocked": now < self.until,
                "remaining_sec": max(0, int(self.until - now)),
                "fail_streak": self.fail_streak,
                "last_error": self.last_error}


_NAVER = _Breaker("naver")
_YAHOO = _Breaker("yahoo")


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


def status() -> dict:
    """`GET /` 진단용 — 설정·소스별 차단 상태(가격 값은 노출하지 않는다)."""
    now = time.time()
    with _LOCK:
        return {"enabled": is_enabled(), "ttl_sec": QUOTE_TTL,
                "naver": _NAVER.status(now), "yahoo": _YAHOO.status(now)}


def clear_cache() -> None:
    """테스트·수동 초기화용(차단 상태·코드 기억까지 지운다)."""
    with _LOCK:
        _CACHE.clear()
        _CODE_CACHE.clear()
        for breaker in (_NAVER, _YAHOO):
            breaker.fail_streak = 0
            breaker.until = 0.0
            breaker.last_error = ""


# --- 파싱 -------------------------------------------------------------------

def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(str(value).replace(",", "")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_naver_quote(payload) -> dict | None:
    """네이버 해외주식 polling 응답 → 시세 dict. 가격이 없으면 None.

    `closePrice`가 정규장가, `overMarketPriceInfo.overPrice`가 프리·애프터마켓 마지막
    체결가다. 둘이 같으면 정규장가로 본다(시간외 체결이 없었다는 뜻).
    """
    if not isinstance(payload, dict):
        return None
    datas = payload.get("datas") or []
    if not datas or not isinstance(datas[0], dict):
        return None
    item = datas[0]
    close = _num(item.get("closePrice"))
    if close is None or close <= 0:
        return None
    over = item.get("overMarketPriceInfo") or {}
    over_price = _num(over.get("overPrice"))
    session_type = str(over.get("tradingSessionType") or "").strip().upper()
    price, session = close, "regular"
    if (over_price and over_price > 0 and session_type
            and abs(over_price - close) > 0.004):
        price, session = over_price, "extended"
    return {
        "price": price,
        "source": SOURCE_NAVER_PREPOST if session == "extended" else SOURCE_NAVER,
        "session": session,
        "regular_price": close,
        "session_type": session_type or None,
        "market_status": str(item.get("marketStatus") or "") or None,
        "as_of": str(over.get("localTradedAt") or item.get("localTradedAt") or _now_iso()),
        "currency": "USD",
    }


def parse_chart_meta(payload) -> dict | None:
    """Yahoo v8 chart 응답 → 시세 dict. 가격이 없으면 None.

    - `session="extended"`면 `price`가 프리·애프터마켓 마지막 체결가다.
    - `session="regular"`이면 정규장가 — 시간외 체결이 없거나 같을 때.
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

    price, session = regular, "regular"
    change_pct = _num(meta.get("fulldayChangePercent"))
    previous = _num(meta.get("chartPreviousClose")) or _num(meta.get("previousClose"))
    if previous and previous > 0 and change_pct is not None and meta.get("hasPrePostMarketData") is not False:
        extended = round(previous * (1.0 + change_pct / 100.0), 2)
        # 말이 안 되는 값(±50% 이상 괴리)은 버린다 — 메타가 다른 날짜와 섞인 경우 방어.
        if extended > 0 and 0.5 <= extended / regular <= 1.5 and abs(extended - regular) > 0.004:
            price, session = extended, "extended"

    return {
        "price": price,
        "source": SOURCE_YAHOO_PREPOST if session == "extended" else SOURCE_YAHOO,
        "session": session,
        "regular_price": regular,
        "change_pct": change_pct,
        "as_of": _now_iso(),
        "currency": meta.get("currency") or "USD",
    }


# --- HTTP -------------------------------------------------------------------

def _http_json(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.json()


def _fetch_naver(code: str) -> dict:
    return _http_json(NAVER_URL.format(code=code))


def _fetch_yahoo(symbol: str) -> dict:
    return _http_json(CHART_URL.format(symbol=symbol),
                      {"range": "1d", "interval": "1d", "includePrePost": "true"})


# --- 조회 -------------------------------------------------------------------

def _naver_code(ticker: str) -> tuple[bool, str | None]:
    """(기억했는지, 네이버 코드). 기억해 둔 코드가 None이면 네이버에 없는 종목이다."""
    with _LOCK:
        cached = _CODE_CACHE.get(ticker)
    if cached and time.time() - cached[0] < (CODE_TTL if cached[1] else CODE_MISS_TTL):
        return True, cached[1]
    return False, None


def _remember_code(ticker: str, code: str | None) -> None:
    with _LOCK:
        _CODE_CACHE[ticker] = (time.time(), code)


def _naver_quote(ticker: str, fetch_naver=None) -> dict | None:
    """네이버 해외주식에서 시간외 포함 시세. 코드 접미사는 한 번 확인해 기억한다.

    나스닥은 `AAPL.O`, 뉴욕은 `MLM`처럼 접미사가 없다. 그래서 `.O`를 먼저 보고, 비어 있으면
    접미사 없이 한 번 더 본다(첫 조회만 2회, 이후에는 기억한 코드 1회). 네이버에 아예 없는
    종목은 1시간 기억해 두고 Yahoo로 넘어간다.
    """
    now = time.time()
    with _LOCK:
        if _NAVER.blocked(now):
            return None
    fetch = fetch_naver or _fetch_naver
    known, remembered = _naver_code(ticker)
    if known:
        candidates = [remembered] if remembered else []
    else:
        candidates = [f"{ticker}.O", ticker]
    for code in candidates:
        try:
            quote = parse_naver_quote(fetch(code))
        except Exception as exc:  # 타임아웃·HTTP·JSON 오류
            detail = f"{ticker}({code}) {type(exc).__name__}: {exc}"
            logger.warning("네이버 시간외 시세 실패 → %s", detail[:160])
            with _LOCK:
                _NAVER.fail(detail)
            return None
        with _LOCK:
            _NAVER.ok()
        if quote:
            _remember_code(ticker, code)
            return quote
    if candidates:   # 조회는 됐는데 데이터가 없다 → 네이버에 없는 종목
        _remember_code(ticker, None)
    return None


def _yahoo_quote(ticker: str, fetch_yahoo=None) -> dict | None:
    now = time.time()
    with _LOCK:
        if _YAHOO.blocked(now):
            return None
    try:
        quote = parse_chart_meta((fetch_yahoo or _fetch_yahoo)(ticker))
    except Exception as exc:
        detail = f"{ticker} {type(exc).__name__}: {exc}"
        logger.warning("Yahoo 시간외 시세 실패 → %s", detail[:160])
        with _LOCK:
            _YAHOO.fail(detail)
        return None
    with _LOCK:
        _YAHOO.ok()
    return quote


def get_quote(ticker: str, fetch_naver=None, fetch_yahoo=None) -> dict | None:
    """한 종목의 시간외 포함 시세(네이버 → Yahoo). 30초 캐시, 실패는 90초만 기억."""
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(sym)
    if cached and now - cached[0] < cached[1]:
        return dict(cached[2]) if cached[2] else None

    quote = _naver_quote(sym, fetch_naver=fetch_naver) or _yahoo_quote(sym, fetch_yahoo=fetch_yahoo)
    with _LOCK:
        _CACHE[sym] = (now, QUOTE_TTL if quote else ERROR_TTL, dict(quote) if quote else None)
    return quote


def get_quotes(tickers, max_workers: int = WORKERS, fetch_naver=None, fetch_yahoo=None) -> dict[str, dict]:
    """여러 미국 종목의 시간외 포함 시세를 병렬로 모은다(실패 종목은 키 없음).

    캐시에 있는 종목은 네트워크 없이 채우고, 나머지는 병렬로 조회한다.
    """
    if not is_enabled():
        return {}
    want: list[str] = []
    for raw in tickers or []:
        sym = str(raw or "").strip().upper()
        if sym and sym not in want:
            want.append(sym)
    if not want:
        return {}

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
        quotes = [get_quote(sym, fetch_naver=fetch_naver, fetch_yahoo=fetch_yahoo) for sym in pending]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extquote") as pool:
            quotes = list(pool.map(
                lambda s: get_quote(s, fetch_naver=fetch_naver, fetch_yahoo=fetch_yahoo), pending))
    for sym, quote in zip(pending, quotes):
        if quote:
            out[sym] = quote
    return out
