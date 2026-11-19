import json
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

MODEL_ID = "gemini-3.5-flash-lite"

SYSTEM_PROMPT = """너는 미국 주식 차트·재무제표 스크리닝 보조 봇이다.
제공된 데이터에 없는 가격·지표·뉴스를 절대 지어내지 마라.
선정한 종목마다 차트 추세(이평선·RSI), 재무 지표(PER·매출성장률·영업이익률·부채비율), 그리고 제공된 최근 뉴스 맥락이 있다면 함께 인용하고 리스크도 짚어라.
만약 일부 종목의 재무 지표가 일시적 API 제한으로 비어(`null`) 있더라도, 주가·이동평균·RSI 등 제공된 수치만으로 분석하거나 값이 있는 다른 종목을 우선 추천하고, '전체 정보가 없다'고 뭉뚱그리지 마라. `financials_note`가 붙은 종목은 재무 지표만 일시 미수신일 뿐 차트 수치는 유효하다.
반드시 아래 JSON 형식으로만 출력하라:
{
  "market_note": "후보군 전반 요약 1~2문장",
  "top_picks": [
    {
      "ticker": "티커",
      "price": 0.0,
      "reason": "차트+재무+뉴스 기준 추천 근거 (실제 수치 포함)",
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
    criteria: str
    tickers: list[str] | None = None


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
    """헬스 체크. UptimeRobot 무료 모니터는 HEAD로 찔러오므로 HEAD에도 200을 준다."""
    return {"status": "ok"}


@app.api_route("/api/watchlist", methods=["GET", "HEAD"])
def get_watchlist():
    return {"tickers": stock_tools.get_watchlist()}

_SP500_CACHE = None


@app.get("/api/sp500")
def get_sp500():
    global _SP500_CACHE
    if _SP500_CACHE is None:
        try:
            df = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
            _SP500_CACHE = [
                {
                    "symbol": str(r["Symbol"]).replace(".", "-"),
                    "name": str(r["Security"]),
                    "exchange": str(r["GICS Sector"]),
                }
                for _, r in df.iterrows()
            ]
        except Exception:
            _SP500_CACHE = []
    return {"results": _SP500_CACHE}

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
    tickers = [t.strip().upper() for t in (req.tickers or stock_tools.get_watchlist()) if t.strip()]
    # 15개 종목을 순차 조회하면 13초 넘게 걸리므로, 최대 6개씩 병렬로 당긴다.
    # bounded_map이 입력 순서를 유지해 주므로 응답의 종목 순서는 그대로다.
    rows = stock_tools.bounded_map(_screen_row, tickers)

    client = genai.Client()
    res = client.models.generate_content(
        model=MODEL_ID,
        contents=f"조건: {req.criteria}\n\n종목 데이터:\n{json.dumps(rows, ensure_ascii=False)}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
        ),
    )

    raw = re.sub(r"^```json\s*|\s*```$", "", (res.text or "").strip())
    data = json.loads(raw)

    # 추천 종목의 차트·뉴스도 선택된 개수만큼 병렬로 채운다(보통 3개).
    stock_tools.bounded_map(_attach_chart, data.get("top_picks", []))

    return {"analysis": data, "all_stocks": rows}


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
