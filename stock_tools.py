import sys
import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "AVGO", "AMD", "TSM",
    "NFLX", "JPM", "LLY", "COST", "PLTR",
]


def get_watchlist() -> list[str]:
    """스크리닝 대상 미국 주식 티커 목록을 반환합니다."""
    return DEFAULT_WATCHLIST


def _calc_rsi(close_series, period: int = 14) -> float | None:
    if len(close_series) < period + 1:
        return None
    delta = close_series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def get_stock_metrics(ticker: str) -> dict:
    """특정 종목의 최근 차트 지표(이동평균·RSI)와 핵심 재무제표 지표를 반환합니다."""
    t = yf.Ticker(ticker.upper())
    hist = t.history(period="6mo")
    if hist.empty:
        return {"ticker": ticker.upper(), "error": "주가 데이터를 찾을 수 없습니다."}

    close = hist["Close"]
    price = round(float(close.iloc[-1]), 2)
    ma20 = round(float(close.rolling(20).mean().iloc[-1]), 2) if len(close) >= 20 else None
    ma60 = round(float(close.rolling(60).mean().iloc[-1]), 2) if len(close) >= 60 else None
    rsi14 = _calc_rsi(close, 14)

    trend = "판단불가"
    if ma20 and ma60:
        if price > ma20 > ma60:
            trend = "상승 추세(정배열: 현재가>20일선>60일선)"
        elif price < ma20 < ma60:
            trend = "하락 추세(역배열: 현재가<20일선<60일선)"
        else:
            trend = "횡보/혼조"

    info = t.info or {}
    return {
        "ticker": ticker.upper(),
        "name": info.get("shortName", ticker.upper()),
        "price": price,
        "ma20": ma20,
        "ma60": ma60,
        "rsi14": rsi14,
        "trend": trend,
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "revenue_growth": info.get("revenueGrowth"),
        "operating_margins": info.get("operatingMargins"),
        "debt_to_equity": info.get("debtToEquity"),
    }


if __name__ == "__main__":
    for sym in ["NVDA", "AAPL"]:
        print(get_stock_metrics(sym))