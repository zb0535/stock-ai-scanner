import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
import stock_tools
import scoring
import sp500_scanner
import kospi200_scanner
import naver_finance
import history_store
import realtime_quotes
import extended_quotes
import sector_pe
import paper_store
import paper_trading
from finnhub_client import status as finnhub_status

logger = logging.getLogger("api")

MODEL_ID = "gemini-3.5-flash-lite"

# Gemini 호출 상한·예산(밀리초). 모델이 느리거나 503을 받았을 때 재시도로 수십 초를
# 버리지 않도록, 시도당 상한(GEMINI_TIMEOUT_MS)과 AI 단계 전체 예산(GEMINI_TOTAL_BUDGET_MS)을
# 함께 둔다. 예산을 넘긴 뒤의 시도는 아예 보내지 않고 점수 기반 폴백으로 간다.
GEMINI_TIMEOUT_MS = int(os.environ.get("GEMINI_TIMEOUT_MS", "30000"))
GEMINI_FALLBACK_TIMEOUT_MS = int(os.environ.get("GEMINI_FALLBACK_TIMEOUT_MS", "15000"))
GEMINI_TOTAL_BUDGET_MS = int(os.environ.get("GEMINI_TOTAL_BUDGET_MS", "45000"))
# 2순위 모델 — 주 모델이 503(과부하)·타임아웃이면 다른 모델 풀에 한 번 더 물어본다.
# (같은 모델을 반복하면 과부하가 계속되기만 하고, 사용자는 'AI 해설 없음' 카드를 받는다.)
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash").strip()
GEMINI_MAX_ATTEMPTS = 3
# gemini-3 계열 flash는 기본적으로 '생각(thinking)'을 켜고 답한다. 이 분석은 주어진 수치를
# 정리해 JSON으로 옮기는 작업이라 사고에 10~60초를 쓰고도 결과가 나아지지 않았다.
# 2026-11 실서버에서 15종목 배치가 25초 상한을 넘겨 504(DEADLINE_EXCEEDED)로 폴백된 원인이
# 이것이라, 기본값을 '사고 끔'(0)으로 둔다. 필요하면 GEMINI_THINKING_BUDGET=-1(자동).
GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))

# S&P 500 자동 발굴에서 Gemini로 넘길 후보 수.
DISCOVERY_TOP_N = sp500_scanner.TOP_N

# AI 분석(프롬프트)에 뉴스를 넣을 상위 종목 수. 뉴스는 종목당 네트워크 1회라
# 정량 점수 상위 몇 개만 넣고, 나머지는 점수·지표만으로 평가한다(프롬프트 토큰·시간 절약).
NEWS_TOP_N = max(0, int(os.environ.get("NEWS_TOP_N", "5")))

# --- AI 분석 캐시 -------------------------------------------------------------
# Gemini 호출이 응답 시간의 대부분을 차지한다. 같은 (mode·전략·조건·종목) 요청이 10분 안에
# 다시 오면 분석 결과를 재사용하고, **주가는 매번 실시간으로 다시 조회해 덮어쓴다**.
# Gemini 실패로 만든 폴백 분석은 결과가 빈약하니 아주 짧게(60초)만 기억해 다음 호출에서
# 정상 분석을 다시 시도한다.
ANALYSIS_CACHE_TTL = int(os.environ.get("ANALYSIS_CACHE_TTL", "600"))
ANALYSIS_FALLBACK_TTL = int(os.environ.get("ANALYSIS_FALLBACK_TTL", "60"))
_ANALYSIS_MAX_ENTRIES = 64
_ANALYSIS_CACHE: dict[tuple, dict] = {}
_ANALYSIS_LOCK = threading.Lock()

SYSTEM_PROMPT = """너는 한국·미국 주식 차트·재무제표 스크리닝 보조 봇이다.
제공된 데이터에 없는 가격·지표·뉴스를 절대 지어내지 마라.
종목은 서버가 계산한 정량 점수 `quant_score`(0~100, 높을수록 조건 적합) 내림차순이고, 점수 근거는 `signals`다. `strategy`는 사용자 조건의 의도(value=가치, oversold=과매도 반등, momentum=모멘텀, balanced=균형)다.
`required_conditions`는 사용자가 문장에 직접 적은 **필수 조건**이다(예: 상승추세(정배열), 저PER, 재무안정). `quant_score`가 높더라도 필수 조건에 어긋나는 종목은 추천에서 제외하고, 조건을 충족하는 종목 중에서 고르라. 조건 충족 여부는 `signals`(예: `정배열일치` / `역배열(조건밖)` / `정배열미충족`)와 `trend`·이평선 값으로 판단하라. 조건을 충족하는 종목이 3개 미만이면 충족하는 종목을 모두 넣고, 그 다음에야 부족분을 점수 높은 종목으로 채우되 그 이유를 밝혀라.
`industry_pe`는 같은 업종 기준 PER이다. `trailing_pe`/`forward_pe`가 업종 기준보다 낮으면(`업종대비저PER` signal) 절대 수치가 높아도 상대적으로 저평가라고 설명하고, 업종보다 크게 높으면(`업종대비고PER`) 리스크로 짚어라. `industry_pe`가 `null`이면 업종 정보가 없다는 뜻이므로 그 종목은 **업종 비교를 아예 언급하지 마라**(절대 PER만으로 평가하고, 없는 업종 수치·업종 내 위치를 지어내지 마라).
통화는 종목의 `currency`를 따른다(`KRW`→`₩65,400`, `USD`→`$352.45`). 환산·혼용하지 마라. `name`이 있으면 `삼성전자(005930.KS)`처럼 써라.
근거에는 `quant_score`와 `signals`를 수치로 인용하고, 차트 지표(이평선·RSI·MACD·볼린저·거래량 배수·고점 대비 낙폭)와 재무 지표(PER·업종 기준 PER·매출성장률·영업이익률·부채비율), 제공된 뉴스를 엮어라. 부정 신호(고PER·과열 RSI·역배열)는 리스크로 짚어라.
`price_source`가 `naver_realtime`/`finnhub_quote`면 그 가격이 실시간 현재가다.
점수는 참고값이다. 비슷하면 재무·뉴스로 우열을 가려도 되지만, 높은 점수를 이유 없이 빼지 마라.
재무 지표가 `null`이거나 `financials_note`가 붙어도 가격·이평선·RSI는 유효하다 — 있는 수치로 분석하라.
반드시 아래 JSON만 출력하라(다른 키를 추가하지 마라):
{
  "market_note": "후보군 전반 요약 1~2문장",
  "top_picks": [
    {"ticker": "티커", "price": 0.0, "reason": "quant_score·signals·차트+재무+업종 PER 비교+뉴스 근거(수치 포함)", "risk": "주의점 또는 단점"}
  ]
}"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """기동/종료 훅 — S&P 500 스냅샷을 백그라운드에서 미리 채워 둔다.

    기동 직후 바로 스캔하지 않고 8초 쉬어 Render 헬스체크(`GET /`)가 먼저 200을 받게 하고,
    이후 50분마다 다시 채워 TTL(70분)이 만료되기 전에 갈아끼운다. 예열은 데몬 스레드라
    실패해도 서버는 계속 동작하고, 직전 스냅샷을 그대로 서빙한다.
    `SP500_PREWARM=0`이면 예열을 끈다(단위 테스트·로컬 실행).
    """
    sp500_scanner.start_prewarm()
    # 코스피 200은 60초 뒤 시작해 S&P 500(8초 뒤) 첫 스캔과 겹치지 않게 한다(메모리 피크 분리).
    kospi200_scanner.start_prewarm()
    # AI 모의투자: 계좌 스키마를 확인하고, 매일 1회(한국 15:40·미국 06:20 KST) 자동 매매 루프를 건다.
    # DB가 잠시 죽어 있어도 서버는 뜨고, 자동 루프는 5분마다 다시 시도한다(PAPER_AUTO=0으로 끔).
    try:
        paper_store.init_store()
    except Exception as exc:  # 저장소 장애가 기동을 막지 않게
        logger.warning("모의투자 계좌 초기화 실패: %s: %s", type(exc).__name__, str(exc)[:200])
    paper_trading.start_auto(ai_call=_paper_ai_call)
    try:
        yield
    finally:
        paper_trading.stop_auto()
        sp500_scanner.stop_prewarm()
        kospi200_scanner.stop_prewarm()


def _json_safe(value):
    """응답·프롬프트로 나가기 직전의 값을 JSON 호환으로 정화합니다.

    NaN·inf는 JSON 표준에 없고, Starlette은 `allow_nan=False`로 직렬화하므로 하나만
    섞여도 응답 전체가 500이 된다. 넘파이 스칼라·pandas Timestamp도 그대로는 직렬화되지
    않으므로 여기서 float·ISO 문자열로 바꿔 준다.

    dict·list는 재귀하여 복사본을 만들고(입력은 바꾸지 않는다), 그 밖의 값은 그대로 둔다.
    """
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    item = getattr(value, "item", None)  # 넘파이 스칼라
    if callable(item):
        try:
            return _json_safe(item())
        except (TypeError, ValueError):
            return None
    isoformat = getattr(value, "isoformat", None)  # Timestamp·date
    if callable(isoformat):
        try:
            return isoformat()
        except (TypeError, ValueError):
            return str(value)
    return value


class SafeJSONResponse(JSONResponse):
    """NaN·inf가 섞여도 500으로 죽지 않는 JSON 응답.

    근본 수정(종가 빈 행 제거)이 실패해도 API가 평문 500으로 무너지지 않게 하는 마지막
    방어선이다. NaN·inf는 `null`로 나가고, 로그는 남기지 않는다(요청마다 시끄러워지지 않게).
    """

    def render(self, content) -> bytes:
        return super().render(_json_safe(content))


app = FastAPI(title="한국·미국 주식 AI 스크리너 API", lifespan=lifespan,
              default_response_class=SafeJSONResponse)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 처리되지 않은 예외 기록 --------------------------------------------------
# 예전에는 500이 나면 Render 로그를 뒤지지 않고는 이유를 알 수 없었다. 이제는
# `error_id`가 로그·응답에 찍히고 최근 오류는 `GET /`(`errors_recent`)에서 바로 보인다.
_ERRORS_LOCK = threading.Lock()
_ERRORS_RECENT: list[dict] = []
_ERRORS_MAX = 10


def _record_error(error_id: str, path: str, exc: Exception, note: str = "") -> None:
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "error_id": error_id,
        "path": path,
        "type": type(exc).__name__,
        "message": (str(exc) or "")[:300],
    }
    if note:
        entry["note"] = note
    with _ERRORS_LOCK:
        _ERRORS_RECENT.insert(0, entry)
        del _ERRORS_RECENT[_ERRORS_MAX:]


def recent_errors() -> list[dict]:
    """최근 처리되지 않은 예외(최신 순, 최대 ERRORS_MAX개)의 사본."""
    with _ERRORS_LOCK:
        return [dict(item) for item in _ERRORS_RECENT]


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """500을 평문 대신 JSON으로 돌리고 구조화 로그를 남긴다.

    응답: `{"detail": "internal_error", "error_id": "..."}` — 로그의 같은 error_id와
    짝지어 원인을 추적한다(내부 예외 내용은 클라이언트에 노출하지 않는다).
    """
    error_id = uuid.uuid4().hex[:8]
    logger.exception("처리되지 않은 예외 [%s] %s %s → %s: %s", error_id, request.method,
                     request.url.path, type(exc).__name__, str(exc)[:300])
    _record_error(error_id, f"{request.method} {request.url.path}", exc)
    return SafeJSONResponse(status_code=500,
                            content={"detail": "internal_error", "error_id": error_id})


class ScreenRequest(BaseModel):
    """스크리닝 요청.

    `mode`는 추가 필드다(기본 "watchlist").
    - "watchlist": `tickers`(없으면 기본 관심 종목 15개) 안에서 고른다. 기존 동작 그대로.
    - "sp500": S&P 500 약 500종목을 기술적으로 먼저 훑어 조건에 맞는 상위 15개를 자동
      발굴한 뒤, 그 15개에만 기존 3단 재무 파이프라인 + 뉴스 + Gemini를 적용한다.
    - "kospi200": 코스피 200 약 200종목을 같은 방식으로 훑어 상위 15개를 발굴한다.
    """

    criteria: str
    tickers: list[str] | None = None
    mode: str = "watchlist"


class TrackRequest(BaseModel):
    """수동 추적 요청(`POST /api/performance/track`).

    `entry_price`는 선택이다. 비었거나 0 이하면 서버가 실시간 현재가를 먼저 받아 채우고
    (네이버·Finnhub), 실시간가를 못 받을 때만 최신 일봉 종가로 폴백한다 — 그마저 실패하면
    400(잘못된 티커)이다.
    """

    ticker: str
    entry_price: float | None = None
    strategy: str = ""
    mode: str = "watchlist"
    quant_score: float | None = None
    criteria: str = ""


# 티커 허용 문자([A-Z0-9.-^=], 최대 12자) — 경로·쿼리 삽입을 막는다.
_TICKER_RE = re.compile(r"^[A-Z0-9.\-^=]{1,12}$")


# --- 뉴스 캐시: 종목별 30분 (429/401 방지) -----------------------------------
_NEWS_TTL = 1800
_NEWS_TTL_EMPTY = 60
_NEWS_FETCH_LIMIT = 5
# ticker -> (저장 시각, 유지 시간, 제목 목록)
_NEWS_CACHE: dict[str, tuple[float, float, list[str]]] = {}
# 병렬 스크리닝에서 여러 스레드가 동시에 뉴스를 조회하므로 캐시는 락으로 보호한다.
_NEWS_LOCK = threading.Lock()


def _get_recent_news(ticker: str, limit: int = 3) -> list[str]:
    sym = (ticker or "").strip().upper()
    if not sym:
        return []
    now = time.time()
    with _NEWS_LOCK:
        cached = _NEWS_CACHE.get(sym)
    if cached and now - cached[0] < cached[1]:
        return list(cached[2][:limit])

    titles: list[str] = []
    if naver_finance.is_korean_ticker(sym):
        # 한국 종목: yfinance 뉴스는 영문 위주라 네이버 종목 뉴스(한국어)를 먼저 쓴다.
        try:
            titles = naver_finance.get_korean_news(sym, limit=_NEWS_FETCH_LIMIT)
        except Exception:
            titles = []
    if not titles:
        # Yahoo 뉴스는 벽시계 타임아웃(5초)+전역 회로 차단기를 거친다 — 막힌 서버에서는
        # 두 번 실패 뒤 30분 동안 아예 호출하지 않아 뉴스가 배치를 느리게 만들지 않는다.
        titles = stock_tools.fetch_yahoo_news(sym, limit=_NEWS_FETCH_LIMIT)

    with _NEWS_LOCK:
        _NEWS_CACHE[sym] = (now, _NEWS_TTL if titles else _NEWS_TTL_EMPTY, titles)
    return list(titles[:limit])


def _yahoo_guard() -> dict:
    """`GET /`용 Yahoo 호출 가드 상태 — t.info·뉴스 전역 우회와 프리 적재된 일봉 캐시."""
    return {"info": stock_tools.info_breaker_status(),
            "news": stock_tools.news_breaker_status(),
            "history_cache": stock_tools.history_cache_status()}


def _attach_news(row: dict) -> dict:
    """한 행에 최근 뉴스(2건)를 붙인다 — 실패해도 행은 그대로 둔다."""
    try:
        row["news"] = _get_recent_news(row.get("ticker"), limit=2)
    except Exception as exc:
        logger.warning("%s: 뉴스 조회 실패 → %s: %s", row.get("ticker"), type(exc).__name__, str(exc)[:120])
        row["news"] = []
    return row


def _attach_news_top(rows: list[dict], top_n: int = NEWS_TOP_N) -> list[dict]:
    """정량 점수 상위 N개(입력 순서 = 점수 내림차순)에만 뉴스를 병렬로 붙인다.

    예전에는 15종목 모두 뉴스를 조회했지만, 프롬프트에 들어가는 것은 상위 후보뿐이라
    나머지는 뉴스 빈 배열(`[]`)로 두어 네트워크·토큰을 아낀다(점수·지표는 그대로 유지).
    """
    if top_n <= 0:
        for row in rows or []:
            if isinstance(row, dict):
                row["news"] = []
        return rows
    targets = [row for row in (rows or [])
               if isinstance(row, dict) and row.get("ticker") and not row.get("error")][:top_n]
    if targets:
        stock_tools.bounded_map(_attach_news, targets)
    return rows


# 프롬프트에 넣는 종목 필드(서버가 계산한 값만). 내부 진단 필드·빈 뉴스는 빼서
# 입력 토큰을 줄인다 — 생성 시간은 프롬프트 길이에 비례해 늘어난다.
_PROMPT_ROW_FIELDS = (
    "ticker", "name", "currency", "price", "price_source", "ma20", "ma60", "rsi14", "trend",
    "macd", "macd_signal", "macd_hist", "macd_cross", "macd_state",
    "bb_upper", "bb_lower", "bb_pct_b", "volume_ratio", "drawdown_from_high_pct",
    "range_position_pct", "trailing_pe", "forward_pe", "sector", "industry_pe",
    "revenue_growth", "operating_margins",
    "debt_to_equity", "quant_score", "signals", "news", "financials_note",
)


def _compact_rows(rows: list[dict]) -> list[dict]:
    """프롬프트용 종목 요약 — 필요한 필드만 남기고 빈 뉴스는 생략한다."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("error"):
            out.append({"ticker": row.get("ticker"), "error": row.get("error")})
            continue
        compact = {key: row[key] for key in _PROMPT_ROW_FIELDS if key in row}
        if not compact.get("news"):
            compact.pop("news", None)
        out.append(compact)
    return out


def _analysis_key(mode: str, strategy: str, criteria: str, tickers: list[str]) -> tuple:
    """AI 분석 캐시 키 — 같은 조건·같은 종목 묶음일 때만 재사용한다.

    종목 순서가 달라도 같은 묶음이면 같은 키가 되도록 정렬하고, 공백만 다른 조건도
    같은 요청으로 본다. `criteria`를 키에 넣는 이유: 전략이 같아도 조건 문장이 다르면
    사용자가 기대하는 해설도 달라진다.
    """
    normalized = re.sub(r"\s+", " ", str(criteria or "")).strip()
    return (mode, strategy, normalized, tuple(sorted({str(t).strip().upper() for t in tickers or []})))


def _analysis_cache_get(key: tuple) -> dict | None:
    """캐시 히트면 `{data, source, model, age_sec}` 사본, 아니면 None."""
    now = time.time()
    with _ANALYSIS_LOCK:
        entry = _ANALYSIS_CACHE.get(key)
        if not entry:
            return None
        if now - entry["at"] >= entry["ttl"]:
            _ANALYSIS_CACHE.pop(key, None)
            return None
        return {"data": json.loads(json.dumps(entry["data"], ensure_ascii=False)),
                "source": entry["source"], "model": entry["model"],
                "age_sec": round(now - entry["at"], 1)}


def _analysis_cache_put(key: tuple, data: dict, source: str, model: str | None) -> None:
    """분석 결과를 기억한다 — Gemini 결과만 길게(기본 600초), 폴백은 짧게(60초)."""
    ttl = ANALYSIS_CACHE_TTL if source == "gemini" else ANALYSIS_FALLBACK_TTL
    if ttl <= 0:
        return
    try:
        snapshot = json.loads(json.dumps(data, ensure_ascii=False))
    except (TypeError, ValueError):  # 직렬화 불가한 값이 섞였으면 캐시하지 않는다
        return
    with _ANALYSIS_LOCK:
        _ANALYSIS_CACHE[key] = {"at": time.time(), "ttl": ttl, "data": snapshot,
                                "source": source, "model": model}
        if len(_ANALYSIS_CACHE) > _ANALYSIS_MAX_ENTRIES:
            oldest = sorted(_ANALYSIS_CACHE.items(), key=lambda kv: kv[1]["at"])[:len(_ANALYSIS_CACHE) - _ANALYSIS_MAX_ENTRIES]
            for old_key, _ in oldest:
                _ANALYSIS_CACHE.pop(old_key, None)


def clear_analysis_cache() -> None:
    """테스트·수동 초기화용."""
    with _ANALYSIS_LOCK:
        _ANALYSIS_CACHE.clear()


def analysis_cache_status() -> dict:
    """`GET /` 진단용 — 캐시 개수·TTL(내용·키는 노출하지 않는다)."""
    now = time.time()
    with _ANALYSIS_LOCK:
        alive = sum(1 for e in _ANALYSIS_CACHE.values() if now - e["at"] < e["ttl"])
        total = len(_ANALYSIS_CACHE)
    return {"entries": alive, "stale_entries": total - alive,
            "ttl_sec": ANALYSIS_CACHE_TTL, "fallback_ttl_sec": ANALYSIS_FALLBACK_TTL}


@app.api_route("/", methods=["GET", "HEAD"])
def health():
    """헬스 체크. UptimeRobot 무료 모니터는 HEAD로 찔러오므로 HEAD에도 200을 준다.

    `build`는 Render가 주는 배포 커밋(RENDER_GIT_COMMIT) 앞 7자다. 어느 커밋이
    실제로 떠 있는지 바로 확인할 수 있어 배포 검증에 쓴다(로컬 실행 시 "local").
    `finnhub`은 키 설정 여부·마지막 실패 사유만 노출한다(키 본문은 응답에 없다).
    `sp500_cache`는 S&P 500 프리 스캔 스냅샷 상태(예열 여부·종목 수·나이)이고,
    `kospi200_cache`는 코스피 200 스냅샷 상태다. `yahoo_guard`는 Yahoo t.info·뉴스의
    전역 우회 상태와 프리 스캔이 미리 적재한 일봉 캐시 개수다. 이 응답은 스캔 중에도 즉시
    돌아온다 — 락은 dict 복사 순간만 잡는다.
    """
    return {"status": "ok", "build": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or "local",
            "finnhub": finnhub_status(),
            "storage_backend": history_store.storage_backend(),
            "sp500_cache": sp500_scanner.cache_status(),
            "sp500_fund_prewarm": sp500_scanner.fundamental_prewarm_status(),
            "kospi200_cache": kospi200_scanner.cache_status(),
            "kospi200_fund_prewarm": kospi200_scanner.fundamental_prewarm_status(),
            "analysis_cache": analysis_cache_status(),
            "sector_pe_samples": sector_pe.sample_status(),
            "realtime": {"enabled": realtime_quotes.is_enabled(),
                         "ttl_sec": realtime_quotes.REALTIME_TTL},
            "extended_quotes": extended_quotes.status(),
            "paper_auto": paper_trading.auto_status(),
            "yahoo_guard": _yahoo_guard(),
            "errors_recent": recent_errors()}


@app.api_route("/api/watchlist", methods=["GET", "HEAD"])
def get_watchlist():
    return {"tickers": stock_tools.get_watchlist()}

@app.get("/api/sp500")
def get_sp500():
    """S&P 500 목록(웹 종목 선택 모달용).

    목록 로딩은 `sp500_scanner`가 맡는다 — Wikipedia 조회에 8초 타임아웃을 걸고, 실패하면
    내장 폴백 목록(503개)을 쓰므로 이 엔드포인트가 빈 목록으로 떨어지지 않는다.
    응답 형태(`{"results": [{symbol, name, exchange}]}`)는 기존과 같다.
    """
    return {"results": sp500_scanner.get_sp500_table()["rows"]}


@app.get("/api/kospi200")
def get_kospi200():
    """코스피 200 구성종목 목록 — 네이버 라이브(6시간 캐시) → 내장 201종목 폴백.

    응답: `{"results": [{symbol, name, exchange}], "source": "naver"|"fallback"|"cache", "count": n}`
    """
    table = naver_finance.get_kospi200_table()
    return {"results": table["rows"], "source": table["source"], "count": len(table["rows"])}


@app.get("/api/resolve-ticker")
def resolve_ticker(q: str = ""):
    """이름·코드·티커를 야후 티커로 해석한다(관심 종목 입력용).

    - `삼성전자` → `005930.KS`, `에코프로` → `086520.KQ`, `000660` → `000660.KS`
    - `NAVER`(한국 종목명) → `035420.KS`, `AAPL`/`NVDA`(미국) → 그대로 통과
    응답: `{"query", "ticker", "name", "exchange", "currency", "source", "candidates"}`
    찾지 못하면 404 `not_found`.
    """
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query_required")
    resolved = naver_finance.resolve_ticker(query)
    if not resolved:
        raise HTTPException(status_code=404, detail="not_found")
    return {"query": query, **resolved}


@app.get("/api/search")
def search_stocks(q: str = ""):
    if not q.strip():
        return {"results": []}
    results = []
    # 한글 이름은 네이버 자동완성으로 먼저 해석한다(yfinance 검색은 한글에 약하다).
    if re.search(r"[가-힣]", q):
        try:
            resolved = naver_finance.resolve_ticker(q)
        except Exception:
            resolved = None
        if resolved:
            results.append({"symbol": resolved["ticker"],
                            "name": resolved.get("name") or resolved["ticker"],
                            "exchange": resolved.get("exchange") or ""})
    korean = results
    try:
        quotes = yf.Search(q.strip(), max_results=12).quotes or []
        results = [
            {
                "symbol": item["symbol"],
                "name": item.get("shortname") or item.get("longname") or item["symbol"],
                "exchange": item.get("exchDisp") or item.get("exchange") or "",
            }
            for item in quotes
            if item.get("symbol") and item.get("quoteType") in ("EQUITY", "ETF", None)
        ]
        return {"results": results + korean}
    except Exception:
        return {"results": korean}


def _screen_row(ticker: str) -> dict:
    """한 종목의 차트·재무 지표와 최근 뉴스를 묶어 한 행으로 만듭니다.

    티커 하나의 사고가 전체 스크리닝을 500으로 만들지 않도록, 지표·뉴스
    조회의 예외는 이 행에서 흡수하고 `error` 행으로 돌려준다(나머지 종목은 그대로 본다).
    """
    try:
        metrics = stock_tools.get_stock_metrics(ticker)
    except Exception as exc:
        logger.warning("%s: 지표 조회 실패 → %s: %s", ticker, type(exc).__name__, str(exc)[:200])
        metrics = {"ticker": ticker, "error": "주가 데이터를 찾을 수 없습니다."}
    if not isinstance(metrics, dict):  # 계약을 어기는 응답도 한 행만 망가지게
        metrics = {"ticker": ticker, "error": "주가 데이터를 찾을 수 없습니다."}
    # 뉴스는 여기서 부르지 않는다 — 정량 점수를 매긴 뒤 상위 몇 개에만 붙인다(`_attach_news_top`).
    metrics.setdefault("news", [])
    return _json_safe(metrics)


def _attach_chart(pick: dict) -> dict:
    """추천 종목 하나에 6개월 차트(가격·20일·60일선)와 뉴스를 붙입니다."""
    try:
        hist = stock_tools.get_history(pick["ticker"], "6mo")
    except Exception as exc:  # 차트 하나 때문에 전체 응답이 죽지 않게
        logger.warning("%s: 차트 조회 실패 → %s: %s", pick.get("ticker"), type(exc).__name__, str(exc)[:120])
        hist = None
    close = pd.Series(dtype=float) if hist is None or hist.empty else hist["Close"]
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    recent = pd.DataFrame({"price": close, "ma20": ma20, "ma60": ma60}).tail(60)
    pick["chart"] = [
        {
            "date": idx.strftime("%m/%d"),
            "price": None if pd.isna(r["price"]) else round(float(r["price"]), 2),
            "ma20": None if pd.isna(r["ma20"]) else round(float(r["ma20"]), 2),
            "ma60": None if pd.isna(r["ma60"]) else round(float(r["ma60"]), 2),
        }
        for idx, r in recent.iterrows()
    ]
    try:
        pick["news"] = _get_recent_news(pick["ticker"], limit=3)
    except Exception:
        pick["news"] = []
    return pick


def _parse_analysis(text: str) -> dict:
    """Gemini 응답 텍스트를 JSON dict로 해석한다(펜스·앞뒤 잡문 허용)."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    if not raw:
        raise ValueError("빈 응답")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 모델이 설명을 붙여 보낸 경우 첫 JSON 객체만 뽑아 다시 시도한다.
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"JSON 객체가 아님: {type(data).__name__}")
    return data


def _fallback_analysis(rows: list[dict], note: str, intents=None) -> dict:
    """Gemini를 쓸 수 없을 때 서버 정량 점수만으로 만드는 분석(200 유지).

    점수·점수 근거(signals)는 모두 서버가 계산한 값이라 지어낸 내용이 없다.
    세부 조건(`intents`)이 추세를 요구하면 그 조건을 충족하는 종목을 먼저 채운다 — AI 없이도
    `"상승추세"`를 요구한 요청이 역배열 종목을 1순위로 내놓지 않게. 충족 종목이 하나도 없으면
    점수 순서로 채우되 그 사실을 `market_note`에 밝힌다(지어내지 않는다).
    """
    usable = [row for row in rows or []
              if isinstance(row, dict) and not row.get("error") and row.get("price") is not None]
    direction = scoring.requires_trend(intents)
    ordered = usable
    missing_note = ""
    if direction:
        matched = [row for row in usable if scoring.trend_state(row) == direction]
        if matched:
            ordered = matched + [row for row in usable if row not in matched]
        else:
            label = scoring.INTENT_LABELS.get("uptrend" if direction == "up" else "downtrend", direction)
            missing_note = f" 조건({label})을 충족하는 종목이 후보에 없어 점수 순위대로 제시합니다."

    picks = []
    for row in ordered:
        if len(picks) >= 3:
            break
        signals = row.get("signals") or []
        picks.append({
            "ticker": row.get("ticker"),
            "price": row.get("price"),
            "reason": (f"서버 정량 점수 quant_score {row.get('quant_score')} — "
                       + (", ".join(str(s) for s in signals[:5]) if signals else "제공된 지표 기준")),
            "risk": "AI 해설 미제공(모델 호출 실패) — 서버가 계산한 점수·신호만 반영",
        })
    return {"market_note": (note or "") + missing_note, "top_picks": picks}


def _gemini_client(timeout_ms: int | None = None):
    """Gemini 클라이언트 — 요청당 타임아웃을 고정한다(느린 모델 하나가 배치를 잡아먹지 않게).

    google-genai가 `http_options`를 모르는 예전 버전이면 그냥 기본 클라이언트를 쓴다
    (해설이 조금 느려질 뿐, 스크리닝은 그대로 돈다).
    """
    timeout = GEMINI_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    try:
        return genai.Client(http_options=types.HttpOptions(
            timeout=timeout,
            retry_options=types.HttpRetryOptions(attempts=1),  # 재시도는 우리 루프가 맡는다
        ))
    except TypeError:  # 구버전 google-genai
        return genai.Client()


def _gemini_model_plan() -> list[tuple[str, int]]:
    """시도할 (모델, 시도당 상한 ms) 목록.

    주 모델은 한 번만, 넉넉하게(30초) 부른다 — 과부하 상황에서 정상 호출이 20~25초 걸리는
    것을 실서버에서 확인했고, 같은 모델을 두 번 부르면 늦은 호출에 값을 두 번 치른다.
    주 모델이 503·타임아웃이면 다른 모델 풀(2순위)에 짧게(15초) 한 번 더 물어본다.
    """
    plan = [(MODEL_ID, GEMINI_TIMEOUT_MS)]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != MODEL_ID:
        plan.append((GEMINI_FALLBACK_MODEL, GEMINI_FALLBACK_TIMEOUT_MS))
    return plan[:max(1, GEMINI_MAX_ATTEMPTS)]


def _gemini_config(thinking_budget: int | None) -> types.GenerateContentConfig:
    """분석 호출 설정 — 사고 토큰 예산으로 응답 지연을 통제한다."""
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0,
    )
    if thinking_budget is not None:
        try:
            config.thinking_config = types.ThinkingConfig(thinking_budget=thinking_budget)
        except Exception:  # 사고 설정을 모르는 구버전 SDK — 기본값으로 둔다
            pass
    return config


def _retryable_gemini_error(exc: Exception) -> bool:
    """다시 시도해 나아질 수 있는 실패인지 — 타임아웃(504)은 재시도해도 같다.

    DEADLINE_EXCEEDED는 그 프롬프트로 제한 시간을 넘긴 것이라 다시 보내면 같은 시간이
    걸리거나 더 걸린다. 그 경우는 바로 점수 기반 폴백으로 간다(대기가 두 배가 되지 않게).
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return ("deadline" not in text and "timeout" not in text
            and "timed out" not in text and "504" not in text)


def _thinking_rejected(exc: Exception) -> bool:
    """사고 예산을 거부하는 모델·버전인지(400 INVALID_ARGUMENT) — 그러면 사고 설정을 뺀다."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "thinking" in text or "invalid_argument" in text or " 400" in text


def _run_analysis(prompt: str, rows: list[dict], intents=None) -> tuple[dict, str, str, str | None]:
    """Gemini 분석을 모델 순서대로 시도하고, 끝내 실패하면 서버 점수 기반 폴백을 돌려준다.

    `intents`(세부 조건)는 폴백 경로에만 쓰인다 — 모델이 없을 때도 조건에 맞는 종목이 앞에 온다.

    반환: (analysis dict, analysis_source, analysis_note, model)
    - `analysis_source`: "gemini"(정상) | "fallback"(모델 호출 실패 → 서버 정량 요약)
    - 타임아웃(DEADLINE_EXCEEDED)·503은 다음 모델로 넘어간다 — 같은 모델을 다시 부르면
      같은 지연을 또 기다리거나 같은 과부하를 만난다.
    """
    data = None
    analysis_source = "gemini"
    analysis_note = ""
    thinking_budget = GEMINI_THINKING_BUDGET
    queue = list(_gemini_model_plan())
    deadline = time.time() + GEMINI_TOTAL_BUDGET_MS / 1000.0
    last_error = None
    analysis_model = None
    attempt = 0
    while queue:
        model, budget_ms = queue.pop(0)
        attempt += 1
        remaining_ms = int((deadline - time.time()) * 1000)
        if remaining_ms < 3000:  # 남은 예산으로는 의미 있는 응답을 못 받는다
            analysis_note = "AI 분석 호출이 실패해 서버 정량 점수 기반 요약을 대신 제공합니다."
            break
        try:
            client = _gemini_client(min(budget_ms, remaining_ms))
            res = client.models.generate_content(
                model=model,
                contents=prompt,
                config=_gemini_config(thinking_budget),
            )
            data = _parse_analysis(res.text)
            analysis_model = model
            break
        except Exception as exc:
            logger.warning("Gemini 분석 실패(시도 %d, %s) → %s: %s", attempt, model,
                           type(exc).__name__, str(exc)[:200])
            last_error = exc
            if _thinking_rejected(exc) and thinking_budget is not None:
                # 400 INVALID_ARGUMENT: 이 모델은 사고 예산을 받지 않는다 → 같은 모델을
                # 사고 설정 없이 다시 보낸다(바로 다른 모델로 넘기면 원인을 못 벗는다).
                thinking_budget = None
                queue.insert(0, (model, budget_ms))
                continue
            if _retryable_gemini_error(exc):
                time.sleep(0.3)   # 빨리 돌아온 과부하만 짧게 쉰다
    if data is None:
        if analysis_note == "":
            analysis_note = "AI 분석 호출이 실패해 서버 정량 점수 기반 요약을 대신 제공합니다."
            _record_error(uuid.uuid4().hex[:8], "POST /api/screen (gemini)",
                          last_error if last_error else Exception("gemini_budget_exhausted"),
                          note="폴백 분석으로 응답")
        data = _fallback_analysis(rows, analysis_note, intents=intents)
        analysis_source = "fallback"
    return data, analysis_source, analysis_note, analysis_model


@app.post("/api/screen")
def screen_stocks(req: ScreenRequest):
    """조건으로 종목을 스크리닝하고 AI 해설을 붙인다.

    속도를 위해 세 가지를 함께 쓴다.
    1. 지표·재무(30분 캐시)·일봉(프리 스캔 적재분)은 다시 받지 않고, **현재가만** 실시간으로
       덮어쓴다(`price`, `price_source`).
    2. 뉴스는 정량 점수 상위 `NEWS_TOP_N`종목만 조회한다.
    3. 같은 (mode·전략·조건·종목) 요청은 10분 동안 AI 분석을 재사용한다(`analysis_cached`).
       재사용해도 주가는 매번 새로 조회하므로 화면의 현재가는 항상 최신이다.
    """
    mode = (req.mode or "watchlist").strip().lower()
    if mode not in ("watchlist", "sp500", "kospi200"):
        mode = "watchlist"  # 모르는 값은 기존 동작으로 되돌린다(클라이언트 호환)

    # 단계별 소요 시간(ms). 응답의 `timings`와 로그에 남겨 어느 단계가 느린지 바로 보이게 한다.
    timings: dict[str, int] = {}
    _t0 = time.perf_counter()

    def _mark(name: str, since: float) -> None:
        timings[name] = int((time.perf_counter() - since) * 1000)

    universe_scanned = None
    if mode in ("sp500", "kospi200"):
        started = time.time()
        scanner = sp500_scanner if mode == "sp500" else kospi200_scanner
        universe_label = "S&P 500" if mode == "sp500" else "코스피 200"
        discovery = scanner.select_candidates(req.criteria, top_n=DISCOVERY_TOP_N)
        tickers = discovery["tickers"]
        universe_scanned = discovery["universe_scanned"]
        _mark("discovery_ms", _t0)
        logger.info(
            "%s 발굴: 전략=%s 유니버스=%d 산출=%d 상위=%d 캐시=%s %.1fs %s",
            universe_label, discovery["strategy"], discovery["universe_scanned"],
            discovery["scanned"], len(tickers), discovery["cached"],
            time.time() - started, ",".join(tickers),
        )
        if not tickers:
            # 프리 스캔이 잠깐 비었을 때(모든 다운로드 실패)는 관심 종목으로 안전하게 되돌린다.
            logger.warning("%s 발굴 결과 없음 → 관심 종목으로 대체", universe_label)
            tickers = [t.strip().upper() for t in (req.tickers or stock_tools.get_watchlist())
                       if t.strip()]
            universe_scanned = 0
    else:
        tickers = [t.strip().upper() for t in (req.tickers or stock_tools.get_watchlist()) if t.strip()]

    # 15개 종목을 최대 8개씩 병렬로 당긴다. bounded_map이 입력 순서를 유지해 주므로
    # 점수가 같은 종목의 정렬도 흔들리지 않는다. 뉴스는 여기서 부르지 않는다.
    # 실시간 시세는 **지표 조회와 동시에** 별도 스레드로 받아 둔다 — 서로 기다리지 않아
    # 첫 호출의 합산 지연이 줄어든다(특히 지표 캐시가 비은 재배포 직후).
    _t = time.perf_counter()
    quote_box: dict = {}

    def _load_quotes() -> None:
        started = time.perf_counter()
        try:
            quote_box["quotes"] = realtime_quotes.get_prices(tickers)
        except Exception as exc:  # 실시간 계층 장애 → 일봉 종가 유지
            logger.warning("실시간 시세 조회 실패: %s: %s", type(exc).__name__, str(exc)[:160])
            quote_box["quotes"] = {}
        finally:
            _mark("realtime_ms", started)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="screen") as pool:
        rows_future = pool.submit(stock_tools.bounded_map, _screen_row, tickers,
                                  stock_tools.screen_workers_for(tickers))
        quotes_future = pool.submit(_load_quotes)
        rows = rows_future.result()
        quotes_future.result()
    _mark("rows_ms", _t)
    rows = [_json_safe(row) for row in rows]  # NaN·inf가 프롬프트·응답으로 나가지 않게

    # 실시간 현재가 적용: 한국 네이버·미국 Finnhub 값을 price에 덮어쓴다(위에서 이미 받아 둔 것).
    _t = time.perf_counter()
    rows = stock_tools.apply_realtime_prices(rows, quotes=quote_box.get("quotes") or {})
    _mark("overlay_ms", _t)

    # 동종업계 비교: `sector`가 비었으면 S&P 500 표에서, `industry_pe`가 비었으면 같은 섹터
    # 표본 중앙값으로 채운다(네트워크 호출 없음). 한국 종목은 네이버 업종 PER이 이미 붙어 있다.
    _t = time.perf_counter()
    rows = sector_pe.fill_sector_pe(rows)
    _mark("sector_ms", _t)

    # 정량 스코어링: 조건에서 전략·세부 조건을 읽고 quant_score·signals를 붙여 내림차순 정렬한다.
    # 네트워크 호출은 없다(이미 받은 지표만 사용). 동점은 입력 순서를 유지한다.
    rows, strategy = scoring.rank_stocks(rows, criteria=req.criteria)
    conditions = scoring.detect_intents(req.criteria)

    # 뉴스는 프롬프트에 들어가는 상위 N종목만 — 나머지는 []로 두어 네트워크·토큰을 아낀다.
    _t = time.perf_counter()
    rows = _attach_news_top(rows, NEWS_TOP_N)
    _mark("news_ms", _t)

    universe_note = ""
    if mode == "sp500":
        universe_note = (f"발굴 유니버스: S&P 500 {universe_scanned}종목 중 기술적 사전 스캔 상위 "
                         f"{len(tickers)}종목(관심 종목 목록이 아니다)\n")
    elif mode == "kospi200":
        universe_note = (f"발굴 유니버스: 코스피 200 {universe_scanned}종목 중 기술적 사전 스캔 상위 "
                         f"{len(tickers)}종목(관심 종목 목록이 아니다). 가격은 원화(₩)다.\n")

    # --- AI 분석: 10분 캐시 먼저 확인 ---
    cache_key = _analysis_key(mode, strategy, req.criteria, tickers)
    cached = _analysis_cache_get(cache_key)
    analysis_cached = cached is not None
    analysis_note = ""
    if cached:
        data = cached["data"]
        analysis_source = cached["source"]
        analysis_model = cached["model"]
        timings["gemini_ms"] = 0
        if analysis_source == "fallback":
            analysis_note = "AI 분석 호출이 실패해 서버 정량 점수 기반 요약을 대신 제공합니다."
        logger.info("AI 분석 캐시 히트(%.0f초 전, %s) mode=%s 종목=%d",
                    cached["age_sec"], analysis_source, mode, len(tickers))
    else:
        condition_note = ""
        if conditions:
            labels = [scoring.INTENT_LABELS.get(name, name) for name in conditions]
            condition_note = (
                f"필수 조건(사용자 문장에서 읽음): {', '.join(labels)}\n"
                "이 조건에 어긋나는 종목은 quant_score가 높아도 top_picks에서 제외하라. "
                "`signals`의 '…일치' / '…(조건밖)' / '…미충족' 표시로 충족 여부를 확인할 수 있다.\n"
            )
        prompt = (
            universe_note
            + condition_note
            + f"조건: {req.criteria}\n"
            f"strategy: {strategy} ({scoring.STRATEGY_LABELS.get(strategy, strategy)})\n"
            f"required_conditions: {json.dumps([scoring.INTENT_LABELS.get(n, n) for n in conditions], ensure_ascii=False)}\n\n"
            f"종목 데이터(quant_score 내림차순):\n"
            f"{json.dumps(_compact_rows(rows), ensure_ascii=False, default=str)}"
        )
        _t = time.perf_counter()
        data, analysis_source, analysis_note, analysis_model = _run_analysis(prompt, rows,
                                                                           intents=conditions)
        _mark("gemini_ms", _t)
        # Gemini 결과만 길게 기억한다(폴백은 60초 — 다음 호출에서 정상 분석을 다시 시도).
        _analysis_cache_put(cache_key, data, analysis_source, analysis_model)

    # 추천 종목에도 같은 quant_score·signals를 붙인다(기존 키는 그대로, 추가만).
    score_by_ticker = {
        (row.get("ticker") or "").strip().upper(): row
        for row in rows if isinstance(row, dict) and row.get("ticker")
    }
    picks = [p for p in (data.get("top_picks") or []) if isinstance(p, dict) and p.get("ticker")]
    data["top_picks"] = picks
    for pick in picks:
        row = score_by_ticker.get(str(pick.get("ticker") or "").strip().upper())
        if not row:
            continue  # 모델이 후보에 없는 티커를 냈다 → 지어낸 점수를 붙이지 않는다
        pick.setdefault("quant_score", row.get("quant_score"))
        pick.setdefault("signals", row.get("signals"))
        # 통화·한국어 종목명도 서버 값으로 붙인다(Gemini가 임의로 만들지 않게).
        for field in ("currency", "name"):
            if row.get(field) is not None:
                pick.setdefault(field, row[field])
        # 가격은 서버가 방금 받은 실시간 값으로 덮어쓴다(캐시된 분석·모델이 적은 옛 가격 모두).
        if row.get("price") is not None:
            pick["price"] = row["price"]
        if row.get("price_source"):
            pick["price_source"] = row["price_source"]

    # 추천 종목의 차트·뉴스도 선택된 개수만큼 병렬로 채운다(보통 3개).
    # 발굴(sp500/kospi200) 모드에서는 6개월 일봉이 프리 스캔으로 이미 캐시에 있어 네트워크가 없다.
    _t = time.perf_counter()
    stock_tools.bounded_map(_attach_chart, picks)
    _mark("charts_ms", _t)
    timings["total_ms"] = int((time.perf_counter() - _t0) * 1000)

    # ⚠️ 여기서는 이력을 남기지 않는다(2026-11 변경). 자동 기록은 사용자가 분석만 돌려도
    # 성적표를 오염시켰으므로, 클라이언트가 카드의 "📌 추적 추가"로 고른 픽만
    # `POST /api/performance/track`에서 기록한다. 응답 모양은 그대로다.

    # strategy·mode·universe_scanned·conditions·analysis_source·analysis_cached·timings는 추가 필드다
    # (기존 클라이언트는 무시해도 동작한다).
    # conditions: 사용자 문장에서 읽은 세부 조건 키 (예: ["uptrend", "low_pe"])
    # analysis_source: "gemini"(정상) | "fallback"(모델 호출 실패 → 서버 점수 기반 요약)
    # analysis_cached: true면 10분 안의 같은 요청 → Gemini를 부르지 않고 분석만 재사용(주가는 최신)
    # timings: 단계별 소요 시간(ms) — discovery_ms·rows_ms(지표+실시간 병렬)·realtime_ms·overlay_ms·
    #          sector_ms·news_ms·gemini_ms·charts_ms·total_ms
    logger.info("스크리닝 완료 mode=%s 종목=%d 상위=%d 캐시=%s %s", mode, len(tickers),
                len(picks), analysis_cached, timings)
    return _json_safe({"analysis": data, "all_stocks": rows, "strategy": strategy,
                       "conditions": conditions,
                       "mode": mode, "universe_scanned": universe_scanned,
                       "analysis_source": analysis_source, "analysis_note": analysis_note,
                       "analysis_model": analysis_model, "analysis_cached": analysis_cached,
                       "timings": timings})


@app.get("/api/performance")
def get_performance(limit: int = history_store.DEFAULT_LIMIT):
    """AI 추천 성적표 — 기록된 추천을 현재가와 비교해 적중률·평균 수익률을 돌려준다.

    응답: `{"summary": {...}, "picks": [...], "as_of": ISO, "limit": n, "storage_backend": str}`
    - `storage_backend`: 이력이 실제로 저장된 곳 — "postgres"(DATABASE_URL 설정) / "sqlite"(로컬 폴백)
    - `summary`: total_picks, evaluated_picks, win_count, win_rate_pct, avg_return_pct,
      best_pick, worst_pick (이력이 없으면 개수 0, 비율 null)
    - `picks`: 최신 추천부터 `id`(개별 삭제용 정수 PK)·ticker·entry_price·current_price·
      return_pct·is_win·days_held 등
    현재가는 장중이면 실시간 시세(네이버·Finnhub)를 쓰고, 실패한 종목만 최근 종가(5분 캐시)로
    대신한다 — 각 행의 `price_source`(`naver_realtime`/`finnhub_quote`/`daily_close`)로 확인한다.
    """
    try:
        return history_store.get_performance(limit=limit)
    except Exception as exc:
        logger.warning("성적표 계산 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        return history_store.empty_performance(error="performance_unavailable")


@app.post("/api/performance/track")
def track_performance(req: TrackRequest):
    """종목 하나를 사용자가 직접 추적 목록에 넣는다(추천 카드의 "📌 추적 추가").

    - `entry_price`가 없거나 0 이하면 **실시간 현재가(네이버·Finnhub)를 먼저** 받아 채운다.
      실시간가를 못 받을 때만 최근 일봉 종가로 폴백하고, 둘 다 실패하면 400(잘못된 티커).
    - 요청으로 온 `entry_price`는 그대로 쓴다(실시간 조회를 하지 않는다 — 기존 동작 유지).
    - 같은 날 같은 `(티커, 전략)`을 다시 넣으면 새 행을 만들지 않고 기존 행을 돌려준다.
    - 응답: `{"ok": true, "created": bool, "pick": {id, ticker, ..., criteria}}`
      + `entry_price_source`: 이 요청이 서버에서 진입가를 조회했을 때 그 출처
      (`naver_realtime`/`finnhub_quote`/`daily_close`, 클라이언트가 값을 보냈거나 기존 행을
      돌려준 경우 null) — 기존 클라이언트는 무시해도 된다.
    """
    ticker = (req.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker_required")
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="invalid_ticker")

    entry = req.entry_price
    if entry is not None and (not math.isfinite(entry) or entry <= 0):
        entry = None  # NaN·inf·0·음수는 "값 없음"으로 보고 서버가 채운다
    entry_source = None
    if entry is None:
        # 성적표 현재가와 같은 우선순위: 실시간 시세 → (실패 종목만) 일봉 종가.
        try:
            prices, sources, _ = history_store.resolve_current_prices([ticker])
            price = prices.get(ticker)
            entry_source = sources.get(ticker)
        except Exception as exc:  # 가격 조회가 죽어도 400으로 깔끔하게 끝낸다
            logger.warning("추적 진입가 조회 실패 %s: %s", ticker, type(exc).__name__)
            price = None
            entry_source = None
        if not price or price <= 0:
            raise HTTPException(status_code=400, detail="invalid_ticker")
        entry = float(price)
        logger.info("추적 진입가 %s=%.4f (source=%s)", ticker, entry, entry_source)

    try:
        out = history_store.record_pick(
            ticker=ticker,
            entry_price=entry,
            strategy=req.strategy or "",
            mode=req.mode or "watchlist",
            criteria=req.criteria or "",
            quant_score=req.quant_score,
        )
    except Exception as exc:
        logger.warning("추적 저장 실패 %s: %s: %s", ticker, type(exc).__name__, str(exc)[:200])
        raise HTTPException(status_code=503, detail="storage_unavailable")

    if not out.get("pick"):
        # 저장은 됐는데 행을 못 읽은 경우(경합 등) — 클라이언트가 id를 못 받으므로 재시도를 유도한다.
        raise HTTPException(status_code=503, detail="track_unavailable")
    # 진입가 출처는 "이 요청이 실제로 저장한 값"에만 붙인다(중복이면 기존 행의 진입가가 남는다).
    return {"ok": True, "created": out["created"], "pick": out["pick"],
            "backend": out.get("backend"),
            "entry_price_source": entry_source if out["created"] else None}


@app.delete("/api/performance/{pick_id}")
def delete_performance_pick(pick_id: int):
    """성적표 행 **하나만** 지운다. 없으면 404.

    응답: `{"ok": true, "deleted_id": pick_id}`
    """
    try:
        deleted = history_store.delete_pick(pick_id)
    except Exception as exc:
        logger.warning("추적 삭제 실패 id=%s: %s: %s", pick_id, type(exc).__name__, str(exc)[:200])
        raise HTTPException(status_code=503, detail="storage_unavailable")
    if not deleted:
        raise HTTPException(status_code=404, detail="pick_not_found")
    return {"ok": True, "deleted_id": pick_id}


@app.get("/api/stock/{ticker}")
def get_stock_detail(ticker: str):
    sym = ticker.strip().upper()
    tk = yf.Ticker(sym)
    hist = stock_tools.get_history(sym, "3mo")
    close = hist["Close"] if hist is not None and not hist.empty else pd.Series(dtype=float)

    levels = {}
    if not close.empty:
        levels = {
            "current": round(float(close.iloc[-1]), 2),
            "resistance_60d": round(float(hist["High"].max()), 2),
            "support_60d": round(float(hist["Low"].min()), 2),
            "ma20": round(float(close.tail(20).mean()), 2),
        }

    quarters = []
    try:
        q = tk.quarterly_income_stmt
        if q is not None and not q.empty:
            for col in list(q.columns)[:4]:
                rev = q.loc["Total Revenue", col] if "Total Revenue" in q.index else None
                op = q.loc["Operating Income", col] if "Operating Income" in q.index else None
                net = q.loc["Net Income", col] if "Net Income" in q.index else None
                quarters.append({
                    "quarter": col.strftime("%Y-%m"),
                    "revenue_m": None if pd.isna(rev) else round(float(rev) / 1e6, 1),
                    "operating_m": None if pd.isna(op) else round(float(op) / 1e6, 1),
                    "net_m": None if pd.isna(net) else round(float(net) / 1e6, 1),
                })
    except Exception:
        quarters = []

    currency = naver_finance.currency_for(sym)
    # 장중이면 상세 화면의 현재가도 실시간 시세로 맞춘다(실패하면 일봉 종가 유지).
    price_source = "daily_close"
    try:
        quote = realtime_quotes.get_prices([sym]).get(sym) or {}
        live = quote.get("price")
        if live is not None and float(live) > 0 and levels:
            levels["current"] = round(float(live), 2)
            price_source = realtime_quotes.source_of(quote)
    except Exception as exc:  # 상세 화면이 실시간 시세 때문에 500이 되지 않게
        logger.warning("%s: 상세 실시간 시세 실패 → %s: %s", sym, type(exc).__name__, str(exc)[:120])
    name = None
    if currency == "KRW":
        try:
            name = naver_finance.display_name(sym)  # 내장 목록 우선(없으면 네이버 1회 조회)
        except Exception:
            name = None

    return _json_safe({"ticker": sym, "name": name, "currency": currency,
                       "levels": levels, "quarters": quarters, "price_source": price_source})


# --- AI 모의투자(페이퍼 트레이딩) ------------------------------------------------
# 계좌·포지션·체결·수익률 스냅샷은 paper_store(성적표와 같은 DB, 별도 테이블)에,
# 매매 로직(환율·후보·매도/매수 판단·현금 한도)은 paper_trading에 있다. 여기서는
# Gemini 호출을 paper_trading에 넘겨 주고(ai_call), HTTP 표면만 만든다.


def _gemini_text(prompt: str) -> str | None:
    """모의투자 판단용 Gemini 호출 — 모델·타임아웃·예산 규칙은 스크리닝 분석과 같은 것을 쓴다.

    실패하면 None을 돌려주고, 호출부(paper_trading)는 정량 점수 폴백으로 내려간다.
    """
    queue = list(_gemini_model_plan())
    deadline = time.time() + GEMINI_TOTAL_BUDGET_MS / 1000.0
    thinking_budget = GEMINI_THINKING_BUDGET
    while queue:
        model, budget_ms = queue.pop(0)
        remaining_ms = int((deadline - time.time()) * 1000)
        if remaining_ms < 3000:
            break
        try:
            client = _gemini_client(min(budget_ms, remaining_ms))
            config = types.GenerateContentConfig(temperature=0)
            if thinking_budget is not None:
                try:
                    config.thinking_config = types.ThinkingConfig(thinking_budget=thinking_budget)
                except Exception:  # 사고 설정을 모르는 SDK — 기본값으로 둔다
                    pass
            res = client.models.generate_content(model=model, contents=prompt, config=config)
            text = (getattr(res, "text", "") or "").strip()
            if text:
                return text
        except Exception as exc:
            logger.warning("모의투자 Gemini 실패(%s) → %s: %s", model, type(exc).__name__, str(exc)[:200])
            if _thinking_rejected(exc) and thinking_budget is not None:
                thinking_budget = None
                queue.insert(0, (model, budget_ms))
                continue
            if not _retryable_gemini_error(exc):
                break
            time.sleep(0.3)
    return None


def _paper_ai_call(prompt: str) -> str | None:
    """paper_trading에 넘기는 AI 판단 함수(자동 루프에서도 같은 함수를 쓴다)."""
    return _gemini_text(prompt)


class PaperConfigureRequest(BaseModel):
    """`POST /api/paper/configure` — 시드머니·시장·전략·자동 실행 설정/리셋.

    - `initial_cash_krw`: 원화 시드머니(10만~10억). `reset` 없이 바꾸면 현금에서 차감/추가된다.
    - `markets`: "ALL"(한국+미국) | "US" | "KR".
    - `strategy`: momentum | value | oversold | balanced.
    - `auto_enabled`: 매일 1회 자동 매매(한국 15:40 · 미국 06:20 KST).
    - `reset`: true면 보유·체결·스냅샷을 지우고 새 시드머니로 다시 시작.
    """

    initial_cash_krw: float | None = None
    markets: str | None = None
    strategy: str | None = None
    auto_enabled: bool | None = None
    reset: bool = False


class PaperRunRequest(BaseModel):
    """`POST /api/paper/run` — AI 매매 1회 실행.

    `force=false`(기본)면 오늘 이미 돈 시장은 건너뛴다. 사용자가 버튼으로 직접 돌릴 때는
    클라이언트가 `force=true`를 보낸다. `markets`를 주면 그 시장만 판단한다.
    """

    force: bool = False
    markets: str | None = None


@app.get("/api/paper/status")
def paper_status(trades: int = 40, history: int = 180):
    """AI 모의투자 계좌 요약·보유 포지션(평가손익)·체결 내역·수익률 추이."""
    try:
        return paper_trading.status(limit_trades=trades, limit_equity=history)
    except Exception as exc:
        logger.exception("모의투자 상태 조회 실패: %s", exc)
        raise HTTPException(status_code=503, detail="paper_storage_unavailable")


@app.post("/api/paper/configure")
def paper_configure(req: PaperConfigureRequest):
    """시드머니 설정·계좌 리셋·시장/전략/자동 실행 설정."""
    try:
        result = paper_trading.configure(
            initial_cash_krw=req.initial_cash_krw,
            markets=req.markets,
            strategy=req.strategy,
            auto_enabled=req.auto_enabled,
            reset=req.reset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("모의투자 설정 실패: %s", exc)
        raise HTTPException(status_code=503, detail="paper_storage_unavailable")
    return {"ok": True, **result, "status": paper_trading.status()}


@app.post("/api/paper/run")
def paper_run(req: PaperRunRequest | None = None):
    """AI 일일 매매 1회 실행 — 매도(손절·익절·AI) → 매수(현금 한도) → 스냅샷.

    응답에는 실행 결과(`result`: 매수/매도 체결·건너뛴 이유·요약)와 갱신된 `status`가 함께 온다.
    """
    payload = req or PaperRunRequest()
    try:
        result = paper_trading.run(markets=payload.markets, force=payload.force,
                                   trigger="manual", ai_call=_paper_ai_call)
    except Exception as exc:
        logger.exception("모의투자 실행 실패: %s", exc)
        raise HTTPException(status_code=503, detail="paper_run_failed")
    return {"ok": True, "result": result, "status": paper_trading.status()}
