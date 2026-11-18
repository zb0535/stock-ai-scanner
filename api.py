import json
import re
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


def _get_recent_news(ticker: str, limit: int = 3) -> list[str]:
    try:
        items = yf.Ticker(ticker).news or []
        titles = []
        for item in items[:limit]:
            title = item.get("title") or item.get("content", {}).get("title")
            if title:
                titles.append(title)
        return titles
    except Exception:
        return []


@app.get("/api/watchlist")
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

@app.post("/api/screen")
def screen_stocks(req: ScreenRequest):
    tickers = [t.strip().upper() for t in (req.tickers or stock_tools.get_watchlist()) if t.strip()]
    rows = []
    for t in tickers:
        m = stock_tools.get_stock_metrics(t)
        m["news"] = _get_recent_news(t, limit=2)
        rows.append(m)

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

    for pick in data.get("top_picks", []):
        hist = yf.Ticker(pick["ticker"]).history(period="6mo")["Close"]
        ma20 = hist.rolling(20).mean()
        ma60 = hist.rolling(60).mean()
        recent = pd.DataFrame({"price": hist, "ma20": ma20, "ma60": ma60}).tail(60)
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

    return {"analysis": data, "all_stocks": rows}

@app.get("/api/stock/{ticker}")
def get_stock_detail(ticker: str):
    sym = ticker.strip().upper()
    tk = yf.Ticker(sym)
    hist = tk.history(period="3mo")
    close = hist["Close"] if not hist.empty else pd.Series(dtype=float)

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