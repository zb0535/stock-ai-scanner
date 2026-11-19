"""SEC EDGAR 무료 재무 폴백.

Yahoo Finance `t.info`가 서버 IP 차단(429/401)으로 막혀도 재무 지표를 채울 수 있도록
미국 증권거래위원회(SEC) XBRL companyfacts API에서 직접 값을 계산한다.
API 키가 필요 없고, SEC가 요구하는 형식의 User-Agent만 있으면 된다.

    https://www.sec.gov/files/company_tickers.json          (티커 -> CIK, 24시간 캐시)
    https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json  (공시 재무 원본, 6시간 캐시)

원본 companyfacts는 회사 하나가 수~수십 MB라, 메모리가 작은 Render 무료 인스턴스를
위해 원본은 LRU(기본 3개)로만 들고 있고 실제 캐시는 추출한 지표(6시간)로 한다.

XBRL 원본은 "기간(start~end)" 단위 값 목록이라 TTM(최근 12개월)을 직접 만들어야 한다.
- 3개월(70~100일) 값 4개가 끊김 없이 이어지면 그 합을 TTM으로 쓴다.
- 아니면 최근 누적값(분기/반기/3분기 누적)에 직전 회계연도 연간값을 더하고
  전년 동기 누적값을 빼는 표준 TTM 공식을 쓴다.
- 최신 값이 연간(10-K)이면 그 값이 곧 TTM이다.
외국 ADR(예: TSM)처럼 US-GAAP 태그가 없는 회사는 조용히 None을 돌려준다.
"""
import logging
import math
import threading
import time
from datetime import date

import requests

logger = logging.getLogger("sec_edgar")

# SEC는 User-Agent에 앱 이름과 연락처를 넣도록 요구한다. 비우거나 브라우저를
# 흉내 내면 403으로 막힌다.
USER_AGENT = "StockAIScanner/1.0 (gusdn0535@jnu.ac.kr)"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

REQUEST_TIMEOUT = 20          # 초
MIN_REQUEST_INTERVAL = 0.12   # SEC 권장 한도(초당 10건) 아래로 묶는다 → 최대 약 8건/초
TICKERS_TTL = 24 * 3600       # 티커 목록은 하루에 한 번만 받는다
FACTS_TTL = 6 * 3600          # 기업 재무 원본은 공시 때만 바뀐다
FACTS_ERROR_TTL = 3600        # 실패(404/빈 응답)는 1시간 동안 기억해 재요청 폭주를 막는다

# --- 캐시 -------------------------------------------------------------------
_TICKERS: dict[str, str] = {}
_TICKERS_AT = 0.0
_TICKERS_LOCK = threading.Lock()

# cik10 -> (저장 시각, 유지 시간, facts dict 또는 None)
# companyfacts 원본은 회사 하나가 4~30MB라 15종목을 다 들고 있으면 Render 무료
# 인스턴스(512MB)가 터진다. 원본은 LRU로 몇 개만 남기고, 실제로 오래 캐시하는 것은
# 뽑아낸 지표(수백 바이트)다.
_FACTS_CACHE: dict[str, tuple[float, float, dict | None]] = {}
_FACTS_LOCK = threading.Lock()
MAX_FACTS_CACHE = 3

# cik10 -> (저장 시각, 유지 시간, extract_metrics 결과)
_RAW_METRICS_CACHE: dict[str, tuple[float, float, dict]] = {}
_RAW_METRICS_LOCK = threading.Lock()

# companyfacts는 한 건이 수~수십 MB다. 6개 종목을 동시에 파싱하면 메모리 피크가 커지므로
# 동시에 처리하는 기업 수를 제한한다(스크리닝 속도는 유지).
SEC_MAX_CONCURRENCY = 3
_SEC_SLOTS = threading.BoundedSemaphore(SEC_MAX_CONCURRENCY)

_THROTTLE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0

# --- XBRL 태그 묶음 (앞에 있을수록 우선) ------------------------------------
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
EQUITY_TAGS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
LONG_TERM_DEBT_TAGS = ("LongTermDebtNoncurrent", "LongTermDebt")
CURRENT_DEBT_TAGS = ("LongTermDebtCurrent", "DebtCurrent")
SHORT_TERM_DEBT_TAGS = ("ShortTermBorrowings", "CommercialPaper")


# --- HTTP -------------------------------------------------------------------

def _throttle() -> None:
    """모든 스레드가 같은 간격으로 요청을 시작하도록 직렬화한다(초당 10건 제한 준수)."""
    global _LAST_REQUEST_AT
    with _THROTTLE_LOCK:
        wait = MIN_REQUEST_INTERVAL - (time.time() - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT = time.time()


def _http_get_json(url: str) -> dict:
    """SEC JSON을 받아온다. 테스트에서는 이 함수만 바꿔 끼우면 네트워크가 필요 없다."""
    _throttle()
    resp = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# --- 티커 -> CIK -------------------------------------------------------------

def _load_tickers() -> dict[str, str]:
    """company_tickers.json을 받아 티커 -> 10자리 CIK 맵을 만든다(24시간 캐시)."""
    global _TICKERS, _TICKERS_AT
    now = time.time()
    with _TICKERS_LOCK:
        if _TICKERS and now - _TICKERS_AT < TICKERS_TTL:
            return _TICKERS

    try:
        raw = _http_get_json(COMPANY_TICKERS_URL)
    except Exception as exc:
        logger.warning("SEC 티커 목록 조회 실패 → %s: %s", type(exc).__name__, str(exc)[:200])
        with _TICKERS_LOCK:
            return _TICKERS  # 만료됐더라도 직전 성공값이 있으면 그대로 쓴다

    mapping: dict[str, str] = {}
    for item in (raw or {}).values():
        if not isinstance(item, dict):
            continue
        sym = str(item.get("ticker") or "").strip().upper()
        cik = item.get("cik_str")
        if not sym or cik is None:
            continue
        try:
            cik10 = f"{int(cik):010d}"
        except (TypeError, ValueError):
            continue
        mapping[sym] = cik10
        mapping[sym.replace(".", "-")] = cik10  # BRK.B -> BRK-B (yfinance 표기)

    with _TICKERS_LOCK:
        _TICKERS = mapping
        _TICKERS_AT = now
    logger.info("SEC 티커 목록 갱신: %d개", len(mapping))
    return mapping


def get_cik(ticker: str) -> str | None:
    """티커를 10자리 0-패딩 CIK로 바꾼다. 없으면 None."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    tickers = _load_tickers()
    return tickers.get(sym) or tickers.get(sym.replace(".", "-"))


# --- companyfacts -----------------------------------------------------------

def get_company_facts(cik10: str) -> dict | None:
    """기업 XBRL 원본을 6시간 캐시로 돌려준다. 실패는 1시간 기억한다."""
    cik = str(cik10 or "").strip()
    if not cik:
        return None
    now = time.time()
    with _FACTS_LOCK:
        cached = _FACTS_CACHE.get(cik)
        if cached and now - cached[0] < cached[1]:
            return cached[2]

    url = COMPANY_FACTS_URL.format(cik=cik)
    try:
        facts = _http_get_json(url)
    except Exception as exc:
        logger.warning("SEC companyfacts 실패(CIK %s) → %s: %s", cik, type(exc).__name__, str(exc)[:200])
        with _FACTS_LOCK:
            _FACTS_CACHE[cik] = (now, FACTS_ERROR_TTL, None)
        return None

    if not isinstance(facts, dict) or not facts.get("facts"):
        with _FACTS_LOCK:
            _FACTS_CACHE[cik] = (now, FACTS_ERROR_TTL, None)
        return None

    with _FACTS_LOCK:
        _FACTS_CACHE.pop(cik, None)          # LRU: 방금 쓴 항목을 맨 뒤로
        _FACTS_CACHE[cik] = (now, FACTS_TTL, facts)
        # 메모리 보호: 가장 오래전에 쓰인 원본부터 버린다.
        while len(_FACTS_CACHE) > MAX_FACTS_CACHE:
            oldest = next(iter(_FACTS_CACHE))
            if oldest == cik:
                break
            _FACTS_CACHE.pop(oldest, None)
    return facts


def _get_raw_metrics(cik: str) -> dict | None:
    """기업 지표 계산 결과를 6시간 캐시로 돌려준다(원본 facts를 다시 받지 않게)."""
    now = time.time()
    with _RAW_METRICS_LOCK:
        cached = _RAW_METRICS_CACHE.get(cik)
        if cached and now - cached[0] < cached[1]:
            return cached[2]

    # 다운로드·JSON 파싱·지표 계산까지 동시 3건으로 묶아 메모리 피크를 잡는다.
    with _SEC_SLOTS:
        facts = get_company_facts(cik)
        if not facts:
            return None
        raw = extract_metrics(facts)
    # 모든 값이 비었으면(외국 ADR 등) 1시간만 기억해 다음 공시를 놓치지 않게 한다.
    ttl = FACTS_TTL if any(v is not None for v in raw.values()) else FACTS_ERROR_TTL
    with _RAW_METRICS_LOCK:
        _RAW_METRICS_CACHE[cik] = (now, ttl, raw)
    return raw


# --- XBRL 파싱 도우미 --------------------------------------------------------

def _entries(facts: dict, tag: str, unit: str = "USD") -> list[dict]:
    """us-gaap 태그의 값 목록. 요청한 단위가 없으면 첫 번째 단위를 쓴다."""
    node = ((facts.get("facts") or {}).get("us-gaap") or {}).get(tag) or {}
    units = node.get("units") or {}
    arr = units.get(unit)
    if arr is None and units:
        arr = next(iter(units.values()))
    return [e for e in (arr or []) if isinstance(e, dict)]


def _series_end(entries: list[dict]) -> str:
    ends = [e.get("end") for e in entries if e.get("end")]
    return max(ends) if ends else ""


def _pick_tag(facts: dict, tags, unit: str = "USD") -> tuple[str | None, list[dict]]:
    """태그 후보 중 '가장 최근 공시까지 이어지는' 값을 고른다.

    회사마다 쓰는 태그가 바뀌기도 한다(예: NVDA는 최근 매출을 Revenues로만 공시).
    단순히 첫 번째 태그를 쓰면 몇 년 전에 끊긴 태그를 골라 최신 태그와 기간이
    어긋나므로, 시계열의 마지막 기간이 가장 최근인 태그를 쓴다. 시점이 같으면
    목록 앞쪽 태그(우선순위)를 쓴다.
    """
    best_key = None
    best_tag, best_arr = None, []
    for idx, tag in enumerate(tags):
        arr = _entries(facts, tag, unit)
        if not arr:
            continue
        key = (_series_end(arr), -idx)
        if best_key is None or key > best_key:
            best_key, best_tag, best_arr = key, tag, arr
    return best_tag, best_arr


def _to_float(value) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _duration_days(entry: dict) -> int | None:
    start, end = entry.get("start"), entry.get("end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def _days_between(a: str, b: str) -> int | None:
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (TypeError, ValueError):
        return None


def _dedupe(entries: list[dict]) -> list[dict]:
    """(시작, 끝)이 같은 값이 여러 공시에 중복되면 가장 최근 제출본만 남긴다."""
    best: dict[tuple[str, str], dict] = {}
    for e in entries or []:
        start, end = e.get("start"), e.get("end")
        if not start or not end or _to_float(e.get("val")) is None:
            continue
        key = (start, end)
        prev = best.get(key)
        if prev is None or str(e.get("filed") or "") >= str(prev.get("filed") or ""):
            best[key] = e
    return sorted(best.values(), key=lambda x: x["end"])


def _instant(entries: list[dict]) -> tuple[float | None, str | None]:
    """재무상태표처럼 시점 값(start 없음)인 항목 중 가장 최근 값."""
    points = [
        (e["end"], _to_float(e.get("val")), str(e.get("filed") or ""))
        for e in (entries or [])
        if e.get("end") and not e.get("start") and _to_float(e.get("val")) is not None
    ]
    if not points:
        return None, None
    # 같은 시점이 여러 공시에 있으면 최근 제출본, 시점은 가장 최근 것
    points.sort(key=lambda p: (p[0], p[2]))
    return points[-1][1], points[-1][0]


def _sum_contiguous_quarters(entries: list[dict]) -> float | None:
    """3개월 값 4개가 12개월을 끊김 없이 덮으면 합을 돌려준다(가장 정확한 TTM 방식)."""
    quarters = [e for e in entries if (d := _duration_days(e)) is not None and 70 <= d <= 100]
    if len(quarters) < 4:
        return None
    tail = quarters[-4:]
    for prev, cur in zip(tail, tail[1:]):
        gap = _days_between(prev["end"], cur["start"])
        if gap is None or not -5 <= gap <= 7:
            return None
    span = _days_between(tail[0]["start"], tail[-1]["end"])
    if span is None or not 300 <= span <= 400:
        return None
    total = 0.0
    for e in tail:
        total += _to_float(e["val"]) or 0.0
    return total


def _annual_ending_at(entries: list[dict], start_date: str) -> dict | None:
    """`start_date` 직전에 끝난 연간(10-K, 300~400일) 값."""
    best = None
    for e in entries:
        d = _duration_days(e)
        if d is None or not 300 <= d <= 400:
            continue
        gap = _days_between(e["end"], start_date)   # 연간 끝 -> 누적 시작
        if gap is None or not -10 <= gap <= 20:
            continue
        if best is None or e["end"] > best["end"]:
            best = e
    return best


def _same_span_last_year(entries: list[dict], latest: dict) -> dict | None:
    """전년 동기 누적값(같은 길이의 기간, 1년 전 종료)."""
    d0 = _duration_days(latest)
    if d0 is None:
        return None
    for e in reversed(entries):
        d = _duration_days(e)
        if d is None or abs(d - d0) > 15:
            continue
        gap = _days_between(e["end"], latest["end"])
        if gap is not None and 340 <= gap <= 390:
            return e
    return None


def _latest_period(entries: list[dict]) -> dict | None:
    """가장 최근에 끝난 보고 기간. 같은 날 끝나는 값이 여럿이면 가장 긴 기간(누적)을 쓴다.

    한 10-Q에는 3개월 단독과 6개월 누적이 같은 날 끝나므로, 단독 분기를 고르면
    TTM 보정에 필요한 누적값을 놓친다.
    """
    if not entries:
        return None
    max_end = _series_end(entries)
    same_end = [e for e in entries if e.get("end") == max_end]
    return max(same_end, key=lambda e: _duration_days(e) or -1)


def _ttm_from_cumulative(entries: list[dict]) -> float | None:
    """최근 누적값 + 직전 회계연도 - 전년 동기 누적값 = TTM."""
    if not entries:
        return None
    latest = _latest_period(entries)
    if latest is None or not latest.get("start"):
        return None
    d = _duration_days(latest)
    if d is None:
        return None
    if d >= 300:                      # 최신 공시가 연간(10-K)이면 그것이 TTM
        return _to_float(latest["val"])

    fy_prev = _annual_ending_at(entries, latest["start"])
    ytd_prev = _same_span_last_year(entries, latest)
    latest_val = _to_float(latest["val"])
    if latest_val is None:
        return None
    if fy_prev is not None and ytd_prev is not None:
        fy_val = _to_float(fy_prev["val"])
        ytd_val = _to_float(ytd_prev["val"])
        if fy_val is not None and ytd_val is not None:
            return fy_val + latest_val - ytd_val

    # 보정에 필요한 전년 값이 없으면 최근 연간값으로 대신한다(근사치).
    annuals = [e for e in entries if (x := _duration_days(e)) is not None and 300 <= x <= 400]
    if annuals:
        return _to_float(annuals[-1]["val"])
    return None


def _ttm(entries: list[dict]) -> float | None:
    """최근 12개월(TTM) 값. 계산이 불가능하면 None."""
    cleaned = _dedupe(entries)
    if not cleaned:
        return None
    value = _sum_contiguous_quarters(cleaned)
    if value is not None:
        return value
    return _ttm_from_cumulative(cleaned)


def _yoy_growth(entries: list[dict]) -> float | None:
    """매출 성장률(YoY). 최근 분기를 전년 동기 분기와 비교하고, 없으면 연간끼리 비교한다."""
    cleaned = _dedupe(entries)
    if not cleaned:
        return None

    quarters = [e for e in cleaned if (d := _duration_days(e)) is not None and 70 <= d <= 100]
    if quarters:
        latest = quarters[-1]
        prior = _same_span_last_year(quarters, latest)
        latest_val = _to_float(latest["val"])
        prior_val = _to_float(prior["val"]) if prior else None
        if latest_val is not None and prior_val:
            return latest_val / prior_val - 1

    annuals = [e for e in cleaned if (d := _duration_days(e)) is not None and 300 <= d <= 400]
    if len(annuals) >= 2:
        latest, prior = annuals[-1], annuals[-2]
        gap = _days_between(prior["end"], latest["end"])
        latest_val, prior_val = _to_float(latest["val"]), _to_float(prior["val"])
        if gap is not None and 340 <= gap <= 390 and latest_val is not None and prior_val:
            return latest_val / prior_val - 1
    return None


def _latest_annual(entries: list[dict]) -> dict | None:
    annuals = [e for e in entries if (d := _duration_days(e)) is not None and 300 <= d <= 400]
    return annuals[-1] if annuals else None


def _operating_margin(facts: dict) -> float | None:
    _, op_entries = _pick_tag(facts, OPERATING_INCOME_TAGS)
    _, rev_entries = _pick_tag(facts, REVENUE_TAGS)
    if not op_entries or not rev_entries:
        return None
    # 두 시계열의 기준 기간이 크게 어긋나면(예: 한쪽은 반기, 다른 쪽은 분기) 이익률이
    # 엉뚱한 값이 되므로 계산하지 않는다.
    op_end, rev_end = _series_end(op_entries), _series_end(rev_entries)
    gap = _days_between(op_end, rev_end)
    if gap is None or abs(gap) > 45:
        return None
    op_ttm = _ttm(op_entries)
    rev_ttm = _ttm(rev_entries)
    if op_ttm is None or not rev_ttm:
        return None
    return op_ttm / rev_ttm


def _debt_to_equity(facts: dict) -> float | None:
    _, equity_entries = _pick_tag(facts, EQUITY_TAGS)
    equity, _ = _instant(equity_entries)
    if equity is None or equity <= 0:
        return None

    _, noncurrent_entries = _pick_tag(facts, LONG_TERM_DEBT_TAGS)
    noncurrent, _ = _instant(noncurrent_entries)

    # DebtCurrent가 있으면 그것이 유동성 장기부채 + 단기차입금을 모두 포함하는 경우가
    # 많아 단기차입금을 따로 더하지 않는다(이중 계상 방지).
    _, current_entries = _pick_tag(facts, ("DebtCurrent",))
    current, _ = _instant(current_entries)
    short_term = 0.0
    if current is None:
        _, current_entries = _pick_tag(facts, ("LongTermDebtCurrent",))
        current, _ = _instant(current_entries)
        _, st_entries = _pick_tag(facts, SHORT_TERM_DEBT_TAGS)
        short_term, _ = _instant(st_entries)
        short_term = short_term or 0.0

    if noncurrent is None and current is None and not short_term:
        return None
    debt = (noncurrent or 0.0) + (current or 0.0) + short_term
    if debt <= 0:
        return None
    return debt / equity * 100


def extract_metrics(facts: dict) -> dict:
    """companyfacts dict에서 재무 지표를 계산한다(가격이 필요한 PER은 제외).

    어떤 항목이 없어도 예외를 던지지 않고 None으로 둔다.
    """
    out = {
        "eps_ttm": None,
        "revenue_ttm": None,
        "revenue_growth": None,
        "operating_margins": None,
        "debt_to_equity": None,
    }
    if not isinstance(facts, dict) or not facts.get("facts"):
        return out

    try:
        _, eps_entries = _pick_tag(facts, EPS_TAGS, unit="USD/shares")
        out["eps_ttm"] = _ttm(eps_entries)
    except Exception as exc:  # 파싱 실패가 종목 전체를 망가뜨리지 않게
        logger.warning("SEC EPS 파싱 실패 → %s", exc)

    try:
        _, rev_entries = _pick_tag(facts, REVENUE_TAGS)
        out["revenue_ttm"] = _ttm(rev_entries)
        out["revenue_growth"] = _yoy_growth(rev_entries)
    except Exception as exc:
        logger.warning("SEC 매출 파싱 실패 → %s", exc)

    try:
        out["operating_margins"] = _operating_margin(facts)
    except Exception as exc:
        logger.warning("SEC 영업이익률 파싱 실패 → %s", exc)

    try:
        out["debt_to_equity"] = _debt_to_equity(facts)
    except Exception as exc:
        logger.warning("SEC 부채비율 파싱 실패 → %s", exc)

    return out


# --- 공개 API ---------------------------------------------------------------

def get_financial_metrics(ticker: str, current_price=None) -> dict:
    """yfinance `t.info`를 대신할 재무 지표를 SEC 공시에서 계산한다.

    반환 키는 stock_tools가 쓰는 것과 같다(trailing_pe, revenue_growth,
    operating_margins, debt_to_equity). 값이 없는 항목은 None이다.
    실패해도 예외를 던지지 않고 빈 dict를 돌려준다.
    """
    empty = {"trailing_pe": None, "revenue_growth": None,
             "operating_margins": None, "debt_to_equity": None}
    sym = (ticker or "").strip().upper()
    if not sym:
        return empty

    try:
        cik = get_cik(sym)
        if not cik:
            return empty
        raw = _get_raw_metrics(cik)
        if not raw:
            return empty
    except Exception as exc:
        logger.warning("%s: SEC 재무 계산 실패 → %s: %s", sym, type(exc).__name__, str(exc)[:200])
        return empty

    price = _to_float(current_price)
    eps = raw.get("eps_ttm")
    trailing_pe = None
    if price and price > 0 and eps and eps > 0:
        trailing_pe = round(price / eps, 2)

    return {
        "trailing_pe": trailing_pe,
        "revenue_growth": raw.get("revenue_growth"),
        "operating_margins": raw.get("operating_margins"),
        "debt_to_equity": raw.get("debt_to_equity"),
    }


def clear_cache() -> None:
    """테스트용 캐시 초기화."""
    global _TICKERS, _TICKERS_AT, _LAST_REQUEST_AT
    with _TICKERS_LOCK:
        _TICKERS = {}
        _TICKERS_AT = 0.0
    with _FACTS_LOCK:
        _FACTS_CACHE.clear()
    with _RAW_METRICS_LOCK:
        _RAW_METRICS_CACHE.clear()
    with _THROTTLE_LOCK:
        _LAST_REQUEST_AT = 0.0
