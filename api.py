import json
import logging
import math
import os
import re
import threading
import time
from contextlib import asynccontextmanager

import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException
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
from finnhub_client import status as finnhub_status

logger = logging.getLogger("api")

MODEL_ID = "gemini-3.5-flash-lite"

# S&P 500 자동 발굴에서 Gemini로 넘길 후보 수.
DISCOVERY_TOP_N = sp500_scanner.TOP_N

SYSTEM_PROMPT = """너는 한국·미국 주식 차트·재무제표 스크리닝 보조 봇이다.
제공된 데이터에 없는 가격·지표·뉴스를 절대 지어내지 마라.
종목 데이터는 서버가 계산한 정량 점수 `quant_score`(0~100, 높을수록 조건 적합) 내림차순으로 정렬돼 있고, 각 종목에는 점수 근거인 `signals` 목록이 붙어 있다. `strategy`는 사용자 조건에서 읽은 전략 의도(value=가치, oversold=과매도 반등, momentum=모멘텀, balanced=균형)다.
통화: 각 종목의 `currency`를 따른다. `KRW`면 가격을 `₩65,400`처럼 원 단위로, `USD`면 `$352.45`처럼 달러로 적어라. 통화를 섞거나 환율로 환산하지 마라. `name`에 한국어 종목명이 있으면 `삼성전자(005930.KS)`처럼 티커와 함께 써라.
선정 근거에는 `quant_score`와 그 근거(`signals`)를 반드시 수치로 인용하고, 함께 제공된 차트 지표(이평선·RSI·MACD·볼린저밴드·거래량 배수·고점 대비 낙폭)와 재무 지표(PER·매출성장률·영업이익률·부채비율), 최근 뉴스 맥락이 있다면 그것도 엮어라. 점수가 높아도 약한 신호(`signals`의 부정 항목, 예: 고PER·과열 RSI·역배열)가 있으면 리스크로 짚어라.
점수는 참고값이다. 점수가 비슷한 종목은 재무·뉴스로 우열을 가려도 되지만, 점수가 높은 종목을 이유 없이 빼지 마라.
만약 일부 종목의 재무 지표가 일시적 API 제한으로 비어(`null`) 있더라도, 주가·이동평균·RSI 등 제공된 수치만으로 분석하거나 값이 있는 다른 종목을 우선 추천하고, '전체 정보가 없다'고 뭉뚱그리지 마라. `financials_note`가 붙은 종목은 재무 지표만 일시 미수신일 뿐 차트 수치는 유효하다.
반드시 아래 JSON 형식으로만 출력하라(다른 키를 추가하지 마라):
{
  "market_note": "후보군 전반 요약 1~2문장",
  "top_picks": [
    {
      "ticker": "티커",
      "price": 0.0,
      "reason": "quant_score·signals·차트+재무+뉴스 기준 추천 근거 (실제 수치 포함)",
      "risk": "주의할 점 또는 단점"
    }
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
    try:
        yield
    finally:
        sp500_scanner.stop_prewarm()
        kospi200_scanner.stop_prewarm()


app = FastAPI(title="한국·미국 주식 AI 스크리너 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    `entry_price`는 선택이다. 비었거나 0 이하면 서버가 최신 종가를 받아 채우고,
    그마저 실패하면 400(잘못된 티커)이다.
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
        try:
            items = yf.Ticker(sym).news or []
            for item in items[:_NEWS_FETCH_LIMIT]:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or (item.get("content") or {}).get("title")
                if title:
                    titles.append(title)
        except Exception:
            titles = []

    with _NEWS_LOCK:
        _NEWS_CACHE[sym] = (now, _NEWS_TTL if titles else _NEWS_TTL_EMPTY, titles)
    return list(titles[:limit])


@app.api_route("/", methods=["GET", "HEAD"])
def health():
    """헬스 체크. UptimeRobot 무료 모니터는 HEAD로 찔러오므로 HEAD에도 200을 준다.

    `build`는 Render가 주는 배포 커밋(RENDER_GIT_COMMIT) 앞 7자다. 어느 커밋이
    실제로 떠 있는지 바로 확인할 수 있어 배포 검증에 쓴다(로컬 실행 시 "local").
    `finnhub`은 키 설정 여부·마지막 실패 사유만 노출한다(키 본문은 응답에 없다).
    `sp500_cache`는 S&P 500 프리 스캔 스냅샷 상태(예열 여부·종목 수·나이)이고,
    `kospi200_cache`는 코스피 200 스냅샷 상태다. 이 응답은 스캔 중에도 즉시 돌아온다 —
    락은 dict 복사 순간만 잡는다.
    """
    return {"status": "ok", "build": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or "local",
            "finnhub": finnhub_status(),
            "storage_backend": history_store.storage_backend(),
            "sp500_cache": sp500_scanner.cache_status(),
            "kospi200_cache": kospi200_scanner.cache_status()}


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
    """한 종목의 차트·재무 지표와 최근 뉴스를 묶어 한 행으로 만듭니다."""
    metrics = stock_tools.get_stock_metrics(ticker)
    metrics["news"] = _get_recent_news(ticker, limit=2)
    return metrics


def _attach_chart(pick: dict) -> dict:
    """추천 종목 하나에 6개월 차트(가격·20일·60일선)와 뉴스를 붙입니다."""
    hist = stock_tools.get_history(pick["ticker"], "6mo")
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
    pick["news"] = _get_recent_news(pick["ticker"], limit=3)
    return pick


@app.post("/api/screen")
def screen_stocks(req: ScreenRequest):
    mode = (req.mode or "watchlist").strip().lower()
    if mode not in ("watchlist", "sp500", "kospi200"):
        mode = "watchlist"  # 모르는 값은 기존 동작으로 되돌린다(클라이언트 호환)

    universe_scanned = None
    if mode in ("sp500", "kospi200"):
        started = time.time()
        scanner = sp500_scanner if mode == "sp500" else kospi200_scanner
        universe_label = "S&P 500" if mode == "sp500" else "코스피 200"
        discovery = scanner.select_candidates(req.criteria, top_n=DISCOVERY_TOP_N)
        tickers = discovery["tickers"]
        universe_scanned = discovery["universe_scanned"]
        logger.info(
            "%s 발굴: 전략=%s 유니버스=%d 산출=%d 상위=%d 캐시=%s %.1fs %s",
            universe_label, discovery["strategy"], discovery["universe_scanned"],
            discovery["scanned"], len(tickers), discovery["cached"],
            time.time() - started, ",".join(tickers),
        )
        if not tickers:
            # 프리 스캔이 완전히 비었을 때(모든 다운로드 실패)는 관심 종목으로 안전하게 되돌린다.
            logger.warning("%s 발굴 결과 없음 → 관심 종목으로 대체", universe_label)
            tickers = [t.strip().upper() for t in (req.tickers or stock_tools.get_watchlist())
                       if t.strip()]
            universe_scanned = 0
    else:
        tickers = [t.strip().upper() for t in (req.tickers or stock_tools.get_watchlist()) if t.strip()]
    # 15개 종목을 순차 조회하면 13초 넘게 걸리므로, 최대 6개씩 병렬로 당긴다.
    # bounded_map이 입력 순서를 유지해 주므로 점수가 같은 종목의 정렬도 흔들리지 않는다.
    rows = stock_tools.bounded_map(_screen_row, tickers)

    # 정량 스코어링: 조건에서 전략을 읽고 quant_score·signals를 붙여 내림차순 정렬한다.
    # 네트워크 호출은 없다(이미 받은 지표만 사용). 동점은 입력 순서를 유지한다.
    rows, strategy = scoring.rank_stocks(rows, criteria=req.criteria)

    client = genai.Client()
    universe_note = ""
    if mode == "sp500":
        universe_note = (f"발굴 유니버스: S&P 500 {universe_scanned}종목 중 기술적 사전 스캔 상위 "
                         f"{len(tickers)}종목(관심 종목 목록이 아니다)\n")
    elif mode == "kospi200":
        universe_note = (f"발굴 유니버스: 코스피 200 {universe_scanned}종목 중 기술적 사전 스캔 상위 "
                         f"{len(tickers)}종목(관심 종목 목록이 아니다). 가격은 원화(₩)다.\n")
    res = client.models.generate_content(
        model=MODEL_ID,
        contents=(
            universe_note
            + f"조건: {req.criteria}\n"
            f"strategy: {strategy} ({scoring.STRATEGY_LABELS.get(strategy, strategy)})\n\n"
            f"종목 데이터(quant_score 내림차순):\n{json.dumps(rows, ensure_ascii=False)}"
        ),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
        ),
    )

    raw = re.sub(r"^```json\s*|\s*```$", "", (res.text or "").strip())
    data = json.loads(raw)

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

    # 추천 종목의 차트·뉴스도 선택된 개수만큼 병렬로 채운다(보통 3개).
    stock_tools.bounded_map(_attach_chart, picks)

    # ⚠️ 여기서는 이력을 남기지 않는다(2026-11 변경). 자동 기록은 사용자가 분석만 돌려도
    # 성적표를 오염시켰으므로, 클라이언트가 카드의 "📌 추적 추가"로 고른 픽만
    # `POST /api/performance/track`에서 기록한다. 응답 모양은 그대로다.

    # strategy·mode·universe_scanned는 추가 필드다(기존 클라이언트는 무시해도 동작한다).
    return {"analysis": data, "all_stocks": rows, "strategy": strategy,
            "mode": mode, "universe_scanned": universe_scanned}


@app.get("/api/performance")
def get_performance(limit: int = history_store.DEFAULT_LIMIT):
    """AI 추천 성적표 — 기록된 추천을 현재가와 비교해 적중률·평균 수익률을 돌려준다.

    응답: `{"summary": {...}, "picks": [...], "as_of": ISO, "limit": n, "storage_backend": str}`
    - `storage_backend`: 이력이 실제로 저장된 곳 — "postgres"(DATABASE_URL 설정) / "sqlite"(로컬 폴백)
    - `summary`: total_picks, evaluated_picks, win_count, win_rate_pct, avg_return_pct,
      best_pick, worst_pick (이력이 없으면 개수 0, 비율 null)
    - `picks`: 최신 추천부터 `id`(개별 삭제용 정수 PK)·ticker·entry_price·current_price·
      return_pct·is_win·days_held 등
    현재가는 성적표 조회 시점 기준 최근 종가(5분 캐시)를 쓴다.
    """
    try:
        return history_store.get_performance(limit=limit)
    except Exception as exc:
        logger.warning("성적표 계산 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        return history_store.empty_performance(error="performance_unavailable")


@app.post("/api/performance/track")
def track_performance(req: TrackRequest):
    """종목 하나를 사용자가 직접 추적 목록에 넣는다(추천 카드의 "📌 추적 추가").

    - `entry_price`가 없거나 0 이하면 최근 종가를 채운다. 가격을 못 받으면 400(잘못된 티커).
    - 같은 날 같은 `(티커, 전략)`을 다시 넣으면 새 행을 만들지 않고 기존 행을 돌려준다.
    - 응답: `{"ok": true, "created": bool, "pick": {id, ticker, ..., criteria}}`
    """
    ticker = (req.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker_required")
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="invalid_ticker")

    entry = req.entry_price
    if entry is not None and (not math.isfinite(entry) or entry <= 0):
        entry = None  # NaN·inf·0·음수는 "값 없음"으로 보고 서버가 채운다
    if entry is None:
        try:
            price = history_store.latest_prices([ticker]).get(ticker)
        except Exception as exc:  # 가격 조회가 죽어도 400으로 깔끔하게 끝낸다
            logger.warning("추적 진입가 조회 실패 %s: %s", ticker, type(exc).__name__)
            price = None
        if not price or price <= 0:
            raise HTTPException(status_code=400, detail="invalid_ticker")
        entry = float(price)

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
    return {"ok": True, "created": out["created"], "pick": out["pick"],
            "backend": out.get("backend")}


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
    name = None
    if currency == "KRW":
        try:
            name = naver_finance.display_name(sym)  # 내장 목록 우선(없으면 네이버 1회 조회)
        except Exception:
            name = None

    return {"ticker": sym, "name": name, "currency": currency,
            "levels": levels, "quarters": quarters}
