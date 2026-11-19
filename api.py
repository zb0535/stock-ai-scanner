import json
import logging
import os
import re
import threading
import time
import pandas as pd
import yfinance as yf
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
import stock_tools
import scoring
import sp500_scanner
from finnhub_client import status as finnhub_status

logger = logging.getLogger("api")

MODEL_ID = "gemini-3.5-flash-lite"

# S&P 500 자동 발굴에서 Gemini로 넘길 후보 수.
DISCOVERY_TOP_N = sp500_scanner.TOP_N

SYSTEM_PROMPT = """너는 미국 주식 차트·재무제표 스크리닝 보조 봇이다.
제공된 데이터에 없는 가격·지표·뉴스를 절대 지어내지 마라.
종목 데이터는 서버가 계산한 정량 점수 `quant_score`(0~100, 높을수록 조건 적합) 내림차순으로 정렬돼 있고, 각 종목에는 점수 근거인 `signals` 목록이 붙어 있다. `strategy`는 사용자 조건에서 읽은 전략 의도(value=가치, oversold=과매도 반등, momentum=모멘텀, balanced=균형)다.
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

app = FastAPI(title="미국 주식 AI 스크리너 API")

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
    """

    criteria: str
    tickers: list[str] | None = None
    mode: str = "watchlist"


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
    """
    return {"status": "ok", "build": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or "local",
            "finnhub": finnhub_status()}


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

@app.get("/api/search")
def search_stocks(q: str = ""):
    if not q.strip():
        return {"results": []}
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
        return {"results": results}
    except Exception:
        return {"results": []}


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
    if mode not in ("watchlist", "sp500"):
        mode = "watchlist"  # 모르는 값은 기존 동작으로 되돌린다(클라이언트 호환)

    universe_scanned = None
    if mode == "sp500":
        started = time.time()
        discovery = sp500_scanner.select_candidates(req.criteria, top_n=DISCOVERY_TOP_N)
        tickers = discovery["tickers"]
        universe_scanned = discovery["universe_scanned"]
        logger.info(
            "S&P 500 발굴: 전략=%s 유니버스=%d 산출=%d 상위=%d 캐시=%s %.1fs %s",
            discovery["strategy"], discovery["universe_scanned"], discovery["scanned"],
            len(tickers), discovery["cached"], time.time() - started, ",".join(tickers),
        )
        if not tickers:
            # 프리 스캔이 완전히 비었을 때(모든 다운로드 실패)는 관심 종목으로 안전하게 되돌린다.
            logger.warning("S&P 500 발굴 결과 없음 → 관심 종목으로 대체")
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
    res = client.models.generate_content(
        model=MODEL_ID,
        contents=(
            (f"발굴 유니버스: S&P 500 {universe_scanned}종목 중 기술적 사전 스캔 상위 "
             f"{len(tickers)}종목(관심 종목 목록이 아니다)\n" if mode == "sp500" else "")
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

    # 추천 종목의 차트·뉴스도 선택된 개수만큼 병렬로 채운다(보통 3개).
    stock_tools.bounded_map(_attach_chart, picks)

    # strategy·mode·universe_scanned는 추가 필드다(기존 클라이언트는 무시해도 동작한다).
    return {"analysis": data, "all_stocks": rows, "strategy": strategy,
            "mode": mode, "universe_scanned": universe_scanned}


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

    return {"ticker": sym, "levels": levels, "quarters": quarters}
