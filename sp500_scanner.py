"""S&P 500 자동 발굴 — 500종목 기술적 프리 스크리너.

왜 필요한가
-----------
`/api/screen`은 지금까지 관심 종목 15개 안에서만 골랐다. 이 모듈은 S&P 500 전체(약 500개)를
**6개월 일봉만으로** 빠르게 훑어 조건(전략)에 맞는 상위 N개(기본 15)를 추린 뒤, 그 15개만
기존 3단 재무 파이프라인(Yahoo → SEC → Finnhub) + 뉴스 + Gemini로 넘긴다.

설계 원칙
---------
- **배치 다운로드**: `yf.download(..., period="6mo", group_by="ticker", threads=True)`를
  `CHUNK_SIZE`(100)개씩 끊어 처리하고, 청크 DataFrame은 계산 직후 버린다(`del` + `gc.collect()`).
  Render 무료 인스턴스(512MB)에서 500종목을 한 번에 받으면 메모리가 튀므로 청크로 나눈다.
- **프리 스캔에서는 네트워크를 더 쓰지 않는다**: `t.info`·SEC·Finnhub·뉴스는 절대 부르지 않는다.
  500종목에 그 호출을 하면 수 분이 걸리고 Yahoo rate limit에 바로 걸린다. 지표는 이미 받은
  일봉에서만 계산한다(`stock_tools.compute_advanced_indicators`, `stock_tools._calc_rsi`).
- **스냅샷 1시간 캐시**(스레드 안전): 같은 스냅샷 위에서 전략별 랭킹만 다시 하므로 반복
  조회는 즉시 끝난다. 스캔은 `_SCAN_LOCK`으로 직렬화해 동시 요청이 500종목을 두 번 받지 않는다.
- **끊겨도 죽지 않는다**: 종목 리스트는 Wikipedia(짧은 타임아웃) → 실패 시 내장 폴백 목록,
  개별 종목 데이터가 없거나 NaN이어도 그 종목만 건너뛴다.
"""
import gc
import io
import logging
import math
import re
import threading
import time

import pandas as pd
import requests
import yfinance as yf

import scoring
import stock_tools

logger = logging.getLogger("sp500_scanner")

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_TIMEOUT = 8            # Wikipedia가 느릴 때 스캔이 멈추지 않게 짧게 끊는다
TICKER_LIST_TTL = 6 * 3600  # 종목 구성은 자주 바뀌지 않는다
PRESCAN_TTL = 3600          # 스냅샷 1시간 (반복 조회 즉시 응답)
CHUNK_SIZE = 100            # 한 번에 받는 종목 수 (메모리 상한)
TOP_N = 15                  # Gemini에 넘길 후보 수
MIN_BARS = 35               # 이보다 짧은 이력은 지표가 비어 후보에서 제외
DOWNLOAD_TIMEOUT = 25       # 청크 하나의 yfinance 타임아웃(초)

# 스캔 결과에 남기는 지표 키(재무 지표는 프리 스캔에 없다).
TECH_FIELDS = (
    "price", "ma20", "ma60", "rsi14", "trend",
    "macd", "macd_signal", "macd_hist", "macd_cross", "macd_state",
    "bb_upper", "bb_lower", "bb_pct_b",
    "volume_ratio", "drawdown_from_high_pct", "range_position_pct",
)

# --- 내장 폴백 목록 ----------------------------------------------------------
# Wikipedia가 막히거나 느릴 때 쓰는 S&P 500 구성 종목(2026-11 기준 503개).
# Yahoo 형식에 맞춰 BRK.B → BRK-B 처럼 점을 하이픈으로 바꿔 둔다.
FALLBACK_TICKERS = (
    "MMM", "AOS", "ABT", "ABBV", "ACN", "ADBE", "AMD", "AES", "AFL", "A",
    "APD", "ABNB", "AKAM", "ALB", "ARE", "ALGN", "ALLE", "LNT", "ALL", "GOOGL",
    "GOOG", "MO", "AMZN", "AMCR", "AEE", "AEP", "AXP", "AIG", "AMT", "AWK",
    "AMP", "AME", "AMGN", "APH", "ADI", "AON", "APA", "APO", "AAPL", "AMAT",
    "APP", "APTV", "ACGL", "ADM", "ARES", "ANET", "AJG", "AIZ", "T", "ATO",
    "ADSK", "ADP", "AZO", "AVY", "AXON", "BKR", "BALL", "BAC", "BAX", "BDX",
    "BRK-B", "BBY", "TECH", "BIIB", "BLK", "BX", "XYZ", "BE", "BNY", "BA",
    "BKNG", "BSX", "BMY", "AVGO", "BR", "BRO", "BF-B", "BG", "BXP", "CHRW",
    "CDNS", "CPT", "COF", "CAH", "CCL", "CARR", "CVNA", "CASY", "CAT", "CBOE",
    "CBRE", "CDW", "COR", "CNC", "CNP", "CF", "CRL", "SCHW", "CHTR", "CVX",
    "CMG", "CB", "CHD", "CIEN", "CI", "CINF", "CTAS", "CSCO", "C", "CFG",
    "CLX", "CME", "CMS", "KO", "CTSH", "COHR", "COIN", "CL", "CMCSA", "FIX",
    "COP", "ED", "STZ", "CEG", "COO", "CPRT", "GLW", "CPAY", "CSGP", "COST",
    "CRH", "CRWD", "CCI", "CSX", "CMI", "CVS", "DHR", "DRI", "DDOG", "DVA",
    "DECK", "DE", "DELL", "DAL", "DVN", "DXCM", "FANG", "DLR", "DG", "DLTR",
    "D", "DPZ", "DASH", "DOV", "DOW", "DHI", "DTE", "DUK", "DD", "ETN",
    "EBAY", "ECHO", "ECL", "EIX", "EW", "ELV", "EME", "EMR", "ETR", "EOG",
    "EQT", "EFX", "EQIX", "ERIE", "ESS", "EL", "EG", "EVRG", "P", "ES",
    "EXC", "EXE", "EXPE", "EXPD", "EXR", "XOM", "FFIV", "FDS", "FICO", "FAST",
    "FRT", "FDX", "FDXF", "FERG", "FIS", "FITB", "FSLR", "FE", "FISV", "FLEX",
    "F", "FTNT", "FTV", "FOXA", "FOX", "BEN", "FCX", "GRMN", "IT", "GE",
    "GEHC", "GEV", "GEN", "GNRC", "GD", "GIS", "GM", "GPC", "GILD", "GPN",
    "GL", "GDDY", "GS", "HAL", "HIG", "HAS", "HCA", "DOC", "HSIC", "HSY",
    "HPE", "HLT", "HD", "HONA", "HON", "HRL", "HST", "HWM", "HPQ", "HUBB",
    "HUM", "HBAN", "HII", "IBM", "IEX", "IDXX", "ITW", "ILMN", "INCY", "IR",
    "PODD", "INTC", "IBKR", "ICE", "IFF", "IP", "INTU", "ISRG", "IVZ", "INVH",
    "IQV", "IRM", "JBHT", "JBL", "JKHY", "J", "JNJ", "JCI", "JPM", "KDP",
    "KEY", "KEYS", "KMB", "KIM", "KMI", "KKR", "KLAC", "KHC", "KR", "LHX",
    "LH", "LRCX", "LVS", "LDOS", "LEN", "LII", "LLY", "LIN", "LYV", "LMT",
    "L", "LOW", "LULU", "LITE", "LYB", "MTB", "MPC", "MAR", "MRSH", "MLM",
    "MRVL", "MAS", "MA", "MKC", "MCD", "MCK", "MDT", "MRK", "META", "MET",
    "MTD", "MGM", "MCHP", "MU", "MSFT", "MAA", "MRNA", "MDLZ", "MPWR", "MNST",
    "MCO", "MS", "MOS", "MSI", "MSCI", "NDAQ", "NTAP", "NFLX", "NEM", "NWSA",
    "NWS", "NEE", "NKE", "NI", "NDSN", "NSC", "NTRS", "NOC", "NCLH", "NRG",
    "NUE", "NVDA", "NVR", "NXPI", "ORLY", "OXY", "OKTA", "ODFL", "OMC", "ON",
    "OKE", "ORCL", "OTIS", "PCAR", "PKG", "PLTR", "PANW", "PH", "PAYX", "PYPL",
    "PNR", "PEP", "PFE", "PCG", "PM", "PSX", "PNW", "PNC", "PPG", "PPL",
    "PFG", "PG", "PGR", "PLD", "PRU", "PTC", "PEG", "PSA", "PHM", "Q",
    "QCOM", "PWR", "DGX", "RL", "RJF", "RDDT", "O", "REG", "REGN", "RF",
    "RSG", "RMD", "RVTY", "HOOD", "ROK", "ROL", "ROP", "ROST", "RCL", "RTX",
    "SPGI", "CRM", "SNDK", "SBAC", "SLB", "STX", "SRE", "NOW", "SHW", "SPG",
    "SKYD", "SWKS", "SJM", "SW", "SNA", "SOLV", "SO", "LUV", "SWK", "SBUX",
    "STT", "STLD", "STE", "SYK", "SMCI", "SYF", "SNPS", "SYY", "TMUS", "TROW",
    "TTWO", "TPR", "TRGP", "TGT", "TEL", "TDY", "TER", "TSLA", "TXN", "TPL",
    "TXT", "TMO", "TJX", "TKO", "TSCO", "TT", "TDG", "TRV", "TRMB", "TFC",
    "TWLO", "TYL", "TSN", "USB", "UBER", "UDR", "ULTA", "UNP", "UAL", "UPS",
    "URI", "UNH", "UHS", "VLO", "VEEV", "VTR", "VLTO", "VRSN", "VRSK", "VZ",
    "VRTX", "VRT", "VTRS", "VICI", "V", "VST", "VMRK", "VMC", "VYLR", "WRB",
    "GWW", "WAB", "WMT", "DIS", "WM", "WAT", "WEC", "WFC", "WELL", "WST",
    "WDC", "WY", "WSM", "WMB", "WTW", "WDAY", "WYNN", "XEL", "XYL", "YUM",
    "ZBRA", "ZBH", "ZTS",
)


_SP500_NAME_OVERRIDES = {}  # 폴백에는 회사명이 없다(티커를 이름으로 쓴다)

# --- 캐시 -------------------------------------------------------------------
_TABLE_LOCK = threading.Lock()
# {"ts": float, "rows": [{"symbol","name","exchange"}], "source": "wikipedia"|"fallback"}
_TABLE_CACHE: dict = {"ts": 0.0, "rows": [], "source": ""}

_SNAPSHOT_LOCK = threading.Lock()
_SCAN_LOCK = threading.Lock()
# {"ts": float, "rows": [지표 dict], "universe": int, "skipped": int}
_SNAPSHOT: dict = {"ts": 0.0, "rows": [], "universe": 0, "skipped": 0}


# --- 종목 목록 ---------------------------------------------------------------

def _wiki_rows() -> list[dict]:
    """Wikipedia에서 S&P 500 표를 읽는다. 실패하면 빈 리스트(예외는 부르는 쪽에서 처리)."""
    resp = requests.get(WIKI_URL, timeout=WIKI_TIMEOUT,
                        headers={"User-Agent": "Mozilla/5.0 (stock-ai-scanner)"})
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    if not tables:
        return []
    df = tables[0]
    rows = []
    for _, r in df.iterrows():
        symbol = str(r.get("Symbol", "")).strip().upper().replace(".", "-")
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "name": str(r.get("Security", symbol)),
            "exchange": str(r.get("GICS Sector", "")),
        })
    return rows


def _fallback_rows() -> list[dict]:
    return [{"symbol": sym, "name": sym, "exchange": ""} for sym in FALLBACK_TICKERS]


def get_sp500_table(force_refresh: bool = False) -> dict:
    """S&P 500 목록을 `{"rows": [...], "source": ...}`로 돌려준다(6시간 캐시).

    Wikipedia가 느리거나 막혀도 내장 폴백 목록으로 항상 채워진다. 직전 성공값이
    있으면 만료 후 조회 실패 때도 그 값을 계속 쓴다(빈 목록으로 떨어지지 않게).
    """
    now = time.time()
    with _TABLE_LOCK:
        cached = dict(_TABLE_CACHE)
    if not force_refresh and cached["rows"] and now - cached["ts"] < TICKER_LIST_TTL:
        return {"rows": list(cached["rows"]), "source": cached["source"]}

    rows: list[dict] = []
    try:
        rows = _wiki_rows()
    except Exception as exc:  # 네트워크·파싱·차단 모두 여기서 흡수
        logger.warning("S&P 500 목록(Wikipedia) 실패 → 내장 폴백 사용: %s: %s",
                       type(exc).__name__, str(exc)[:160])

    if rows:
        with _TABLE_LOCK:
            _TABLE_CACHE.update(ts=now, rows=rows, source="wikipedia")
        return {"rows": list(rows), "source": "wikipedia"}

    if cached["rows"]:  # 만료됐지만 이번 조회도 실패 → 직전 성공값 유지
        return {"rows": list(cached["rows"]), "source": cached["source"] or "cache"}

    rows = _fallback_rows()
    with _TABLE_LOCK:
        _TABLE_CACHE.update(ts=now, rows=rows, source="fallback")
    return {"rows": rows, "source": "fallback"}


def get_sp500_tickers(force_refresh: bool = False) -> list[str]:
    """스캔에 쓸 티커 목록(중복 제거, Yahoo 형식)."""
    seen: set[str] = set()
    out: list[str] = []
    for row in get_sp500_table(force_refresh=force_refresh)["rows"]:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym or not re.fullmatch(r"[A-Z0-9.\-]{1,10}", sym) or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


# --- 배치 다운로드 -----------------------------------------------------------

def _chunks(items: list[str], size: int = CHUNK_SIZE) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _download_batch(symbols: list[str]):
    """한 청크를 6개월 일봉으로 받는다. 실패해도 예외를 올리지 않고 None."""
    if not symbols:
        return None
    try:
        return yf.download(
            list(symbols),
            period="6mo",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
            timeout=DOWNLOAD_TIMEOUT,
        )
    except Exception as exc:  # 429·네트워크·빈 응답 모두 여기서 흡수
        logger.warning("yf.download 청크 실패(%d종목) → %s: %s",
                       len(symbols), type(exc).__name__, str(exc)[:160])
        return None


def _sub_frame(batch, symbol: str):
    """배치 DataFrame에서 한 종목의 OHLCV 프레임을 꺼낸다(없으면 None)."""
    if batch is None or getattr(batch, "empty", True):
        return None
    try:
        if isinstance(batch.columns, pd.MultiIndex):
            first = batch.columns.get_level_values(0)
            if symbol not in first:
                return None
            frame = batch[symbol]
            if isinstance(frame, pd.Series):  # 이론상 단일 컬럼만 남은 경우
                return None
        else:
            frame = batch
        if "Close" not in frame.columns:
            return None
        return frame
    except (KeyError, IndexError, TypeError):
        return None


def _row_from_frame(symbol: str, frame) -> dict | None:
    """한 종목 프레임 → 지표 dict. 데이터가 짧거나 NaN뿐이면 None."""
    if frame is None or getattr(frame, "empty", True):
        return None
    needed = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in frame.columns]
    try:
        frame = frame[needed].apply(pd.to_numeric, errors="coerce")
    except Exception:
        return None
    frame = frame.dropna(subset=["Close"])
    if len(frame) < MIN_BARS:
        return None
    close = frame["Close"].astype("float64")
    price = float(close.iloc[-1])
    if not math.isfinite(price) or price <= 0:
        return None

    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    ma60 = float(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else None
    if ma20 is not None and not math.isfinite(ma20):
        ma20 = None
    if ma60 is not None and not math.isfinite(ma60):
        ma60 = None

    trend = "판단불가"
    if ma20 and ma60:
        if price > ma20 > ma60:
            trend = "상승 추세(정배열: 현재가>20일선>60일선)"
        elif price < ma20 < ma60:
            trend = "하락 추세(역배열: 현재가<20일선<60일선)"
        else:
            trend = "횡보/혼조"

    row = {
        "ticker": symbol,
        "price": round(price, 2),
        "ma20": None if ma20 is None else round(ma20, 2),
        "ma60": None if ma60 is None else round(ma60, 2),
        "rsi14": stock_tools._calc_rsi(close, 14),
        "trend": trend,
    }
    row.update(stock_tools.compute_advanced_indicators(frame))
    return row


def _scan_universe(tickers: list[str] | None = None) -> dict:
    """500종목을 청크로 훑어 지표 스냅샷을 만든다(네트워크는 yf.download만)."""
    symbols = list(tickers) if tickers is not None else get_sp500_tickers()
    rows: list[dict] = []
    skipped: list[str] = []
    for chunk in _chunks(symbols):
        batch = _download_batch(chunk)
        for sym in chunk:
            row = _row_from_frame(sym, _sub_frame(batch, sym))
            if row is None:
                skipped.append(sym)
            else:
                rows.append(row)
        del batch
        gc.collect()  # 청크 DataFrame을 바로 버려 RSS를 낮게 유지한다
    logger.info("프리 스캔: %d/%d 종목 지표 산출(건너뜀 %d)",
                len(rows), len(symbols), len(skipped))
    return {"rows": rows, "universe": len(symbols), "skipped": len(skipped)}


# --- 스냅샷 캐시 -------------------------------------------------------------

def _fresh(snapshot: dict, now: float) -> bool:
    return bool(snapshot.get("rows")) and (now - float(snapshot.get("ts") or 0)) < PRESCAN_TTL


def prescan(force_refresh: bool = False) -> dict:
    """500종목 지표 스냅샷. 1시간 캐시(스레드 안전), 동시 요청은 한 번만 스캔한다.

    반환: {"rows", "universe", "skipped", "ts", "cached", "stale"}
    """
    now = time.time()
    with _SNAPSHOT_LOCK:
        snapshot = dict(_SNAPSHOT)
    if not force_refresh and _fresh(snapshot, now):
        return {**snapshot, "cached": True, "stale": False}

    with _SCAN_LOCK:  # 스캔 직렬화: 동시에 들어온 요청이 500종목을 두 번 받지 않게
        with _SNAPSHOT_LOCK:
            snapshot = dict(_SNAPSHOT)
        now = time.time()
        if not force_refresh and _fresh(snapshot, now):
            return {**snapshot, "cached": True, "stale": False}

        fresh = _scan_universe()
        if fresh["rows"]:
            with _SNAPSHOT_LOCK:
                _SNAPSHOT.update(ts=time.time(), rows=fresh["rows"],
                                 universe=fresh["universe"], skipped=fresh["skipped"])
                stored = dict(_SNAPSHOT)
            return {**stored, "cached": False, "stale": False}

        if snapshot.get("rows"):  # 이번 스캔이 완전히 실패 → 직전 스냅샷 유지
            logger.warning("프리 스캔 실패 → 직전 스냅샷(%d종목) 사용", len(snapshot["rows"]))
            return {**snapshot, "cached": True, "stale": True}
        return {"ts": now, "rows": [], "universe": fresh["universe"], "skipped": fresh["skipped"],
                "cached": False, "stale": False}


# --- 후보 선정 ---------------------------------------------------------------

def rank_rows(rows: list[dict], strategy: str, top_n: int = TOP_N) -> list[dict]:
    """지표 행을 전략별 프리 스캔 점수로 정렬해 상위 top_n개를 돌려준다.

    점수는 `scoring.score_prescan`이 계산한다(재무 지표가 없으므로 value는 품질 조정
    프록시를 쓴다). 동점은 입력 순서(=S&P 500 목록 순서)를 유지한다.
    """
    scored: list[tuple[float, int, dict]] = []
    for idx, row in enumerate(rows or []):
        if not isinstance(row, dict) or not row.get("ticker"):
            continue
        score, signals = scoring.score_prescan(row, strategy)
        enriched = dict(row)
        enriched["prescore"] = score
        enriched["presignals"] = signals
        scored.append((score, idx, enriched))
    scored.sort(key=lambda item: (-item[0], item[1]))
    limit = max(1, int(top_n or TOP_N))
    return [item[2] for item in scored[:limit]]


def select_candidates(criteria: str | None = None, top_n: int = TOP_N,
                      force_refresh: bool = False) -> dict:
    """조건에 맞는 S&P 500 상위 후보를 고른다.

    반환: {"tickers": [...], "candidates": [...], "strategy": "oversold",
           "universe_scanned": 503, "scanned": 498, "skipped": 5,
           "cached": True, "stale": False}
    """
    strategy = scoring.detect_strategy(criteria)
    snapshot = prescan(force_refresh=force_refresh)
    ranking = rank_rows(snapshot["rows"], strategy, top_n=top_n)
    return {
        "tickers": [row["ticker"] for row in ranking],
        "candidates": ranking,
        "strategy": strategy,
        "universe_scanned": snapshot.get("universe", 0),
        "scanned": len(snapshot.get("rows") or []),
        "skipped": snapshot.get("skipped", 0),
        "cached": snapshot.get("cached", False),
        "stale": snapshot.get("stale", False),
    }


def reset_cache() -> None:
    """테스트·수동 초기화용. 스냅샷과 목록 캐시를 모두 비운다."""
    with _SNAPSHOT_LOCK:
        _SNAPSHOT.update(ts=0.0, rows=[], universe=0, skipped=0)
    with _TABLE_LOCK:
        _TABLE_CACHE.update(ts=0.0, rows=[], source="")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = select_candidates("단기 눌림목 반등")
    print(f"전략={result['strategy']} 유니버스={result['universe_scanned']} "
          f"산출={result['scanned']} 캐시={result['cached']}")
    for row in result["candidates"]:
        print(f"  {row['ticker']:6s} {row['prescore']:5.1f} {row['presignals']}")
