"""Finnhub — 3순위 재무 폴백 + 미국 주식 실시간 현재가(/quote).

Yahoo Finance `t.info`가 서버 IP 차단(429/401)으로 막히고, SEC EDGAR 공시(XBRL)에도
값이 없을 때(예: us-gaap 태그가 없는 외국 ADR TSM) Finnhub의 `stock/metric` API로
핵심 재무 지표를 채운다. API 키는 Render 환경변수 `FINNHUB_API_KEY`에서 읽는다.

    https://finnhub.io/api/v1/stock/metric?symbol=TSM&metric=all&token={key}

Finnhub는 값을 퍼센트로 주는 항목이 있어 yfinance와 단위를 맞춰 변환한다.
- `revenueGrowthQuarterlyYoy` 30.5  → 0.305  (yfinance revenueGrowth 비율)
- `operatingMarginTTM` 47.2        → 0.472  (yfinance operatingMargins 비율)
- `totalDebt/totalEquityQuarterly` 0.246 → 24.6 (yfinance debtToEquity 퍼센트)
  Finnhub의 이 항목은 비율이다. AAPL 2024년 응답 `totalDebt/totalEquityQuarterly`가
  1.4097(≈140.97%)였고 TSMC는 0.15 안팎으로, yfinance의 퍼센트 값(140.97)과 같은
  스케일이 되려면 ×100이 필요하다.

무료 플랜은 분당 60회 제한이라, 응답은 종목별로 6시간 캐시하고 실패는 15분만
기억해 쿼터를 아낀다. 키가 없거나 `FINNHUB_FALLBACK=0`이면 조용히 비활성화된다.

실시간 현재가
-------------
`get_quote(ticker)`가 `https://finnhub.io/api/v1/quote`를 불러 `c`(현재가)를 돌려준다.
지표·재무는 기존 30분 캐시를 그대로 쓰고 **현재가만** 10초 캐시로 짧게 둔다 —
S&P 500 프리 스캔(500종목)에는 쓰지 않고, 최종 15종목·성적표에서만 호출한다.
무료 쿼터를 재무 폴백과 나눠 쓰도록 시세는 분당 50회로 자체 제한하고,
`c`가 0·None(미지원 티커·장 마감)이면 None을 돌려줘 호출부가 일봉 종가를 유지하게 한다.
"""
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger("finnhub_client")

BASE_URL = "https://finnhub.io/api/v1/stock/metric"
QUOTE_URL = "https://finnhub.io/api/v1/quote"
REQUEST_TIMEOUT = 10          # 초
QUOTE_TIMEOUT = 3             # 실시간 시세는 짧게 — 스크리닝 응답을 붙잡지 않는다

METRICS_TTL = 6 * 3600        # 성공: 재무 지표는 공시 때만 바뀐다
ERROR_TTL = 15 * 60           # 실패·빈 응답: 15분 뒤 다시 시도(쿼터 보호)

# 실시간 시세 캐시(초) — 같은 요청 안의 중복 호출만 막고, 새 조회에는 최신가를 반영한다.
QUOTE_TTL = 10
QUOTE_ERROR_TTL = 30
# 무료 플랜은 분당 60회. 재무 폴백과 쿼터를 나눠 쓰도록 시세 호출은 분당 50회로 스스로 제한한다.
QUOTE_MAX_PER_MIN = 50
QUOTE_WORKERS = 8             # 여러 종목 시세의 병렬 조회 상한

# stock_tools가 쓰는 키 이름과 같게 맞춘다.
FIELDS = ("trailing_pe", "forward_pe", "revenue_growth", "operating_margins", "debt_to_equity")

# ticker -> (저장 시각, 유지 시간, 지표 dict 또는 None)
_CACHE: dict[str, tuple[float, float, dict | None]] = {}
_CACHE_LOCK = threading.Lock()

# ticker -> (저장 시각, 유지 시간, 현재가 또는 None) — 실시간 시세 전용 캐시
_QUOTE_CACHE: dict[str, tuple[float, float, float | None]] = {}
_QUOTE_LOCK = threading.Lock()
# 분당 호출 상한 계산용 호출 시각 목록(최근 60초)
_QUOTE_CALLS: list[float] = []

# 마지막 실패 사유(운영 진단용). 키 본문은 절대 남기지 않는다.
_LAST_ERROR = ""
_LAST_ERROR_LOCK = threading.Lock()


class FinnhubHTTPError(RuntimeError):
    """HTTP 실패를 상태코드만으로 전달한다(오류 메시지에 token이 섞이는 것을 막는다)."""

    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.body = (body or "")[:80]
        super().__init__(f"HTTP {status_code}")


# --- 설정 -------------------------------------------------------------------

def get_api_key() -> str:
    """환경변수에서 Finnhub API 키를 읽는다(테스트에서 env를 바꿔 끼울 수 있게 매번 읽음).

    `FINNHUB_API_KEY`가 표준이고, 대시보드에서 이름을 달리 넣은 경우를 위해
    `FINN_API_KEY`·`FINNHUB_KEY` 같은 별칭도 받아준다. 어느 이름으로 읽혔는지는
    `status()`의 env_name에 남는다.
    """
    return _resolve_key()[1]


_KEY_ENV_NAMES = ("FINNHUB_API_KEY", "FINN_API_KEY", "FINNHUB_KEY", "FINNHUB_TOKEN", "FINNHUB_API_TOKEN")


def _resolve_key() -> tuple[str, str]:
    """(사용한 환경변수 이름, 키 값). 없으면 ("", "")."""
    for name in _KEY_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return name, value
    return "", ""


def is_enabled() -> bool:
    """키가 있고 FINNHUB_FALLBACK으로 끄지 않았을 때만 True."""
    if (os.environ.get("FINNHUB_FALLBACK") or "1").strip() in ("0", "false", "False"):
        return False
    return bool(get_api_key())


# --- HTTP -------------------------------------------------------------------

def _http_get_json(url: str, params: dict, timeout: float | None = None) -> dict:
    """Finnhub JSON을 받아온다. 테스트에서는 이 함수만 바꿔 끼우면 네트워크가 필요 없다.

    실패는 상태코드만 담은 FinnhubHTTPError로 올린다. requests의 기본 예외 메시지에는
    요청 URL이 들어 있어 token(=API 키)이 로그·응답에 노출될 수 있기 때문이다.
    """
    resp = requests.get(
        url,
        params=params,
        headers={"Accept": "application/json", "User-Agent": "StockAIScanner/1.0"},
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    if resp.status_code >= 400:
        raise FinnhubHTTPError(resp.status_code, resp.text)
    return resp.json()


def _redact(text: str) -> str:
    """오류 문자열에 섞일 수 있는 token=... 값을 지운다(로그·헬스 노출 방지)."""
    return re.sub(r"token=[^&\s\"']+", "token=***", str(text or ""))


def _set_last_error(message: str) -> None:
    global _LAST_ERROR
    with _LAST_ERROR_LOCK:
        _LAST_ERROR = _redact(message)[:160]


def status() -> dict:
    """키가 설정됐는지·마지막 호출이 실패했는지 비밀값 없이 알려준다(헬스 체크용).

    키 본문은 절대 담지 않고 길이와 설정 여부만 노출한다.
    """
    key = get_api_key()
    env_name, _ = _resolve_key()
    with _LAST_ERROR_LOCK:
        last_error = _LAST_ERROR
    return {
        "configured": bool(key),
        "env_name": env_name,
        "env_flags": {name: bool((os.environ.get(name) or "").strip()) for name in _KEY_ENV_NAMES},
        "key_len": len(key),
        "enabled": is_enabled(),
        "last_error": last_error,
    }


# --- 파싱 도우미 -------------------------------------------------------------

def _to_float(value) -> float | None:
    """문자열("30.5", "")·NaN·inf를 안전한 float 또는 None으로 바꾼다."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value or value in ("-", "N/A", "n/a", "None", "null", "nan"):
            return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _pick(metric: dict, *keys) -> float | None:
    """후보 키 중 처음으로 값이 있는 것을 쓴다(앞에 있을수록 우선)."""
    for key in keys:
        num = _to_float(metric.get(key))
        if num is not None:
            return num
    return None


def _pick_ratio(metric: dict, *keys) -> float | None:
    """Finnhub 퍼센트 값을 yfinance식 소수 비율로 바꾼다(30.5 → 0.305)."""
    num = _pick(metric, *keys)
    return None if num is None else num / 100.0


def parse_metrics(payload) -> dict:
    """Finnhub `stock/metric` 응답에서 핵심 재무 지표를 뽑는다. 없으면 None."""
    out = {key: None for key in FIELDS}
    if not isinstance(payload, dict):
        return out
    metric = payload.get("metric")
    if not isinstance(metric, dict) or not metric:
        return out

    out["trailing_pe"] = _pick(metric, "peTTM", "peAnnual")
    out["forward_pe"] = _pick(metric, "forwardPE")
    out["revenue_growth"] = _pick_ratio(metric, "revenueGrowthQuarterlyYoy", "revenueGrowthTTMYoy")
    out["operating_margins"] = _pick_ratio(metric, "operatingMarginTTM", "operatingMarginAnnual")
    # 부채비율은 Finnhub가 비율(0.246)로 주고 yfinance는 퍼센트(24.6)로 준다 → ×100.
    debt_to_equity = _pick(metric, "totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual")
    out["debt_to_equity"] = None if debt_to_equity is None else debt_to_equity * 100.0
    return out


# --- 공개 API ---------------------------------------------------------------

def get_financial_metrics(ticker: str) -> dict:
    """Finnhub에서 재무 지표를 받아온다(6시간 캐시, 실패 15분 캐시).

    반환 키는 stock_tools/SEC 폴백과 같다. 키가 없거나 실패해도 예외를 던지지 않고
    모든 값이 None인 dict를 돌려준다.
    """
    empty = {key: None for key in FIELDS}
    sym = (ticker or "").strip().upper()
    if not sym:
        return empty
    token = get_api_key()
    if not token:
        return empty

    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(sym)
        if cached and now - cached[0] < cached[1]:
            return dict(cached[2]) if cached[2] else dict(empty)

    try:
        payload = _http_get_json(
            BASE_URL,
            {"symbol": sym, "metric": "all", "token": token},
        )
        metrics = parse_metrics(payload)
    except Exception as exc:  # 429/네트워크/JSON 오류 모두 여기서 흡수
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("%s: Finnhub 조회 실패 → %s", sym, _redact(detail)[:200])
        _set_last_error(f"{sym} {detail}")
        with _CACHE_LOCK:
            _CACHE[sym] = (now, ERROR_TTL, None)
        return empty

    _set_last_error("")
    # 값이 하나도 없으면(지원하지 않는 티커 등) 15분만 기억해 다음에 다시 시도한다.
    ttl = METRICS_TTL if any(value is not None for value in metrics.values()) else ERROR_TTL
    with _CACHE_LOCK:
        _CACHE[sym] = (now, ttl, metrics)
    return dict(metrics)


def clear_cache() -> None:
    """테스트용 캐시·오류 상태 초기화."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _QUOTE_LOCK:
        _QUOTE_CACHE.clear()
        _QUOTE_CALLS.clear()
    _set_last_error("")


# --- 실시간 시세(/quote) ------------------------------------------------------
#
#   https://finnhub.io/api/v1/quote?symbol=AAPL&token={key}
#   → {"c": 195.89, "d": 1.2, "dp": 0.61, "h": 196.5, "l": 194.2, "o": 195.0, "pc": 194.7, "t": 1700000000}
#
# `c`(current price)만 쓴다. 0·null은 "시세 없음"이다 — 유효하지 않은 티커나(예: 005930.KS)
# 무료 플랜이 지원하지 않는 시장에서 Finnhub가 0을 채워 보낸다. 그때는 호출부가 일봉 종가를
# 그대로 쓰면 되므로 예외 대신 None으로 돌려준다.


def parse_quote(payload) -> float | None:
    """quote 응답 → 현재가. `c`가 유효한 양수가 아니면 None."""
    if not isinstance(payload, dict):
        return None
    price = _to_float(payload.get("c"))
    if price is None or price <= 0:
        return None
    return price


def _quote_budget_ok(now: float) -> bool:
    """분당 호출 상한 안에 있으면 호출 시각을 기록하고 True."""
    with _QUOTE_LOCK:
        cutoff = now - 60.0
        _QUOTE_CALLS[:] = [t for t in _QUOTE_CALLS if t > cutoff]
        if len(_QUOTE_CALLS) >= QUOTE_MAX_PER_MIN:
            return False
        _QUOTE_CALLS.append(now)
        return True


def get_quote(ticker: str) -> float | None:
    """미국 종목 현재가(Finnhub `/quote`). 10초 캐시, 실패는 30초만 기억.

    키가 없거나 `FINNHUB_FALLBACK=0`이면 호출 자체를 하지 않고 None.
    429·네트워크 오류·`c` 0/None 모두 조용한 None이다(예외를 밖으로 던지지 않는다).
    """
    sym = (ticker or "").strip().upper()
    token = get_api_key()
    if not sym or not token or not is_enabled():
        return None

    now = time.time()
    with _QUOTE_LOCK:
        cached = _QUOTE_CACHE.get(sym)
    if cached and now - cached[0] < cached[1]:
        return cached[2]

    if not _quote_budget_ok(now):  # 분당 상한 초과 → 일봉 종가로 폴백하게 둔다
        logger.warning("%s: Finnhub 시세 분당 상한(%d회) 도달 → 일봉 종가 사용", sym,
                       QUOTE_MAX_PER_MIN)
        return None

    try:
        payload = _http_get_json(QUOTE_URL, {"symbol": sym, "token": token},
                                 timeout=QUOTE_TIMEOUT)
        price = parse_quote(payload)
    except Exception as exc:  # 429·네트워크·JSON 오류 모두 흡수
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("%s: Finnhub 시세 조회 실패 → %s", sym, _redact(detail)[:200])
        _set_last_error(f"quote {sym} {detail}")
        with _QUOTE_LOCK:
            _QUOTE_CACHE[sym] = (now, QUOTE_ERROR_TTL, None)
        return None

    with _QUOTE_LOCK:
        _QUOTE_CACHE[sym] = (now, QUOTE_TTL if price else QUOTE_ERROR_TTL, price)
    return price


def get_quotes(tickers, max_workers: int = QUOTE_WORKERS) -> dict[str, dict]:
    """여러 미국 종목 시세를 병렬로(최대 8개) 모아 돌려준다.

    반환: `{티커: {"price": float, "source": "finnhub_quote"}}` — 실패한 종목은 키가 없다.
    호출부(실시간 오버레이)가 일봉 종가를 유지할 수 있게 실패를 빈 결과로 표현한다.
    """
    want: list[str] = []
    for raw in tickers or []:
        sym = (raw or "").strip().upper()
        if sym and sym not in want:
            want.append(sym)
    if not want or not is_enabled():
        return {}

    workers = max(1, min(int(max_workers or 1), len(want)))
    if workers == 1:
        prices = [get_quote(sym) for sym in want]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quote") as pool:
            prices = list(pool.map(get_quote, want))
    return {sym: {"price": float(price), "source": "finnhub_quote"}
            for sym, price in zip(want, prices) if price}

