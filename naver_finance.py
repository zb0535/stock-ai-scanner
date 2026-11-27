"""네이버 금융 — 한국 종목 해석(이름/코드)·재무 지표·뉴스·코스피 200 구성종목.

왜 필요한가
-----------
yfinance는 `.KS`/`.KQ` 종목의 시세·차트는 잘 주지만, Render 같은 데이터센터 IP에서는
`t.info`가 429/401로 막히거나 핵심 값이 비고(Trailing PER가 None), 한국어 종목명도 없다.
이 모듈은 네이버 금융의 공개 JSON을 써서 ① 한국어 이름·6자리 코드 → 야후 티커 해석
② PER·영업이익률·부채비율·매출증가율 보완 ③ 한국어 뉴스 ④ 코스피 200 구성종목 목록을 맡는다.

라이브 확인한 엔드포인트 (2026-11-19)
-------------------------------------
- 자동완성  `https://ac.stock.naver.com/ac?q=삼성전자&target=stock`
  → `{"items":[{"code":"005930","name":"삼성전자","typeCode":"KOSPI"}]}`
- 종목 통합  `https://m.stock.naver.com/api/stock/005930/integration`
  → `totalInfos`에 `per`(30.07배)·`cnsPer`(추정PER)·`pbr`·`eps`·`dividendYieldRatio`
- 연간 재무  `https://m.stock.naver.com/api/stock/005930/finance/annual`
  → `rowList`에 `매출액`·`영업이익률`·`부채비율`(연도별, `trTitleList.isConsensus`로 확정/추정 구분)
- 뉴스        `https://m.stock.naver.com/api/news/stock/005930?pageSize=10` (구글 뉴스 RSS 보조)
- 코스피 200  `https://m.stock.naver.com/api/index/KPI200/enrollStocks?page=1` (20개씩, page 증가)
  ⚠️ 옛 `finance.naver.com/sise/entryJongmok.naver?type=KPI200`은 410 Gone(2026-11 확인),
  `finance.naver.com/item/main.naver`는 새 앱(stock.naver.com)으로 302 리다이렉트된다.

단위 규칙
---------
- `영업이익률`은 % → `stock_tools`는 소수(0.1307)를 쓰므로 /100 한다.
- `부채비율`은 이미 % 단위 → `stock_tools`의 `debt_to_equity`(yfinance `debtToEquity`도 %)와 그대로 맞는다.
- `매출증가율`은 최근 **확정** 연도 vs 직전 확정 연도 매출액으로 계산해 소수(0.1088)로 준다.
  (추정 컨센서스 연도는 쓰지 않는다 — 아직 실적이 아니다.)

설계 원칙
---------
- 네트워크 없이 import되고, 모든 외부 호출은 짧은 타임아웃 + 예외 흡수. 실패는 `None`/빈 목록.
- 응답은 TTL 캐시(해석 24시간, 재무 6시간, 목록 6시간, 실패 15분)로 네이버 호출을 아낀다.
- 코스피 200 목록은 라이브 실패 시 **내장 201종목 폴백**으로 항상 채워진다(스캔이 비지 않게).
"""
import logging
import math
import os
import re
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

import memory_guard

logger = logging.getLogger("naver_finance")

BASE_AC = "https://ac.stock.naver.com/ac"
BASE_MOBILE = "https://m.stock.naver.com/api"
BASE_POLLING = "https://polling.finance.naver.com/api/realtime/domestic/stock"
BASE_GOOGLE_NEWS = "https://news.google.com/rss/search"
TIMEOUT = 8
HEADERS = {"User-Agent": "Mozilla/5.0 (stock-ai-scanner)"}

# 캐시 TTL(초)
RESOLVE_TTL = 24 * 3600
FUND_TTL = 6 * 3600
TABLE_TTL = 6 * 3600
FAIL_TTL = 15 * 60
NEWS_TTL = 10 * 60

# 동종업계 PER 비교(2026-11 추가)
# 종목 통합 조회 응답의 `industryCode`·`industryCompareInfo`(같은 업종 종목)를 써서
# 업종명과 **업종 기준 PER(이웃 종목 PER 중앙값)** 을 만든다. 이웃 PER은 종목별 통합 조회로
# 받아오므로 업종 단위로 6시간 캐시하고, 한 번에 최대 PEER_SAMPLE_LIMIT개만 본다.
INDUSTRY_TTL = 24 * 3600     # 업종명·업종 PER — 하루면 중앙값이 크게 달라지지 않는다
PEER_SAMPLE_LIMIT = 8        # 업종 중앙값에 넣을 이웃 종목 수 상한(호출 폭증 방지)
PEER_MIN_SAMPLES = 3         # 업종 중앙값을 인정하는 최소 표본 수
PEER_WORKERS = 6             # 이웃 PER 병렬 조회 상한(네이버에 부담을 주지 않게)
PEER_TIMEOUT = 6

# 실시간 현재가(초) — 같은 요청 안에서 중복 호출만 막을 만큼만 짧게 둔다.
REALTIME_TTL = 10            # 성공: 10초 뒤 다시 조회하면 최신 체결가가 온다
REALTIME_FAIL_TTL = 30       # 실패: 30초만 기억(장 마감·일시 장애 때 매 호출을 반복하지 않게)
REALTIME_TIMEOUT = 3         # 실시간 시세 한 번에 허용하는 대기(초) — 스크리닝을 붙잡지 않게
REALTIME_BATCH = 50          # polling은 쉼표 목록을 받는다 — 한 요청에 넣을 최대 코드 수

KOSPI200_PAGE_SIZE = 20      # enrollStocks 한 페이지 기본 개수
KOSPI200_MAX_PAGES = 15      # 20 × 15 = 300 (201종목이면 11페이지에서 끝난다)
MIN_LIVE_CONSTITUENTS = 150  # 라이브 목록이 이보다 적으면 폴백을 쓴다(부분 응답 방어)

_KR_SUFFIXES = (".KS", ".KQ")
_KOREAN_RE = re.compile(r"[가-힣]")
_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")             # 005930·0126Z0 등 6자리 신형 코드
_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}$")  # AAPL·BRK-B

# 한국 시장(KOSPI/KOSDAQ)만 매핑한다. KONEX·ETF 등은 야후 티커가 없어 제외.
_MARKET_SUFFIX = {"KOSPI": ".KS", "KOSDAQ": ".KQ"}


# --- 캐시 -------------------------------------------------------------------

_MISS = object()
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()
# 코스피 200(201종목) × 조회 종류(해석·재무·업종) 정도만 남긴다 — Render 512MB에서
# 장수 프로세스가 오래된 항목을 무한정 쥐고 있지 않게 상한을 둔다.
MAX_CACHE_ENTRIES = max(1, int(os.environ.get("NAVER_CACHE_MAX", "900")))


def _prune_locked(now: float) -> int:
    """만료된 항목을 지우고, 상한을 넘으면 가장 오래 쓰이지 않은(만료 시각이 이른) 것부터 버린다."""
    removed = 0
    for key in [k for k, item in _CACHE.items() if item[0] <= now]:
        _CACHE.pop(key, None)
        removed += 1
    extra = len(_CACHE) - MAX_CACHE_ENTRIES
    if extra > 0:
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:extra]
        for key, _item in oldest:
            _CACHE.pop(key, None)
            removed += 1
    return removed


def _cache_get(key: str):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and item[0] > time.time():
            return item[1]
    return _MISS


def _cache_put(key: str, value, ttl: float) -> None:
    now = time.time()
    with _CACHE_LOCK:
        _CACHE[key] = (now + ttl, value)
        _prune_locked(now)


def trim_caches(now: float | None = None) -> dict:
    """만료·초과 캐시 정리(`memory_guard`가 압력이 높을 때 부른다)."""
    stamp = time.time() if now is None else float(now)
    with _CACHE_LOCK:
        return {"entries": _prune_locked(stamp)}


def cache_sizes() -> dict:
    with _CACHE_LOCK:
        return {"entries": len(_CACHE), "max": MAX_CACHE_ENTRIES}


def clear_cache() -> None:
    """테스트·수동 초기화용 캐시 비우기."""
    with _CACHE_LOCK:
        _CACHE.clear()


memory_guard.register_cache("naver_finance", trim_caches, cache_sizes)


# 업종별 계산은 한 번만: 같은 업종 종목이 병렬로 들어와도 이웃 PER을 중복 조회하지 않는다.
_INDUSTRY_LOCKS: dict[str, threading.Lock] = {}
_INDUSTRY_LOCKS_GUARD = threading.Lock()


def _industry_lock(key: str) -> threading.Lock:
    with _INDUSTRY_LOCKS_GUARD:
        return _INDUSTRY_LOCKS.setdefault(key, threading.Lock())


# --- 공통 유틸 ---------------------------------------------------------------

def is_korean_ticker(ticker) -> bool:
    """`.KS`/`.KQ`로 끝나는 한국 종목인지."""
    sym = str(ticker or "").strip().upper()
    return sym.endswith(_KR_SUFFIXES)


def currency_for(ticker) -> str:
    """표시 통화 — 한국 종목은 KRW, 그 밖은 USD."""
    return "KRW" if is_korean_ticker(ticker) else "USD"


def naver_code(ticker) -> str | None:
    """`005930.KS` → `005930`. 한국 티커가 아니면 None."""
    sym = str(ticker or "").strip().upper()
    if sym.endswith(_KR_SUFFIXES):
        code = sym[:-3].strip()
        return code or None
    return None


def _num(value):
    """네이버가 주는 문자열('30.07배', '1,668원', '13.07')을 float로. 해석 불가는 None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip().replace(",", "")
        text = text.rstrip("배%원").strip()
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return None  # '-', '1,154조 554억' 같은 복합 표기는 버린다
        num = float(text)
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _get_json(url: str, params: dict | None = None, timeout: float | None = None):
    """짧은 타임아웃 GET → JSON. 실패는 예외 대신 None."""
    try:
        resp = requests.get(url, params=params or None,
                            timeout=TIMEOUT if timeout is None else timeout, headers=HEADERS)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # 네트워크·JSON·차단 모두 흡수
        logger.warning("네이버 요청 실패 %s: %s: %s", url, type(exc).__name__, str(exc)[:160])
        return None


# --- 이름/코드 해석 ----------------------------------------------------------

def _ac_items(query: str) -> list[dict]:
    """네이버 자동완성 결과(종목만). 실패하면 빈 목록."""
    data = _get_json(BASE_AC, {"q": query, "target": "stock"})
    items = (data or {}).get("items") or []
    return [item for item in items if isinstance(item, dict) and item.get("code")]


def _from_item(item: dict, source: str) -> dict | None:
    """자동완성 한 항목 → 해석 결과. KOSPI/KOSDAQ가 아니면 None."""
    suffix = _MARKET_SUFFIX.get(str(item.get("typeCode") or "").upper())
    if not suffix:
        return None
    code = str(item.get("code") or "").strip().upper()
    if not code:
        return None
    return {
        "ticker": f"{code}{suffix}",
        "name": item.get("name") or code,
        "exchange": "KOSPI" if suffix == ".KS" else "KOSDAQ",
        "currency": "KRW",
        "source": source,
    }


def _candidates(items: list[dict], source: str, limit: int = 5) -> list[dict]:
    out = []
    for item in items:
        row = _from_item(item, source)
        if row and row["ticker"] not in {r["ticker"] for r in out}:
            out.append(row)
        if len(out) >= limit:
            break
    return out


def _static_code(name: str) -> str | None:
    """네트워크 없이 내장 목록에서 한국어 이름 → 6자리 코드."""
    target = re.sub(r"\s+", "", str(name or "")).upper()
    if not target:
        return None
    for code, kr_name in KOSPI200_FALLBACK:
        if re.sub(r"\s+", "", kr_name).upper() == target:
            return code
    return EXTRA_NAMES.get(target)


def _static_name(code: str) -> str | None:
    """내장 목록에서 6자리 코드 → 한국어 이름(오프라인)."""
    if not code:
        return None
    for fallback_code, name in KOSPI200_FALLBACK:
        if fallback_code == code:
            return name
    return EXTRA_NAMES_BY_CODE.get(code)


def static_display_name(ticker) -> str | None:
    """오프라인 한국어 이름(코스피 200 + 주요 코스닥 내장 목록). 없으면 None."""
    return _static_name(naver_code(ticker) or "")


def display_name(ticker) -> str | None:
    """한국 종목의 한국어 이름 — 내장 목록 우선, 없으면 네이버 자동완성(코드 조회)."""
    code = naver_code(ticker)
    if not code:
        return None
    name = _static_name(code)
    if name:
        return name
    key = f"name:{code}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    name = None
    for item in _ac_items(code):
        if str(item.get("code") or "").strip().upper() == code:
            name = item.get("name")
            break
    _cache_put(key, name, RESOLVE_TTL if name else FAIL_TTL)
    return name


def _with_candidates(result: dict | None, candidates: list[dict]) -> dict | None:
    """해석 결과에 후보 목록(자기 자신이 첫 번째)을 붙인다."""
    if result is None:
        return None
    out = dict(result)
    rest = [c for c in candidates if c["ticker"] != out["ticker"]][:4]
    out["candidates"] = [{k: out.get(k) for k in ("ticker", "name", "exchange", "currency")}] + rest
    out["candidates"][0]["source"] = out["source"]
    return out


def _resolve_uncached(raw: str) -> dict | None:
    upper = raw.upper()

    # 1) 이미 한국 티커 형식(.KS/.KQ)
    if is_korean_ticker(upper):
        code = naver_code(upper) or ""
        suffix = upper[-3:]
        return _with_candidates({
            "ticker": upper,
            "name": display_name(upper) or code,
            "exchange": "KOSPI" if suffix == ".KS" else "KOSDAQ",
            "currency": "KRW",
            "source": "suffix",
        }, [])

    # 2) 6자리 코드(005930 / 0126Z0) → 자동완성으로 시장 확인
    if _CODE_RE.match(upper):
        items = _ac_items(upper)
        for item in items:
            if str(item.get("code") or "").strip().upper() == upper:
                return _with_candidates(_from_item(item, "naver"), _candidates(items, "naver"))
        static_name = _static_name(upper)
        if static_name:  # 네이버가 막혀도 코스피 200 종목은 해석한다
            return _with_candidates({"ticker": f"{upper}.KS", "name": static_name,
                                     "exchange": "KOSPI", "currency": "KRW",
                                     "source": "fallback"}, [])

    # 3) 한글 이름 → 자동완성(정확 일치 우선) → 내장 목록
    if _KOREAN_RE.search(raw):
        items = _ac_items(raw)
        candidates = _candidates(items, "naver")
        exact = [c for c in candidates
                 if c["name"] == raw or c["name"].replace(" ", "") == raw.replace(" ", "")]
        if exact:
            return _with_candidates(exact[0], candidates)
        if candidates:
            return _with_candidates(candidates[0], candidates)
        code = _static_code(raw)
        if code:
            return _with_candidates({"ticker": f"{code}.KS", "name": _static_name(code) or raw,
                                     "exchange": "KOSPI", "currency": "KRW",
                                     "source": "fallback"}, [])

    # 4) 영문 이름(NAVER·SK 등 한국 종목명) → 이름 정확 일치만 인정, 아니면 미국 티커 통과
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 \-]{0,19}", raw) and _US_TICKER_RE.match(upper):
        items = _ac_items(raw)
        exact = [item for item in items
                 if str(item.get("name") or "").strip().upper() == upper]
        for item in exact:
            row = _from_item(item, "naver")
            if row:
                return _with_candidates(row, _candidates(items, "naver"))
        return _with_candidates({"ticker": upper.replace(".", "-"), "name": None, "exchange": "",
                                 "currency": "USD", "source": "passthrough"}, [])

    return None


def resolve_ticker(query: str) -> dict | None:
    """이름·코드·티커 → 한국/미국 티커 해석.

    반환: `{"ticker","name","exchange","currency","source","candidates"}` 또는 None.
    - `삼성전자` → `005930.KS`, `에코프로` → `086520.KQ`, `000660` → `000660.KS`
    - `NAVER`(한국 종목명) → `035420.KS`, `AAPL`/`NVDA`(미국) → 그대로 통과
    - `005930.KS`처럼 이미 완성된 티커도 정규화해 돌려준다.
    """
    raw = str(query or "").strip()
    if not raw:
        return None
    key = f"resolve:{raw.lower()}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached

    result = _resolve_uncached(raw)
    _cache_put(key, result, RESOLVE_TTL if result else FAIL_TTL)  # 없는 이름은 15분만 기억
    return result


# --- 재무 지표 ---------------------------------------------------------------

def _row_map(finance_info: dict, title: str) -> dict:
    """`financeInfo.rowList`에서 제목이 title인 행의 {연도키: 원문값}."""
    for row in finance_info.get("rowList") or []:
        if row.get("title") == title:
            return {key: (value or {}).get("value") if isinstance(value, dict) else value
                    for key, value in (row.get("columns") or {}).items()}
    return {}


def _fetch_integration_and_annual(code: str) -> tuple:
    """코드 하나의 [통합 시세]·[연간 재무]를 **동시에** 받는다.

    두 응답은 서로 의존하지 않는데 예전에는 순서대로 기다렸다(종목당 2×지연).
    네이버가 종목당 두 요청을 동시에 받는 부담은 작고(각 응답 5~10KB), 15종목 발굴에서
    이 병렬화만으로 대기시간이 약 절반으로 줄었다. `_get_json`은 예외를 흡수한다.
    """
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="naver-fund") as pool:
        integration_future = pool.submit(_get_json, f"{BASE_MOBILE}/stock/{code}/integration")
        annual_future = pool.submit(_get_json, f"{BASE_MOBILE}/stock/{code}/finance/annual")
        return integration_future.result(), annual_future.result()


def has_fundamentals(ticker) -> bool:
    """이 종목의 재무가 캐시에 살아 있는지(예열·중복 조회 판단용)."""
    code = naver_code(ticker)
    if not code:
        return False
    return _cache_get(f"fund:{code}") is not _MISS


def _fetch_fundamentals(code: str) -> dict:
    """네이버 통합 시세 + 연간 재무 → stock_tools 키와 같은 재무 dict."""
    out = {
        "name": None,
        "trailing_pe": None,
        "forward_pe": None,
        "revenue_growth": None,
        "operating_margins": None,
        "debt_to_equity": None,
        "pbr": None,
        "eps": None,
        "dividend_yield": None,
        "sector": None,
        "industry_pe": None,
        "source": "naver",
    }

    # ① 통합 시세(현재 PER·추정 PER·PBR·업종)와 ② 연간 재무를 동시에 받는다.
    integration, annual = _fetch_integration_and_annual(code)

    # ① 통합 시세: 현재 PER · 추정 PER · PBR (+ 업종 코드·같은 업종 종목)
    if isinstance(integration, dict):
        out["name"] = integration.get("stockName") or out["name"]
        infos = {str(i.get("code")): i.get("value")
                 for i in (integration.get("totalInfos") or []) if isinstance(i, dict)}
        out["trailing_pe"] = _num(infos.get("per"))
        out["forward_pe"] = _num(infos.get("cnsPer"))
        out["pbr"] = _num(infos.get("pbr"))
        out["eps"] = _num(infos.get("eps"))
        out["dividend_yield"] = _num(infos.get("dividendYieldRatio"))

        # ①-1 동종업계: 업종명 + 업종 기준 PER(이웃 종목 PER 중앙값)
        industry_code = str(integration.get("industryCode") or "").strip()
        peers = [str(p.get("itemCode")).strip()
                 for p in (integration.get("industryCompareInfo") or [])
                 if isinstance(p, dict) and p.get("itemCode")]
        peers = [peer for peer in peers if peer and peer != code]
        if industry_code:
            out["sector"] = _industry_name(industry_code)
        if out["trailing_pe"] is not None and peers:
            out["industry_pe"] = _industry_pe(industry_code or code, peers)

    # ② 연간 재무: 영업이익률 · 부채비율 · 매출증가율(확정 연도끼리)
    info = (annual or {}).get("financeInfo") if isinstance(annual, dict) else None
    if isinstance(info, dict):
        titles = [t for t in (info.get("trTitleList") or []) if isinstance(t, dict)]
        confirmed = [str(t.get("key")) for t in titles
                     if str(t.get("isConsensus") or "N").upper() != "Y" and t.get("key")]
        confirmed.sort()
        revenue = _row_map(info, "매출액")
        op_margin = _row_map(info, "영업이익률")
        debt_ratio = _row_map(info, "부채비율")

        with_revenue = [key for key in confirmed if _num(revenue.get(key)) is not None]
        latest = with_revenue[-1] if with_revenue else (confirmed[-1] if confirmed else None)
        if latest:
            margin = _num(op_margin.get(latest))
            if margin is not None:
                out["operating_margins"] = round(margin / 100.0, 4)   # % → 소수
            debt = _num(debt_ratio.get(latest))
            if debt is not None:
                out["debt_to_equity"] = round(debt, 2)                # 이미 %
        if len(with_revenue) >= 2:
            cur, prev = _num(revenue[with_revenue[-1]]), _num(revenue[with_revenue[-2]])
            if cur is not None and prev not in (None, 0):
                out["revenue_growth"] = round((cur - prev) / abs(prev), 4)

    return out


def _industry_name(industry_code: str) -> str | None:
    """네이버 업종 코드 → 업종명(예: 278 → `반도체와반도체장비`). 실패하면 None.

    업종 목록 API는 종목 목록과 함께 `groupInfo.name`을 준다 — pageSize=1이면 응답이 작다.
    """
    code = str(industry_code or "").strip()
    if not code:
        return None
    key = f"indname:{code}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    data = _get_json(f"{BASE_MOBILE}/stocks/industry/{code}", {"pageSize": 1}, timeout=PEER_TIMEOUT)
    name = None
    if isinstance(data, dict):
        name = (data.get("groupInfo") or {}).get("name")
    name = str(name).strip() if name else None
    _cache_put(key, name, INDUSTRY_TTL if name else FAIL_TTL)
    return name


def _peer_pe(code: str) -> float | None:
    """이웃 종목 하나의 현재 PER(6시간 캐시). 적자(0 이하)·결측은 None."""
    key = f"pe:{code}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    data = _get_json(f"{BASE_MOBILE}/stock/{code}/integration", timeout=PEER_TIMEOUT)
    pe = None
    if isinstance(data, dict):
        infos = {str(i.get("code")): i.get("value")
                 for i in (data.get("totalInfos") or []) if isinstance(i, dict)}
        pe = _num(infos.get("per"))
        if pe is not None and pe <= 0:
            pe = None
    _cache_put(key, pe, FUND_TTL if pe is not None else FAIL_TTL)
    return pe


def _peer_median(peers: list[str]) -> float | None:
    """이웃 종목 PER 중앙값 — 표본이 PEER_MIN_SAMPLES개 미만이면 None.

    중앙값을 쓰는 이유: 한 종목의 적자 직전 PER(수백 배)이 평균을 통째로 끌고 간다.
    """
    codes = [str(peer).strip() for peer in (peers or []) if str(peer).strip()][:PEER_SAMPLE_LIMIT]
    if len(codes) < PEER_MIN_SAMPLES:
        return None
    values: list[float] = []
    try:
        with ThreadPoolExecutor(max_workers=PEER_WORKERS, thread_name_prefix="naver-peer") as pool:
            for value in pool.map(_peer_pe, codes):
                if value is not None:
                    values.append(float(value))
    except Exception as exc:  # 병렬 조회 실패 → 업종 비교만 생략
        logger.warning("업종 PER 이웃 조회 실패: %s: %s", type(exc).__name__, str(exc)[:120])
        return None
    if len(values) < PEER_MIN_SAMPLES:
        return None
    return round(float(statistics.median(values)), 2)


def _industry_pe(industry_key: str, peers: list[str]) -> float | None:
    """업종 기준 PER(이웃 종목 PER 중앙값). 업종 단위 6시간 캐시 + 업종별 락.

    같은 업종 종목이 병렬로 스크리닝돼도 이웃 PER 조회는 한 번만 한다.
    """
    key = f"indpe:{industry_key}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    with _industry_lock(key):
        cached = _cache_get(key)
        if cached is not _MISS:
            return cached
        value = _peer_median(peers)
        _cache_put(key, value, INDUSTRY_TTL if value is not None else FAIL_TTL)
        return value


def get_fundamentals(ticker) -> dict | None:
    """한국 종목의 네이버 재무 지표(6시간 캐시). 한국 티커가 아니면 None.

    반환 키: `name`·`trailing_pe`·`forward_pe`·`revenue_growth`(소수)·`operating_margins`(소수)·
    `debt_to_equity`(%)·`pbr`·`eps`·`dividend_yield`·`sector`(업종명)·`industry_pe`(업종 기준 PER)·
    `source`. 못 받은 값은 None.
    """
    code = naver_code(ticker)
    if not code:
        return None
    key = f"fund:{code}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached

    data = _fetch_fundamentals(code)
    has_value = any(data.get(field) is not None
                    for field in ("trailing_pe", "forward_pe", "revenue_growth",
                                  "operating_margins", "debt_to_equity"))
    _cache_put(key, data, FUND_TTL if has_value else FAIL_TTL)
    return data


# --- 실시간 현재가 -----------------------------------------------------------
# 라이브 확인한 엔드포인트 (2026-11-20, 장중 15:11 KST)
# - 실시간 polling  `https://polling.finance.naver.com/api/realtime/domestic/stock/005930`
#   → `{"pollingInterval":7000,"datas":[{"itemCode","stockName","closePrice":"183,200",
#      "marketStatus":"OPEN","localTradedAt":"...",...}]}`  · 7초 간격 갱신, **쉼표로 여러 코드**를
#      한 번에 받을 수 있다(`/stock/005930,000660` → datas 2개). 잘못된 코드만 있으면
#      `datas: []`(200)이므로 404 예외 처리가 필요 없다.
# - 종목 기본    `https://m.stock.naver.com/api/stock/005930/basic` → 같은 `closePrice`
#   polling이 빈 응답을 준 경우(일부 코드만 누락)에만 코드별로 한 번 더 확인하는 폴백이다.
#
# 장 마감 뒤에도 `closePrice`는 그날 종가로 채워져 있어 "현재가"로 쓸 수 있다(장외 단일가인
# `overMarketPriceInfo.overPrice`는 일봉 종가와 기준이 달라 쓰지 않는다).
#
# 반환 dict: `{"price": float, "source": "naver_realtime", "market_status": str, "traded_at": str}`


def _parse_realtime_item(item) -> dict | None:
    """polling 응답 한 항목 → 시세 dict. 가격이 없거나 0 이하이면 None."""
    if not isinstance(item, dict):
        return None
    price = _num(item.get("closePrice"))
    if price is None or price <= 0:
        return None
    return {
        "price": float(price),
        "source": "naver_realtime",
        "market_status": str(item.get("marketStatus") or ""),
        "traded_at": str(item.get("localTradedAt") or ""),
    }


def _basic_quote(code: str) -> dict | None:
    """polling이 그 코드를 안 줬을 때의 단건 폴백(`.../basic`)."""
    data = _get_json(f"{BASE_MOBILE}/stock/{code}/basic", timeout=REALTIME_TIMEOUT)
    if not isinstance(data, dict):
        return None
    price = _num(data.get("closePrice"))
    if price is None or price <= 0:
        return None
    return {
        "price": float(price),
        "source": "naver_realtime",
        "market_status": str(data.get("marketStatus") or ""),
        "traded_at": str(data.get("localTradedAt") or ""),
    }


def _fetch_realtime_chunk(codes: list[str]) -> dict[str, dict]:
    """코드 여러 개를 polling 한 번으로 받는다. 응답이 아예 없으면 폴백도 하지 않는다.

    (polling 자체가 죽었을 때 코드마다 basic을 부르면 50번 요청이 나간다 — 그 경우는
     빈 dict로 끝내고 호출부가 일봉 종가를 유지하는 편이 낫다.)
    """
    data = _get_json(f"{BASE_POLLING}/{','.join(codes)}", timeout=REALTIME_TIMEOUT)
    out: dict[str, dict] = {}
    if isinstance(data, dict):
        for item in data.get("datas") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("itemCode") or "").strip().upper()
            quote = _parse_realtime_item(item)
            if code and quote:
                out[code] = quote
    else:
        return out  # 네트워크·차단 실패 → 폴백 없음

    for code in codes:  # 응답은 왔는데 일부만 빠진 경우만 단건으로 보충
        if code in out:
            continue
        quote = _basic_quote(code)
        if quote:
            out[code] = quote
    return out


def get_realtime_prices(tickers) -> dict[str, dict]:
    """한국 종목 여러 개의 실시간 현재가를 10초 캐시로 모아 돌려준다.

    - 여러 코드를 `REALTIME_BATCH`개씩 묶어 **요청 수를 종목 수가 아니라 묶음 수**로 만든다
        (15종목이면 1회). 스크리닝·성적표에서 부담 없이 부를 수 있는 이유다.
    - 반환: `{야후 티커: {"price", "source": "naver_realtime", "market_status", "traded_at"}}`
      실패한 종목은 키가 없다(호출부가 일봉 종가 유지).
    - 한국 티커(`.KS`/`.KQ`)가 아니면 아무것도 하지 않는다.
    """
    wanted: dict[str, str] = {}       # 야후 티커 -> 6자리 코드
    pending: list[str] = []
    out: dict[str, dict] = {}
    for raw in tickers or []:
        sym = str(raw or "").strip().upper()
        code = naver_code(sym)
        if not sym or not code or sym in wanted:
            continue
        wanted[sym] = code
        cached = _cache_get(f"rt:{code}")
        if cached is not _MISS:
            if cached:
                out[sym] = dict(cached)
            continue
        pending.append(code)

    fetched: dict[str, dict] = {}
    for start in range(0, len(pending), REALTIME_BATCH):
        fetched.update(_fetch_realtime_chunk(pending[start:start + REALTIME_BATCH]))

    for code in pending:
        quote = fetched.get(code)
        _cache_put(f"rt:{code}", dict(quote) if quote else None,
                   REALTIME_TTL if quote else REALTIME_FAIL_TTL)

    for sym, code in wanted.items():
        if sym in out:
            continue
        quote = fetched.get(code)
        if quote:
            out[sym] = dict(quote)
    return out


def get_realtime_price(ticker) -> float | None:
    """한국 종목 하나의 실시간 현재가(원). 실패·타임아웃·파싱 불가면 None."""
    sym = str(ticker or "").strip().upper()
    if not naver_code(sym):
        return None
    quote = get_realtime_prices([sym]).get(sym)
    return float(quote["price"]) if quote else None


# --- 한국 뉴스 ---------------------------------------------------------------

def _clean_title(raw) -> str:
    text = str(raw or "").strip()
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")


def _google_news_titles(query: str, limit: int) -> list[str]:
    """구글 뉴스 RSS(한국어)에서 제목만. 실패하면 빈 목록."""
    try:
        resp = requests.get(BASE_GOOGLE_NEWS, timeout=TIMEOUT, headers=HEADERS,
                            params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning("구글 뉴스 실패(%s): %s: %s", query, type(exc).__name__, str(exc)[:120])
        return []
    out = []
    for item in root.findall(".//item")[:max(1, limit)]:
        title = _clean_title(item.findtext("title"))
        if title:
            out.append(title)
    return out


def get_korean_news(ticker, limit: int = 3) -> list[str]:
    """한국 종목 최근 뉴스 제목 — 구글 뉴스(한국어, '{종목명} 주가') 우선, 모자라면 네이버 종목 뉴스로 보충.

    네이버 종목 뉴스 API는 시장 전체 인기 기사를 함께 내려 주는 경우가 있어(다른 종목과 같은
    제목이 섞인다) 종목명으로 검색되는 구글 뉴스 RSS를 먼저 쓴다. 둘 다 실패하면 호출부(api)가
    yfinance 뉴스로 넘어간다.
    """
    code = naver_code(ticker)
    if not code:
        return []
    key = f"news:{code}:{int(limit)}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return list(cached)

    titles: list[str] = []
    name = static_display_name(ticker) or display_name(ticker)
    if name:
        for title in _google_news_titles(f"{name} 주가", limit * 3):
            if title not in titles:
                titles.append(title)

    if len(titles) < limit:
        data = _get_json(f"{BASE_MOBILE}/news/stock/{code}", {"pageSize": max(6, limit * 3)})
        for group in data or []:
            if not isinstance(group, dict):
                continue
            for item in group.get("items") or []:
                if not isinstance(item, dict):
                    continue
                title = _clean_title(item.get("title"))
                if title and title not in titles:
                    titles.append(title)

    titles = titles[:max(1, limit)]
    _cache_put(key, titles, NEWS_TTL if titles else FAIL_TTL)
    return list(titles)


# --- 코스피 200 구성종목 ------------------------------------------------------
# 라이브(네이버) 실패 시 쓰는 폴백. 2026-11-19 네이버 `index/KPI200/enrollStocks`에서
# 받은 201종목(코스피 200 + 편입 대기 포함)을 그대로 굳혀 두었다. 시가총액 순 아니다.
KOSPI200_FALLBACK = (
    ("005930", "삼성전자"), ("000660", "SK하이닉스"),
    ("402340", "SK스퀘어"), ("373220", "LG에너지솔루션"),
    ("009150", "삼성전기"), ("207940", "삼성바이오로직스"),
    ("105560", "KB금융"), ("005380", "현대차"),
    ("032830", "삼성생명"), ("068270", "셀트리온"),
    ("028260", "삼성물산"), ("055550", "신한지주"),
    ("034730", "SK"), ("000270", "기아"),
    ("006400", "삼성SDI"), ("086790", "하나금융지주"),
    ("012450", "한화에어로스페이스"), ("012330", "현대모비스"),
    ("034020", "두산에너빌리티"), ("329180", "HD현대중공업"),
    ("035420", "NAVER"), ("000810", "삼성화재"),
    ("316140", "우리금융지주"), ("066570", "LG전자"),
    ("011200", "HMM"), ("138040", "메리츠금융지주"),
    ("005490", "POSCO홀딩스"), ("033780", "KT&G"),
    ("010120", "LS ELECTRIC"), ("017670", "SK텔레콤"),
    ("096770", "SK이노베이션"), ("018260", "삼성에스디에스"),
    ("010130", "고려아연"), ("298040", "효성중공업"),
    ("042700", "한미반도체"), ("009540", "HD한국조선해양"),
    ("015760", "한국전력"), ("024110", "기업은행"),
    ("086280", "현대글로비스"), ("267260", "HD현대일렉트릭"),
    ("051910", "LG화학"), ("003670", "포스코퓨처엠"),
    ("180640", "한진칼"), ("010140", "삼성중공업"),
    ("035720", "카카오"), ("042660", "한화오션"),
    ("003550", "LG"), ("028050", "삼성E&A"),
    ("003490", "대한항공"), ("030200", "KT"),
    ("010950", "S-Oil"), ("005830", "DB손해보험"),
    ("006800", "미래에셋증권"), ("323410", "카카오뱅크"),
    ("047810", "한국항공우주"), ("003230", "삼양식품"),
    ("267250", "HD현대"), ("272210", "한화시스템"),
    ("278470", "에이피알"), ("259960", "크래프톤"),
    ("0126Z0", "삼성에피스홀딩스"), ("000720", "현대건설"),
    ("000150", "두산"), ("005940", "NH투자증권"),
    ("047050", "포스코인터내셔널"), ("307950", "현대오토에버"),
    ("443060", "HD현대마린솔루션"), ("090430", "아모레퍼시픽"),
    ("079550", "LIG디펜스앤에어로스페이스"), ("352820", "하이브"),
    ("161390", "한국타이어앤테크놀로지"), ("078930", "GS"),
    ("021240", "코웨이"), ("064350", "현대로템"),
    ("064400", "LG씨엔에스"), ("071050", "한국금융지주"),
    ("011070", "LG이노텍"), ("326030", "SK바이오팜"),
    ("016360", "삼성증권"), ("032640", "LG유플러스"),
    ("007660", "이수페타시스"), ("000100", "유한양행"),
    ("006260", "LS"), ("128940", "한미약품"),
    ("039490", "키움증권"), ("241560", "두산밥캣"),
    ("029780", "삼성카드"), ("175330", "JB금융지주"),
    ("009830", "한화솔루션"), ("271560", "오리온"),
    ("047040", "대우건설"), ("377300", "카카오페이"),
    ("267270", "HD건설기계"), ("138930", "BNK금융지주"),
    ("012750", "에스원"), ("036570", "NC"),
    ("004020", "현대제철"), ("051900", "LG생활건강"),
    ("000880", "한화"), ("001440", "대한전선"),
    ("062040", "산일전기"), ("034220", "LG디스플레이"),
    ("011790", "SKC"), ("036460", "한국가스공사"),
    ("008930", "한미사이언스"), ("022100", "포스코DX"),
    ("001040", "CJ"), ("000990", "DB하이텍"),
    ("454910", "두산로보틱스"), ("192820", "코스맥스"),
    ("088350", "한화생명"), ("010060", "OCI홀딩스"),
    ("111770", "영원무역"), ("018880", "한온시스템"),
    ("002380", "KCC"), ("035250", "강원랜드"),
    ("302440", "SK바이오사이언스"), ("066970", "엘앤에프"),
    ("001450", "현대해상"), ("009420", "한올바이오파마"),
    ("004170", "신세계"), ("026960", "동서"),
    ("097950", "CJ제일제당"), ("161890", "한국콜마"),
    ("028670", "팬오션"), ("251270", "넷마블"),
    ("052690", "한전기술"), ("139130", "iM금융지주"),
    ("017800", "현대엘리베이터"), ("282330", "BGF리테일"),
    ("023530", "롯데쇼핑"), ("383220", "F&F"),
    ("011780", "금호석유화학"), ("457190", "이수스페셜티케미컬"),
    ("004990", "롯데지주"), ("007340", "DN오토모티브"),
    ("002790", "아모레퍼시픽홀딩스"), ("069960", "현대백화점"),
    ("004370", "농심"), ("009970", "영원무역홀딩스"),
    ("082740", "한화엔진"), ("011170", "롯데케미칼"),
    ("375500", "DL이앤씨"), ("014680", "한솔케미칼"),
    ("450080", "에코프로머티"), ("030000", "제일기획"),
    ("000240", "한국앤컴퍼니"), ("007070", "GS리테일"),
    ("005850", "에스엘"), ("139480", "이마트"),
    ("112610", "씨에스윈드"), ("081660", "미스토홀딩스"),
    ("483650", "달바글로벌"), ("000120", "CJ대한통운"),
    ("051600", "한전KPS"), ("204320", "HL만도"),
    ("006280", "녹십자"), ("073240", "금호타이어"),
    ("008770", "호텔신라"), ("103140", "풍산"),
    ("001800", "오리온홀딩스"), ("006040", "동원산업"),
    ("011210", "현대위아"), ("280360", "롯데웰푸드"),
    ("192080", "더블유게임즈"), ("007310", "오뚜기"),
    ("071970", "HD현대마린엔진"), ("069620", "대웅제약"),
    ("000080", "하이트진로"), ("298020", "효성티앤씨"),
    ("001430", "세아베스틸지주"), ("300720", "한일시멘트"),
    ("120110", "코오롱인더"), ("034230", "파라다이스"),
    ("005300", "롯데칠성"), ("003240", "태광산업"),
    ("093370", "후성"), ("361610", "SK아이이테크놀로지"),
    ("003090", "대웅"), ("137310", "에스디바이오센서"),
    ("000210", "DL"), ("185750", "종근당"),
    ("004000", "롯데정밀화학"), ("009240", "한샘"),
    ("071320", "지역난방공사"), ("298050", "HS효성첨단소재"),
    ("017960", "한국카본"), ("285130", "SK케미칼"),
    ("0220W0", "한화머시너리앤서비스홀딩스"), ("268280", "미원에스씨"),
    ("014820", "동원시스템즈"), ("001680", "대상"),
    ("006650", "대한유화"), ("002840", "미원상사"),
    ("000670", "영풍"), ("069260", "TKG휴켐스"),
    ("456040", "OCI"), ("003030", "세아제강지주"),
    ("002030", "아세아"), ("005420", "코스모화학"),
    ("008730", "율촌화학"),
)

# 코스피 200 밖이지만 이름으로 자주 찾는 코스닥 대표 종목(오프라인 해석·이름 표시용).
EXTRA_NAMES = {
    "에코프로": "086520",
    "에코프로비엠": "247540",
    "알테오젠": "196170",
    "HLB": "028300",
    "리노공업": "058470",
    "셀트리온제약": "068760",
    "카카오게임즈": "293490",
    "펄어비스": "263750",
    "위메이드": "112040",
    "솔브레인": "357780",
}
EXTRA_NAMES_BY_CODE = {code: name for name, code in EXTRA_NAMES.items()}


_TABLE_LOCK = threading.Lock()
_TABLE_CACHE: dict = {"ts": 0.0, "rows": [], "source": ""}


def _live_kospi200_rows() -> list[dict]:
    """네이버 `enrollStocks`를 페이지로 끝까지 읽어 구성종목을 만든다."""
    rows: list[dict] = []
    seen: set[str] = set()
    for page in range(1, KOSPI200_MAX_PAGES + 1):
        data = _get_json(f"{BASE_MOBILE}/index/KPI200/enrollStocks", {"page": page})
        if not isinstance(data, list) or not data:
            break
        for item in data:
            if not isinstance(item, dict):
                continue
            code = str(item.get("itemCode") or "").strip().upper()
            if not code or code in seen:
                continue
            exchange = str((item.get("stockExchangeType") or {}).get("code") or "KS").upper()
            suffix = ".KQ" if exchange == "KQ" else ".KS"
            seen.add(code)
            rows.append({"symbol": f"{code}{suffix}",
                         "name": item.get("stockName") or code,
                         "exchange": "코스피 200"})
        if len(data) < KOSPI200_PAGE_SIZE:
            break
    return rows


def _fallback_rows() -> list[dict]:
    return [{"symbol": f"{code}.KS", "name": name, "exchange": "코스피 200"}
            for code, name in KOSPI200_FALLBACK]


def get_kospi200_table(force_refresh: bool = False) -> dict:
    """코스피 200 목록 `{"rows", "source"}`, 6시간 캐시.

    라이브가 150종목 미만이거나 실패하면 직전 성공값 → 내장 폴백 순으로 채운다.
    """
    now = time.time()
    with _TABLE_LOCK:
        cached = dict(_TABLE_CACHE)
    if not force_refresh and cached["rows"] and now - cached["ts"] < TABLE_TTL:
        return {"rows": list(cached["rows"]), "source": cached["source"]}

    rows = _live_kospi200_rows()
    if len(rows) >= MIN_LIVE_CONSTITUENTS:
        with _TABLE_LOCK:
            _TABLE_CACHE.update(ts=now, rows=rows, source="naver")
        return {"rows": list(rows), "source": "naver"}

    if cached["rows"]:  # 라이브가 흔들려도 직전 성공값 유지
        logger.warning("코스피 200 라이브 목록 부족(%d종목) → 직전 목록 %d종목 유지",
                       len(rows), len(cached["rows"]))
        return {"rows": list(cached["rows"]), "source": cached["source"] or "cache"}

    rows = _fallback_rows()
    logger.warning("코스피 200 라이브 실패 → 내장 폴백 %d종목", len(rows))
    with _TABLE_LOCK:
        _TABLE_CACHE.update(ts=now, rows=rows, source="fallback")
    return {"rows": rows, "source": "fallback"}


def get_kospi200_tickers(force_refresh: bool = False) -> list[str]:
    """스캔용 코스피 200 티커(중복 제거, `.KS`/`.KQ`)."""
    seen: set[str] = set()
    out: list[str] = []
    for row in get_kospi200_table(force_refresh=force_refresh)["rows"]:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym or sym in seen or not is_korean_ticker(sym):
            continue
        seen.add(sym)
        out.append(sym)
    return out


def reset_table_cache() -> None:
    with _TABLE_LOCK:
        _TABLE_CACHE.update(ts=0.0, rows=[], source="")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for query in ("삼성전자", "에코프로", "000660", "NAVER", "AAPL"):
        print(query, "→", resolve_ticker(query))
    print("fund", get_fundamentals("005930.KS"))
    print("news", get_korean_news("005930.KS", 3))
    tickers = get_kospi200_tickers()
    print("kospi200", len(tickers), tickers[:5])
