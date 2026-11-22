"""동종업계 PER 비교 — 종목에 `sector`(업종)와 `industry_pe`(업종 기준 PER)를 붙인다.

왜 필요한가
-----------
`scoring._score_value`가 PER 절대값만 보면 **'업종 전체가 비싼 곳이라 30배가 싼 종목'** 과
**'업종 전체가 싼 곳이라 15배가 비싼 종목'** 을 구분하지 못한다. 사용자가 "PER이 상대적으로
낮은" 종목을 원할 때 기준은 시장 평균이 아니라 **같은 업종**이다.

값은 어디서 오나 (추가 호출을 최소화하는 쪽으로 고른다)
-----------------------------------------------------
- 한국 종목(.KS/.KQ): 네이버 금융. 종목 통합 조회 응답에 이미 들어 있는 `industryCompareInfo`
  (같은 업종 종목)로 업종명(`sector`)과 같은 업종 PER 중앙값(`industry_pe`)을 만든다
  — 자세한 계산은 `naver_finance.get_fundamentals`가 맡는다(6시간 캐시).
- 미국 종목: S&P 500 목록표의 GICS Sector를 `sector`로 쓰고(추가 호출 0회),
  `industry_pe`는 **이 프로세스가 최근 24시간 동안 본 같은 섹터 종목들의 PER 중앙값**이다.
  500종목의 PER을 새로 받아 오면 스캔이 수 분 늘어나므로, 스크리닝이 지나가며 채운 표본을
  재사용한다. 표본이 `MIN_SAMPLES`개 미만이면 `None` — 몇 개로 만든 중앙값은 비교 근거가
  되지 못하고, 없으면 `scoring`이 절대 PER 기준으로 자연히 되돌아간다.

설계 원칙
---------
- 네트워크 호출 0회(이 모듈 자체는). 스레드 안전. 실패·결측은 조용히 `None`.
- 같은 입력이면 같은 값: 표본은 (섹터, 티커)별로 하나만 남기고 오래된 것만 버린다.
"""
import logging
import math
import threading
import time

logger = logging.getLogger("sector_pe")

PE_SAMPLE_TTL = 24 * 3600   # 표본 유지 시간 — 하루면 PER이 크게 달라지지 않는다
MIN_SAMPLES = 4             # 업종 중앙값을 인정하는 최소 표본 수
MAX_SECTORS = 64            # 섹터 수 상한(메모리 보호)

# sector -> {ticker: (pe, 저장 시각)}
_SAMPLES: dict[str, dict[str, tuple[float, float]]] = {}
_LOCK = threading.Lock()


# 미국 종목은 두 출처에서 섹터명이 온다 — Yahoo `info.sector`("Technology")와
# S&P 500 표의 GICS Sector("Information Technology"). 같은 섹터가 두 이름으로 갈라지면
# 중앙값 표본이 쪼개지고 화면 표기도 흔들리므로 GICS 표기로 통일한다.
_SECTOR_ALIASES = {
    "technology": "Information Technology",
    "information technology": "Information Technology",
    "financial services": "Financials",
    "financials": "Financials",
    "healthcare": "Health Care",
    "health care": "Health Care",
    "consumer cyclical": "Consumer Discretionary",
    "consumer discretionary": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "consumer staples": "Consumer Staples",
    "basic materials": "Materials",
    "materials": "Materials",
    "communication services": "Communication Services",
    "industrials": "Industrials",
    "energy": "Energy",
    "utilities": "Utilities",
    "real estate": "Real Estate",
}


# --- 공개 API ---------------------------------------------------------------

def normalize_sector(value) -> str | None:
    """업종명 문자열 정리 — 빈 값·플레이스홀더(N/A, -)는 None, 미국 섹터는 GICS 표기로 통일."""
    text = str(value or "").strip()
    if not text or text.lower() in ("nan", "none", "n/a", "na", "-", "unknown"):
        return None
    return _SECTOR_ALIASES.get(text.lower(), text)


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num) or num <= 0:
        return None
    return num


def record_pe(sector, ticker, pe=None) -> bool:
    """한 종목의 PER을 섹터 표본에 기록한다. 기록했으면 True.

    PER이 0·음수·결측이면(적자 기업) 기록하지 않는다 — 중앙값을 왜곡한다.
    """
    name = normalize_sector(sector)
    sym = str(ticker or "").strip().upper()
    value = _number(pe)
    if not name or not sym or value is None:
        return False
    now = time.time()
    with _LOCK:
        if name not in _SAMPLES and len(_SAMPLES) >= MAX_SECTORS:
            # 가장 오래된 섹터를 버린다(무한히 쌓이지 않게).
            oldest = min(_SAMPLES.items(),
                         key=lambda kv: max((t for _, t in kv[1].values()), default=0.0))
            _SAMPLES.pop(oldest[0], None)
        bucket = _SAMPLES.setdefault(name, {})
        bucket[sym] = (value, now)
    return True


def sector_median_pe(sector, exclude_ticker: str | None = None,
                     min_samples: int = MIN_SAMPLES) -> tuple[float | None, int]:
    """섹터 표본의 PER 중앙값과 표본 수를 돌려준다. 표본이 모자라면 `(None, n)`.

    중앙값을 쓰는 이유: 한 종목의 적자 직전 PER(수백 배)이 평균을 통째로 끌고 간다.
    """
    name = normalize_sector(sector)
    if not name:
        return None, 0
    skip = str(exclude_ticker or "").strip().upper()
    now = time.time()
    with _LOCK:
        bucket = dict(_SAMPLES.get(name) or {})
    values = sorted(pe for sym, (pe, at) in bucket.items()
                    if sym != skip and now - at < PE_SAMPLE_TTL)
    if len(values) < max(2, int(min_samples)):
        return None, len(values)
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0
    return round(median, 2), len(values)


def us_sector(ticker) -> str | None:
    """S&P 500 표(GICS Sector)에서 미국 종목의 섹터. 모르면 None."""
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None
    try:  # 순환 import 방지: 호출 시점에 가져온다
        import sp500_scanner
        return normalize_sector(sp500_scanner.sector_for(sym))
    except Exception as exc:
        logger.debug("%s: 섹터 표 조회 실패 → %s", sym, type(exc).__name__)
        return None


def fill_sector_pe(rows, record: bool = True) -> list[dict]:
    """행 목록에 `sector`·`industry_pe`를 채운다(없는 것만). 입력 행을 그대로 고친다.

    - `sector`가 비어 있으면 미국 종목이면 S&P 500 표에서 채운다.
    - `industry_pe`가 비어 있으면(네이버 업종 PER이 없는 경우) 같은 섹터 표본 중앙값을 쓴다.
    - `record=True`면 이번 행들의 PER도 표본에 기록한다(다음 요청의 중앙값이 두터워진다).
    """
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    for row in rows:
        if row.get("error"):
            continue
        sector = normalize_sector(row.get("sector")) or us_sector(row.get("ticker"))
        if sector:
            row["sector"] = sector      # 표기 통일(Yahoo "Technology" → GICS "Information Technology")
        if record:
            record_pe(sector, row.get("ticker"), row.get("trailing_pe"))
    for row in rows:
        if row.get("error") or row.get("industry_pe") is not None:
            continue
        sector = normalize_sector(row.get("sector"))
        if not sector:
            continue
        median, count = sector_median_pe(sector, exclude_ticker=row.get("ticker"))
        if median is not None:
            row["industry_pe"] = median
            row["industry_pe_basis"] = f"{sector} 섹터 {count}종목 PER 중앙값"
    return rows


def sample_status() -> dict:
    """`GET /` 진단용 — 섹터별 표본 수(종목·가격 값은 노출하지 않는다)."""
    now = time.time()
    with _LOCK:
        return {name: sum(1 for _, at in bucket.values() if now - at < PE_SAMPLE_TTL)
                for name, bucket in _SAMPLES.items()}


def clear_cache() -> None:
    """테스트·수동 초기화용."""
    with _LOCK:
        _SAMPLES.clear()
