"""AI 모의투자(페이퍼 트레이딩) 실행 엔진 — 미국(US)·한국(KR) 두 계좌.

계좌
----
- **시장별로 완전히 분리된 두 계좌**: 미국 계좌(id=1)와 한국 계좌(id=2)가 각각 시드머니·
  현금·보유 종목·체결·일별 수익률을 따로 가진다. 한 번의 실행(`run(market="US"|"KR")`)은
  **그 시장 계좌에만** 매매하고 기록하므로 두 계좌의 수익률을 공정하게 비교할 수 있다.

무엇을 하나
-----------
1. **매도 판단**: 그 계좌의 보유 포지션 현재가(미국은 시간외 포함 실시간, 한국은 네이버)를 받아
   손절(-8%)·익절(+25%) 규칙과 AI 판단으로 전량/부분 매도해 현금을 만든다.
2. **매수 판단**: 그 시장의 스크리너 상위 후보(S&P 500 · 코스피 200 + 정량 점수 + Gemini)를
   보고 **남은 현금 한도 안에서만** 종목·금액을 정해 현재가로 체결한다. Gemini가 죽거나
   429면 정량 점수 상위 종목을 균등 배분하는 폴백으로 내려간다(AI 없이도 매매는 돈다).
3. **기록**: 체결·포지션·일별 총자산 스냅샷을 `paper_store`에 남긴다(계좌별).

통화·수량 규칙
--------------
- 계좌의 모든 금액(시드머니·현금·총자산·손익)은 **원화(₩)**다. 미국 종목은 달러 가격으로
  체결하고, **체결 시점의 USD/KRW 환율**을 포지션에 저장해 원화 원가를 고정한다.
  평가할 때는 현재 환율을 써서 환율 변동도 수익률에 들어간다(환차손익 포함).
- 미국 주식 수량은 **소수점 4자리**(토스증권 소수점 거래 방식), 한국 주식은 **1주 정수**.
- 현금은 절대 음수가 되지 않는다: 매수 금액을 잔여 현금으로 자르고, 체결 후 잔고를 다시 확인한다.

거래 비용
---------
- 실제 한국 개인투자자 기준이다(`FEE_POLICY` 상수).
  - 한국: 매수 0.015% / 매도 0.015% + 증권거래세 0.18% = **0.195%**
  - 미국: 수수료 0.1% + 환전 스프레드 0.05% = **0.15%** (매수·매도 동일)
- 매수는 `체결원금 + 매수수수료 <= 현금`이 되도록 수량을 정하고, 포지션 원가(`cost_krw`)에
  매수수수료를 넣는다. 매도는 `총매도대금 - (수수료+세금)`만 현금으로 들어오고,
  `순수취액 - 원가`가 실현손익이다. 즉 손익은 언제나 **수수료·세금·환차를 뺀 순액**이다.
- 매도 판단(손절·익절·AI)은 주가 등락률이 아니라 **지금 팔 때의 순수익률**(`net_pnl_pct`)을 기준으로 한다.

자동 실행
--------
`auto_due_markets()`가 KST 시각과 계좌별 `last_run_*_date`를 보고 지금 돌릴 시장을 알려준다.
- 한국 계좌: 15:40 이후(한국 정규장 마감 15:30 + 여유) — 한국 종목만
- 미국 계좌: 06:20 이후(미국 정규장 마감 16:00 ET = KST 05:00~06:00 + 여유) — 미국 종목만
`start_auto()`가 5분 간격으로 이 함수를 확인해 시장별로 하루 한 번씩 실행한다(`PAPER_AUTO=0`으로 끔).
수동 `POST /api/paper/run`은 `force`를 주면 같은 날에도 다시 돈다.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import broker
import broker_credentials
import extended_quotes
import fcm
import history_store
import kospi200_scanner
import memory_guard
import naver_finance
import paper_auth
import paper_store
import realtime_quotes
import scoring
import sector_pe
import sp500_scanner
import stock_tools

logger = logging.getLogger("paper_trading")

KST = timezone(timedelta(hours=9))

# --- 매매 규칙 ---------------------------------------------------------------
STOP_LOSS_PCT = float(os.environ.get("PAPER_STOP_LOSS_PCT", "-8"))
TAKE_PROFIT_PCT = float(os.environ.get("PAPER_TAKE_PROFIT_PCT", "25"))
MAX_POSITIONS = int(os.environ.get("PAPER_MAX_POSITIONS", "8"))
# 한 종목이 총자산에서 차지할 수 있는 비중 상한(AI가 몰빵하는 것을 막는 안전장치).
MAX_POSITION_WEIGHT = float(os.environ.get("PAPER_MAX_POSITION_WEIGHT", "0.30"))
MIN_ORDER_KRW = float(os.environ.get("PAPER_MIN_ORDER_KRW", "10000"))
CANDIDATES_PER_MARKET = int(os.environ.get("PAPER_CANDIDATES", "10"))

# --- 거래 비용(수수료·증권거래세·환전 스프레드) -------------------------------
# 실제 한국 개인투자자 기준으로 맞춘 값이다. 화면 안내문·프롬프트·회계가 모두 이 표 하나를 쓴다.
# - 한국: 매수 위탁수수료 0.015% / 매도 위탁수수료 0.015% + 증권거래세 0.18% = 0.195%
#   (증권거래세는 매도에만 붙는다. 2026년 코스피 0.18% 기준.)
# - 미국: 위탁수수료 0.1% + 원화↔달러 환전 스프레드 0.05% = 0.15% (매수·매도 동일)
FEE_POLICY = {
    "KR": {
        "label": "한국 주식",
        "currency": "KRW",
        "buy": {"commission_pct": 0.015, "tax_pct": 0.0, "fx_spread_pct": 0.0, "total_pct": 0.015},
        "sell": {"commission_pct": 0.015, "tax_pct": 0.18, "fx_spread_pct": 0.0, "total_pct": 0.195},
    },
    "US": {
        "label": "미국 주식",
        "currency": "USD",
        "buy": {"commission_pct": 0.1, "tax_pct": 0.0, "fx_spread_pct": 0.05, "total_pct": 0.15},
        "sell": {"commission_pct": 0.1, "tax_pct": 0.0, "fx_spread_pct": 0.05, "total_pct": 0.15},
    },
}
# 위 표의 %를 소수 비율로. 계산은 이 두 표만 본다(FEE_POLICY와 어긋나지 않게 함께 고칠 것).
BUY_FEE_RATE = {"KR": 0.000_15, "US": 0.001_5}
SELL_FEE_RATE = {"KR": 0.001_95, "US": 0.001_5}
SELL_TAX_RATE = {"KR": 0.001_8, "US": 0.0}
AI_MAX_BUYS = 3

# 자동 실행 시각(KST, 시:분). 정규장 마감 직후 + 여유.
KR_RUN_AT = (15, 40)
US_RUN_AT = (6, 20)
RUN_AT = {"KR": KR_RUN_AT, "US": US_RUN_AT}
ACCOUNT_NAMES = {"US": "미국", "KR": "한국"}
ACCOUNT_FLAGS = {"US": "🇺🇸", "KR": "🇰🇷"}
ACCOUNT_ORDER = ("KR", "US")   # 같은 시각에 둘 다 밀려 있으면 한국을 먼저 처리한다
AUTO_INTERVAL_SEC = int(os.environ.get("PAPER_AUTO_INTERVAL", "300"))
AUTO_START_GRACE_SEC = 120

# 상태 스냅샷 캐시(탭 전환·새로고침) — 계좌 두 개를 한 번에 평가한 결과를 잠시 재사용한다.
_STATUS_CACHE: dict[str, dict] = {}       # 키 -> {"ts": float, "snapshot": dict}
_RANK_CACHE: dict[str, dict] = {}         # db 키 -> {"ts": float, "data": dict}
STATUS_TTL = float(os.environ.get("PAPER_STATUS_TTL", "25"))
STATUS_STALE_SEC = float(os.environ.get("PAPER_STATUS_STALE", "180"))
RANK_TTL = float(os.environ.get("PAPER_RANK_TTL", "90"))
STATUS_WARM_DELAY = float(os.environ.get("PAPER_STATUS_WARM_DELAY", "20"))
STATUS_WARM_INTERVAL = float(os.environ.get("PAPER_STATUS_WARM_INTERVAL", "60"))
_STATUS_CACHE: dict[str, dict] = {}       # 키 -> {"ts": float, "snapshot": dict}
_STATUS_LOCK = threading.Lock()
_STATUS_BUILD_LOCK = threading.Lock()
_STATUS_REFRESHING: set[str] = set()
_STATUS_GENERATION = 0                    # 매매·설정으로 캐시를 비울 때마다 +1(진행 중 갱신 무효화)

# --- 환율 -------------------------------------------------------------------
DEFAULT_USD_KRW = float(os.environ.get("PAPER_USDKRW", "1350"))
FX_TTL = 600
_FX: dict = {"ts": 0.0, "rate": None, "source": None}
_FX_LOCK = threading.Lock()

NAVER_FX_URL = "https://api.stock.naver.com/marketindex/exchange/FX_USDKRW"
YAHOO_FX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?range=5d&interval=1d"

# 전략 → 스크리너 조건 문장(정량 점수 공식·세부 조건이 이 문장에서 해석된다).
STRATEGY_CRITERIA = {
    "momentum": "상승 추세(정배열)이면서 매출 성장률과 영업이익률이 탄탄한 모멘텀 종목",
    "value": "PER이 상대적으로 낮고 영업이익률과 부채비율이 안정적인 가치주",
    "oversold": "RSI가 낮아 과매도이고 20일선 아래에서 반등 조짐이 있는 종목",
    "balanced": "재무가 안정적이고 과열되지 않은 균형 잡힌 종목",
}

AUTO_STATE: dict = {"started": False, "last_check": None, "last_run": None, "last_error": None}
# 같은 순간에 자동 실행과 수동 실행이 겹치면 두 실행이 같은 현금을 보고 각자 매수해
# 시드머니를 넘겨 살 수 있다(실서버에서 확인). 실행은 프로세스 안에서 하나만 돈다.
_RUN_LOCK = threading.Lock()
_AUTO_STOP = threading.Event()
_AUTO_THREAD: threading.Thread | None = None
_AUTO_FAILURES: dict[tuple[str, str], int] = {}


# --- 작은 유틸 ---------------------------------------------------------------


def market_of(ticker) -> str:
    """티커 → "KR"(`.KS`/`.KQ`) 또는 "US"."""
    return "KR" if naver_finance.is_korean_ticker(ticker) else "US"


def account_scope(value=None) -> str:
    """`"US"`/`"KR"`/`1`/`2` → 계좌의 시장 코드. 빈 값·`"ALL"`은 미국(기본 계좌).

    모르는 값은 `ValueError("market_invalid")` — 오타가 엉뚱한 계좌를 건드리지 않게 한다.
    """
    if value is None:
        return "US"
    if isinstance(value, bool):
        raise ValueError("market_invalid")
    if isinstance(value, int):
        return paper_store.account_market(value)
    token = str(value).strip().upper()
    if not token:
        return "US"
    if token in ("US", "USA", "ALL"):     # ALL은 더 이상 없다(예전 클라이언트 호환 = 미국)
        return "US"
    if token in ("KR", "KOR"):
        return "KR"
    raise ValueError("market_invalid")


def account_id_of(value=None) -> int:
    """`"US"`/`"KR"`/`1`/`2` → 계좌 id(1=미국, 2=한국)."""
    return paper_store.MARKET_ACCOUNTS[account_scope(value)]


def account_label(value=None) -> str:
    """화면·프롬프트·푸시용 이름 — `"🇺🇸 미국"` / `"🇰🇷 한국"`."""
    scope = account_scope(value)
    return f"{ACCOUNT_FLAGS[scope]} {ACCOUNT_NAMES[scope]}"


def normalize_markets(value) -> list[str]:
    """`"ALL"`/`"US"`/`"KR"`/리스트 → 실제 실행 대상 목록(중복 제거, 순서 유지)."""
    if isinstance(value, (list, tuple, set)):
        raw = [str(item) for item in value]
    else:
        raw = [str(value or "")]
    out: list[str] = []
    for item in raw:
        token = item.strip().upper()
        if token == "ALL":
            return ["KR", "US"]
        if token in ("KR", "US") and token not in out:
            out.append(token)
    return out or ["KR", "US"]


def criteria_for(strategy: str | None) -> str:
    return STRATEGY_CRITERIA.get(str(strategy or ""), STRATEGY_CRITERIA["balanced"])


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round_shares(shares: float, currency: str) -> float:
    """미국은 소수점 4자리, 한국은 1주 정수."""
    if currency == "KRW":
        return float(math.floor(shares))
    return math.floor(shares * 10000) / 10000


def _native_amount(shares: float, price: float) -> float:
    return round(float(shares) * float(price), 4)


def fee_rates(market: str | None) -> tuple[float, float]:
    """시장의 (매수 비용률, 매도 비용률). 모르는 시장은 미국 기준으로 본다."""
    code = str(market or "").upper()
    if code not in BUY_FEE_RATE:
        code = "US"
    return BUY_FEE_RATE[code], SELL_FEE_RATE[code]


def fee_policy() -> dict:
    """응답 JSON·화면 안내문에 싣는 수수료·세금 기준(사본)."""
    return json.loads(json.dumps(FEE_POLICY))


# --- 환율 -------------------------------------------------------------------


def _fetch_naver_fx() -> float | None:
    request = urllib.request.Request(NAVER_FX_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://m.stock.naver.com/",
    })
    with urllib.request.urlopen(request, timeout=6) as response:
        payload = json.loads(response.read().decode("utf-8"))
    info = payload.get("exchangeInfo") or {}
    for key in ("closePrice", "calcPrice", "price"):
        rate = _num(str(info.get(key) or "").replace(",", ""))
        if rate and rate > 0:
            return rate
    return None


def _fetch_yahoo_fx() -> float | None:
    request = urllib.request.Request(YAHOO_FX_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    with urllib.request.urlopen(request, timeout=6) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = ((payload.get("chart") or {}).get("result") or [{}])[0]
    meta = result.get("meta") or {}
    rate = _num(meta.get("regularMarketPrice") or meta.get("previousClose"))
    if rate and rate > 0:
        return rate
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for value in reversed(closes):
        rate = _num(value)
        if rate and rate > 0:
            return rate
    return None


def get_usd_krw(force: bool = False) -> dict:
    """USD/KRW 환율 — `{"rate": 1344.5, "source": "naver_fx"}`. 10분 캐시.

    1순위 네이버 금융(하나은행 고시), 2순위 Yahoo `KRW=X`, 마지막은 고정값(1,350원).
    어느 경로든 실패해도 매매가 멈추지 않도록 고정값을 쓰고 `source="fixed_fallback"`로 남긴다.
    """
    now = time.time()
    with _FX_LOCK:
        cached = dict(_FX)
    if not force and cached.get("rate") and now - float(cached.get("ts") or 0) < FX_TTL:
        return cached

    rate, source = None, None
    for name, fetch in (("naver_fx", _fetch_naver_fx), ("yahoo_fx", _fetch_yahoo_fx)):
        try:
            rate = fetch()
        except Exception as exc:
            logger.warning("USD/KRW %s 실패: %s: %s", name, type(exc).__name__, str(exc)[:160])
            rate = None
        if rate:
            source = name
            break
    if not rate:
        rate, source = DEFAULT_USD_KRW, "fixed_fallback"

    fresh = {"ts": now, "rate": round(float(rate), 2), "source": source}
    with _FX_LOCK:
        _FX.update(fresh)
    return fresh


def reset_fx_cache() -> None:
    with _FX_LOCK:
        _FX.update(ts=0.0, rate=None, source=None)


# --- 시세 -------------------------------------------------------------------


def default_prices(tickers) -> tuple[dict, dict]:
    """현재가 — 실시간(네이버·Finnhub·시간외) 우선, 실패분만 일봉 종가. (가격, 출처)."""
    prices, sources, _ = history_store.resolve_current_prices(list(tickers or []))
    return prices, sources


# --- 후보 -------------------------------------------------------------------


def _metrics_row(ticker: str) -> dict:
    """한 종목 지표 — 종목 하나의 실패가 전체를 막지 않게 흡수한다."""
    try:
        row = stock_tools.get_stock_metrics(ticker)
    except Exception as exc:
        logger.warning("%s: 지표 조회 실패 → %s: %s", ticker, type(exc).__name__, str(exc)[:160])
        return {"ticker": ticker, "error": "주가 데이터를 찾을 수 없습니다."}
    if not isinstance(row, dict):
        return {"ticker": ticker, "error": "주가 데이터를 찾을 수 없습니다."}
    return row


def default_candidates(market: str, criteria: str, top_n: int = CANDIDATES_PER_MARKET) -> list[dict]:
    """스크리너 상위 후보(현재가 오버레이 + 정량 점수) — 매수 후보 목록."""
    scanner = sp500_scanner if market == "US" else kospi200_scanner
    discovery = scanner.select_candidates(criteria, top_n=top_n)
    tickers = [str(t).strip().upper() for t in (discovery.get("tickers") or []) if t]
    if not tickers:
        return []
    rows = [_metrics_row(ticker) for ticker in tickers]
    try:
        quotes = realtime_quotes.get_prices(tickers)
    except Exception as exc:  # 시세 계층 장애 → 일봉 종가 유지
        logger.warning("모의투자 시세 오버레이 실패: %s: %s", type(exc).__name__, str(exc)[:160])
        quotes = {}
    rows = stock_tools.apply_realtime_prices(rows, quotes=quotes)
    rows = sector_pe.fill_sector_pe(rows)
    ranked, _ = scoring.rank_stocks(rows, criteria=criteria)
    return [row for row in ranked if isinstance(row, dict) and not row.get("error")
            and _num(row.get("price"))]


def compact_candidate(row: dict) -> dict:
    """프롬프트·응답용 후보 요약(지어낼 수 없는 서버 계산 값만)."""
    keys = ("ticker", "name", "currency", "price", "price_source", "rsi14", "trend",
            "trailing_pe", "forward_pe", "revenue_growth", "operating_margins",
            "debt_to_equity", "sector", "industry_pe", "quant_score", "signals",
            "drawdown_from_high_pct", "volume_ratio")
    return {key: row[key] for key in keys if key in row}


# --- AI ---------------------------------------------------------------------


def build_prompt(account: dict, markets: list[str], cash_krw: float, total_value_krw: float,
                 positions: list[dict], candidates: dict[str, list[dict]], fx: dict) -> str:
    """매매 판단 프롬프트 — 그 계좌에 있는 돈과 그 시장 후보만 주고 JSON 하나만 받는다."""
    scope = account_scope(account.get("market_scope") or (markets[0] if markets else None))
    held = [
        {
            "ticker": item["ticker"], "name": item.get("name"), "market": item.get("market"),
            "shares": item["shares"], "avg_price": item.get("avg_price"),
            "currency": item.get("currency"), "current_price": item.get("current_price"),
            "price_pnl_pct": item.get("pnl_pct"), "cost_krw": item.get("cost_krw"),
            "market_value_krw": item.get("market_value_krw"),
            "buy_fx_rate": item.get("buy_fx_rate"), "current_fx_rate": item.get("fx_rate"),
            "fx_change_pct": item.get("fx_change_pct"),
            "sell_fee_krw": item.get("sell_fee_krw"),
            "net_pnl_krw": item.get("net_pnl_krw"), "net_pnl_pct": item.get("net_pnl_pct"),
            "reason": item.get("reason") or "",
        }
        for item in positions
    ]
    lines = [
        f"너는 {ACCOUNT_FLAGS[scope]} {ACCOUNT_NAMES[scope]} 계좌(원화)만 운용하는 모의투자 AI다.",
        f"이 계좌는 {ACCOUNT_NAMES[scope]} 주식만 매매한다(다른 시장 종목은 사지도 팔지도 마라).",
        "아래 '후보'는 서버가 스크리너(정량 점수 quant_score)로 추린 종목이고, '보유'는 지금 계좌다.",
        "규칙:",
        "- 매수는 **남은 현금 한도 안에서만** 한다. 원화 금액(amount_krw)으로만 말하라.",
        "- amount_krw는 **수수료까지 포함한 그 종목에 쓸 총액**이다(서버가 수수료를 빼고 수량을 정한다).",
        "- 후보 목록에 없는 종목은 절대 사지 마라. 보유 목록에 없는 종목은 팔지 마라.",
        f"- 매수는 최대 {AI_MAX_BUYS}종목, 한 종목은 총자산의 {int(MAX_POSITION_WEIGHT * 100)}% 이하.",
        f"- 종목당 최소 주문 금액은 {int(MIN_ORDER_KRW):,}원이다(그보다 작게 쓰지 마라).",
        "- 매도 비용: 한국은 수수료 0.015% + 증권거래세 0.18% = 0.195%, 미국은 수수료 0.1% + "
        "환전 스프레드 0.05% = 0.15%가 매도대금에서 빠진다.",
        "- 보유의 net_pnl_krw/net_pnl_pct는 **지금 전량 매도할 때의 순실현손익·순수익률**이다 "
        "(매수 수수료가 든 원가, 매도 수수료·세금, 매수 환율 대비 현재 환율의 환차까지 모두 반영).",
        "- price_pnl_pct는 주가 등락만 본 수익률이다. 매도 판단은 수수료·세금·환차를 반영한 "
        "net_pnl_pct를 기준으로 하라. 미국 종목은 fx_change_pct(환율 변동)도 함께 보라.",
        "- 수익률이 나쁘거나 근거가 약해지면 보유 종목을 팔아 현금을 만들어도 된다.",
        "- 제공된 수치에 없는 가격·지표를 지어내지 마라.",
        "",
        f"계좌: {ACCOUNT_FLAGS[scope]} {ACCOUNT_NAMES[scope]} · 시드 {account['initial_cash_krw']:,.0f}원 · "
        f"남은 현금 {cash_krw:,.0f}원 · 총자산 {total_value_krw:,.0f}원 · 전략 {account.get('strategy')}",
        f"환율: 1 USD = {fx['rate']:,.2f} KRW ({fx.get('source')})",
        f"대상 시장: {', '.join(markets)}",
        "",
        "보유:",
        json.dumps(held, ensure_ascii=False),
        "",
    ]
    for market in markets:
        rows = candidates.get(market) or []
        lines.append(f"후보({market}, quant_score 내림차순):")
        lines.append(json.dumps([compact_candidate(row) for row in rows], ensure_ascii=False))
        lines.append("")
    lines.append(
        "반드시 아래 JSON만 출력하라(설명·코드펜스 금지):\n"
        '{"market_note": "한 줄 요약", '
        '"sells": [{"ticker": "티커", "fraction": 1.0, "reason": "근거"}], '
        '"buys": [{"ticker": "티커", "amount_krw": 0, "reason": "근거"}]}'
    )
    return "\n".join(lines)


def _parse_ai(text: str) -> dict | None:
    """모델 응답 → dict(펜스·앞뒤 잡문 허용). 실패하면 None."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _ai_decide(prompt: str, ai_call) -> tuple[dict | None, str]:
    """AI 판단 → (결정 dict 또는 None, source). 실패는 조용히 폴백으로."""
    if ai_call is None:
        return None, "quant"
    try:
        raw = ai_call(prompt)
    except Exception as exc:
        logger.warning("모의투자 AI 판단 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        return None, "quant"
    data = _parse_ai(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else None)
    if not data:
        logger.warning("모의투자 AI 응답 해석 실패 → 정량 폴백")
        return None, "quant"
    return data, "gemini"


def _quant_plan(candidates: list[dict], cash_krw: float, held: set[str],
                total_value_krw: float) -> list[dict]:
    """AI가 없을 때: 정량 점수 상위 종목을 균등 배분(최대 3종목, 종목 상한 준수)."""
    picks = [row for row in candidates if str(row.get("ticker", "")).upper() not in held][:AI_MAX_BUYS]
    if not picks:
        return []
    per_name = cash_krw / len(picks)
    cap = max(MIN_ORDER_KRW, total_value_krw * MAX_POSITION_WEIGHT)
    amount = min(per_name, cap)
    if amount < MIN_ORDER_KRW:
        return []
    return [
        {
            "ticker": str(row.get("ticker") or "").upper(),
            "amount_krw": round(amount, 2),
            "reason": (f"AI 판단 없이 서버 정량 점수 {row.get('quant_score')} 기준 "
                       f"({', '.join(str(s) for s in (row.get('signals') or [])[:4]) or '지표 기준'})"),
        }
        for row in picks
    ]


# --- 매매 실행 ---------------------------------------------------------------


def _sell_shares(position: dict, fraction) -> float:
    """매도 비중 → 실제 주문 수량(미국 소수점 4자리·한국 1주 정수, 보유량 이하).

    비중이 너무 작아 0주가 되면 전량으로 올린다(0주 주문 방지).
    """
    currency = position.get("currency") or "USD"
    held = float(position["shares"])
    shares = _round_shares(held * float(fraction), currency)
    if shares <= 0:
        shares = held
    return min(shares, held)


def _order_skip_reason(result, action: str) -> str:
    """어댑터 주문 결과 → 실행 결과의 `skipped` 사유 문장(화면·로그에 그대로 보인다)."""
    if result.status == broker.STATUS_DRY_RUN:
        return f"{action} 드라이런 — 실제 주문을 보내지 않았습니다."
    if result.status == broker.STATUS_NOT_CONFIGURED:
        return f"{action} 실계좌 미연결(broker_not_configured) — 증권사 키 설정이 필요합니다."
    if result.status == broker.STATUS_BLOCKED:
        return f"{action} 차단({result.reason}) — 주문 안전장치가 막았습니다."
    detail = f" — {result.reason}" if result.reason else ""
    return f"{action} 미체결({result.status or 'rejected'}){detail}"


def _execute_sell(position: dict, price: float, fx_rate: float, fraction: float, reason: str,
                  run_id: str, source: str, db_path: str | None,
                  account_id: int = paper_store.ACCOUNT_US) -> "broker.OrderResult":
    """매도 **주문 경로** — 수량을 정하고 브로커 어댑터에 맡긴다(모의투자는 `_fill_sell`).

    실계좌 어댑터가 붙으면 이 함수만 바뀌지 않고 어댑터가 주문을 내보낸다(안전장치 포함).
    """
    market = position.get("market") or market_of(position["ticker"])
    currency = position.get("currency") or ("KRW" if market == "KR" else "USD")
    shares = _sell_shares(position, fraction)
    rate = 1.0 if currency == "KRW" else float(fx_rate)
    order = broker.OrderRequest(
        side="SELL", ticker=str(position["ticker"]), market=str(market),
        shares=shares, price=float(price), currency=str(currency),
        amount_krw=round(shares * float(price) * rate, 2), fx_rate=float(fx_rate),
        reason=reason, run_id=run_id, name=str(position.get("name") or ""),
        account_id=account_id, db_path=db_path,
        extra={"position": position, "fraction": fraction, "source": source},
    )
    result = broker.get_adapter(market=market, db_path=db_path,
                                account_id=account_id).place_sell(order)
    if not result.ok or result.trade is None:
        logger.info("매도 주문 미체결 %s(%s): %s %s", order.ticker, result.adapter,
                    result.status, result.reason)
    return result


def _fill_sell(position: dict, shares: float, price: float, fx_rate: float, reason: str,
               run_id: str, source: str, db_path: str | None,
               account_id: int = paper_store.ACCOUNT_US) -> dict:
    """모의투자 매도 체결 — 수수료·증권거래세를 뺀 **순수취액**이 현금으로 들어온다.

    - 총 매도대금 `gross_krw` = 주가 × 수량 × 현재환율
    - 매도 비용 `fee_krw` = gross × 매도 비용률(한국 0.195% · 미국 0.15%)
    - 실제 입금 `net_krw` = gross - fee
    - `realized_pnl_krw` = net - (매도 수량 비율 × 포지션 원가). 원가에는 매수 수수료가 들어 있어
      이 값이 곧 **수수료·세금·환차를 모두 반영한 순손익**이다.

    포지션 축소·현금 증가·체결 기록은 `paper_store.apply_sell`이 한 트랜잭션으로 처리한다.
    """
    currency = position.get("currency") or "USD"
    market = position.get("market") or market_of(position["ticker"])
    _, sell_fee_rate = fee_rates(market)
    rate = 1.0 if currency == "KRW" else fx_rate
    held = float(position["shares"])
    shares = min(float(shares), held)

    gross_krw = round(shares * float(price) * rate, 2)
    fee_krw = round(gross_krw * sell_fee_rate, 2)
    net_krw = round(gross_krw - fee_krw, 2)
    cost_share = round(position["cost_krw"] * (shares / held), 2) if held else 0.0
    realized = round(net_krw - cost_share, 2)

    return paper_store.apply_sell(
        position["ticker"], shares, cost_share, net_krw,
        {
            "run_id": run_id,
            "trade_date": paper_store.kst_today_str(),
            "ticker": position["ticker"],
            "market": market,
            "name": position.get("name") or "",
            "side": "SELL",
            "shares": shares,
            "price": float(price),
            "currency": currency,
            "fx_rate": rate,
            "amount_krw": gross_krw,
            "fee_krw": fee_krw,
            "settle_krw": net_krw,
            "realized_pnl_krw": realized,
            "reason": reason,
            "ai_source": source,
        },
        db_path,
        account_id,
    )


def _execute_buy(symbol: str, market: str, name: str, currency: str, price: float,
                 amount_krw: float, fx_rate: float, reason: str, run_id: str, source: str,
                 db_path: str | None,
                 account_id: int = paper_store.ACCOUNT_US) -> "broker.OrderResult":
    """매수 **주문 경로** — 브로커 어댑터에 맡긴다(모의투자는 `_fill_buy`).

    실계좌 어댑터가 붙으면 이 함수만 바뀌지 않고 어댑터가 주문을 내보낸다(안전장치 포함).
    """
    order = broker.OrderRequest(
        side="BUY", ticker=symbol, market=market, shares=0.0, price=float(price),
        currency=currency, amount_krw=float(amount_krw), fx_rate=float(fx_rate),
        reason=reason, run_id=run_id, name=name, account_id=account_id, db_path=db_path,
        extra={"source": source},
    )
    result = broker.get_adapter(market=market, db_path=db_path,
                                account_id=account_id).place_buy(order)
    if not result.ok or result.trade is None:
        logger.info("매수 주문 미체결 %s(%s): %s %s", order.ticker, result.adapter,
                    result.status, result.reason)
    return result


def _fill_buy(symbol: str, market: str, name: str, currency: str, price: float,
              amount_krw: float, fx_rate: float, reason: str, run_id: str, source: str,
              db_path: str | None, account_id: int = paper_store.ACCOUNT_US) -> dict | None:
    """모의투자 매수 체결 — 현금 한도·최소 주문·수량 단위를 지키고, 불가능하면 None.

    **수수료까지 예산 안에서 계산한다**: `amount_krw`는 이 종목에 쓸 수 있는 총액(수수료 포함)이고,
    `체결원금 + 매수수수료 <= 현금`이 되도록 수량을 정한다. 원가(`cost_krw`)는 원금과 매수수수료의
    합이라, 나중에 매도하면 `순수취액 - 원가`가 바로 수수료를 뺀 실현손익이 된다.

    포지션 추가·현금 차감·체결 기록은 `paper_store.apply_buy`가 한 트랜잭션으로 처리한다.
    """
    account = paper_store.get_account(account_id, db_path)
    cash = float(account["cash_krw"])
    buy_fee_rate, _ = fee_rates(market)
    budget = min(float(amount_krw), cash)
    rate = 1.0 if currency == "KRW" else float(fx_rate)
    if budget < MIN_ORDER_KRW or price <= 0:
        return None

    def _fill(budget_krw: float) -> tuple[float, float, float, float]:
        """예산 → (주수, 원금, 수수료, 총지출). 주수가 0이면 모두 0."""
        principal_budget = budget_krw / (1.0 + buy_fee_rate)
        qty = _round_shares((principal_budget / (price * rate)) - 1e-12, currency)
        if qty <= 0:
            return 0.0, 0.0, 0.0, 0.0
        principal = round(qty * price * rate, 2)
        fee = round(principal * buy_fee_rate, 2)
        return qty, principal, fee, round(principal + fee, 2)

    shares, principal_krw, fee_krw, settle_krw = _fill(budget)
    if shares <= 0:
        return None
    if settle_krw > cash:  # 반올림·환율 변동 대비 마지막 방어선
        shares, principal_krw, fee_krw, settle_krw = _fill(cash)
        if shares <= 0 or settle_krw > cash:
            return None
    if settle_krw < MIN_ORDER_KRW:
        return None
    cost_krw = round(principal_krw + fee_krw, 2)

    return paper_store.apply_buy(
        symbol, market, name or "", currency, shares, price, rate, cost_krw,
        reason, -settle_krw,
        {
            "run_id": run_id,
            "trade_date": paper_store.kst_today_str(),
            "ticker": symbol,
            "market": market,
            "name": name or "",
            "side": "BUY",
            "shares": shares,
            "price": float(price),
            "currency": currency,
            "fx_rate": rate,
            "amount_krw": principal_krw,
            "fee_krw": fee_krw,
            "settle_krw": settle_krw,
            "realized_pnl_krw": None,
            "reason": reason,
            "ai_source": source,
        },
        db_path,
        account_id,
    )


def _valuation(positions: list[dict], prices: dict, sources: dict, fx_rate: float) -> dict:
    """포지션 평가 — 원화 평가액·손익·수익률. 현재가를 못 받으면 원가로 평가한다."""
    rows = []
    stock_value = 0.0
    for position in positions:
        symbol = position["ticker"]
        currency = position.get("currency") or ("KRW" if market_of(symbol) == "KR" else "USD")
        market = position.get("market") or market_of(symbol)
        _, sell_fee_rate = fee_rates(market)
        rate = 1.0 if currency == "KRW" else fx_rate
        price = _num(prices.get(symbol))
        price_source = sources.get(symbol) or ("stored_cost" if price is None else "daily_close")
        if price is None or price <= 0:
            price = _num(position.get("avg_price")) or 0.0
            price_source = "avg_price_fallback"
        cost_krw = round(position["cost_krw"], 2)
        market_value = round(position["shares"] * price * rate, 2)
        # 지금 전량 팔면: 매도 수수료·세금을 뺀 순수취액, 그 원가 대비 순손익.
        sell_fee = round(market_value * sell_fee_rate, 2)
        net_proceeds = round(market_value - sell_fee, 2)
        net_pnl = round(net_proceeds - cost_krw, 2)
        pnl_krw = round(market_value - cost_krw, 2)
        pnl_pct = round(pnl_krw / cost_krw * 100, 2) if cost_krw > 0 else None
        net_pnl_pct = round(net_pnl / cost_krw * 100, 2) if cost_krw > 0 else None
        buy_fx = _num(position.get("fx_rate")) or 1.0
        fx_change_pct = (round((rate / buy_fx - 1.0) * 100, 2)
                         if currency == "USD" and buy_fx > 0 else None)
        stock_value += market_value
        rows.append({
            "ticker": symbol,
            "name": position.get("name") or naver_finance.static_display_name(symbol) or symbol,
            "market": market,
            "currency": currency,
            "shares": round(position["shares"], 4) if currency != "KRW" else int(position["shares"]),
            "avg_price": round(position["avg_price"], 4),
            "current_price": round(price, 4),
            "price_source": price_source,
            "fx_rate": round(rate, 2),
            "buy_fx_rate": round(buy_fx, 2),
            "fx_change_pct": fx_change_pct,
            "avg_price_krw": round(position["avg_price"] * (1.0 if currency == "KRW"
                                                            else position["fx_rate"]), 2),
            "market_value_krw": market_value,
            "cost_krw": cost_krw,
            "pnl_krw": pnl_krw,
            "pnl_pct": pnl_pct,
            "sell_fee_pct": round(sell_fee_rate * 100, 4),
            "sell_fee_krw": sell_fee,
            "net_proceeds_krw": net_proceeds,
            "net_pnl_krw": net_pnl,
            "net_pnl_pct": net_pnl_pct,
            "reason": position.get("reason") or "",
            "opened_at": position.get("opened_at"),
        })
    return {"positions": rows, "stock_value_krw": round(stock_value, 2)}


def _summary(account: dict, cash_krw: float, stock_value_krw: float) -> dict:
    initial = float(account["initial_cash_krw"]) or 0.0
    total = cash_krw + stock_value_krw
    pnl = total - initial
    return {
        "initial_cash_krw": round(initial, 2),
        "cash_krw": round(cash_krw, 2),
        "stock_value_krw": round(stock_value_krw, 2),
        "total_value_krw": round(total, 2),
        "total_pnl_krw": round(pnl, 2),
        "total_return_pct": (round(pnl / initial * 100, 2) if initial > 0 else None),
    }


def _position_views(positions: list[dict], price_fn, fx_rate: float) -> dict:
    symbols = [position["ticker"] for position in positions]
    prices, sources = ({}, {})
    if symbols:
        try:
            prices, sources = price_fn(symbols)
        except Exception as exc:  # 시세 장애 → 원가 평가로 계속
            logger.warning("모의투자 평가 시세 실패: %s: %s", type(exc).__name__, str(exc)[:160])
    return _valuation(positions, prices or {}, sources or {}, fx_rate)


# --- 실행 -------------------------------------------------------------------


def run(market=None, force: bool = False, trigger: str = "manual", db_path: str | None = None,
        ai_call=None, price_fn=None, candidate_fn=None, fx_fn=None,
        now: datetime | None = None, push_fn=None, user_id: int | None = None) -> dict:
    """지정한 시장 계좌에서 AI 모의투자 1회 실행(매도 → 매수 → 스냅샷). 동시에 두 번 돌지 않는다.

    `market`: "US"(미국 계좌) 또는 "KR"(한국 계좌). 비우면 미국 계좌.
    `user_id`: 어느 닉네임 유저의 계좌인지(기본 관리자 1).
    이미 다른 실행이 돌고 있으면 기다리지 않고 `skipped: "already_running"`을 돌려준다 —
    두 실행이 같은 현금을 보고 각자 매수하면 시드머니를 넘겨 살 수 있기 때문이다.
    """
    scope = account_scope(market)
    uid = int(user_id or paper_store.DEFAULT_USER_ID)
    if not _RUN_LOCK.acquire(blocking=False):
        return {
            "ok": True, "skipped": "already_running", "market": scope, "markets": [scope],
            "user_id": uid,
            "message": f"{account_label(scope)} 계좌의 AI 매매가 실행 중입니다. 끝나면 결과가 계좌에 반영됩니다.",
            "ran_at": paper_store._now(),
        }
    try:
        return _run_locked(market=scope, force=force, trigger=trigger, db_path=db_path,
                           ai_call=ai_call, price_fn=price_fn, candidate_fn=candidate_fn,
                           fx_fn=fx_fn, now=now, push_fn=push_fn, user_id=uid)
    finally:
        # 매매가 끝나면 상태 스냅샷은 반드시 새로 만든다(체결 직후 탭은 최신 값이어야 한다).
        invalidate_status_cache(db_path)
        _RUN_LOCK.release()


def _run_locked(market=None, force: bool = False, trigger: str = "manual",
                db_path: str | None = None, ai_call=None, price_fn=None, candidate_fn=None,
                fx_fn=None, now: datetime | None = None, push_fn=None,
                user_id: int | None = None) -> dict:
    """실제 실행 본체(`run`이 락을 잡고 부른다) — 한 시장 계좌만 건드린다.

    `market`: "US"/"KR". 그 계좌의 현금·보유·설정만 보고, 그 계좌에만 기록한다.
    `force`: False면 오늘 이미 돈 계좌는 건너뛴다(자동 실행 중복 방지).
    `ai_call(prompt) -> str|dict|None`, `price_fn(tickers) -> (prices, sources)`,
    `candidate_fn(market, criteria) -> list[dict]`, `fx_fn() -> {"rate","source"}`,
    `push_fn(sells, buys, db_path) -> dict`는 테스트·대체 경로용 주입점이다.
    """
    price_fn = price_fn or default_prices
    candidate_fn = candidate_fn or default_candidates
    fx_fn = fx_fn or get_usd_krw

    scope = account_scope(market)
    uid = int(user_id or paper_store.DEFAULT_USER_ID)
    account_id = paper_store.account_id_for(uid, scope, db_path)
    # 실계좌 모드에서는 이 실행기가 모의투자 원장을 보고 주문 금액을 정하면 안 된다
    # (가짜 잔고로 실주문을 내는 사고). 실계좌 주문 경로는 어댑터를 붙일 때 따로 연결한다
    # — docs/BROKER_INTEGRATION.md '4. 새 증권사 붙이기 → 3) 실행기 연결'.
    adapter = broker.get_adapter(market=scope, db_path=db_path, account_id=account_id)
    if adapter.is_live:
        return {
            "ok": True, "skipped": "live_broker_not_wired", "market": scope, "markets": [scope],
            "user_id": uid,
            "broker": {"mode": adapter.name, "live": True,
                       "status": "not_configured" if not adapter.configured else "wired_pending"},
            "message": (f"{account_label(scope)} 계좌는 실계좌 모드(BROKER_MODE={adapter.name})입니다. "
                        "모의투자 실행기로는 실주문을 내지 않습니다 — 모의투자는 BROKER_MODE=paper입니다."),
            "ran_at": paper_store._now(),
        }
    account = paper_store.init_store(db_path, account_id, user_id=uid)
    targets = [scope]

    today = paper_store.kst_today_str()
    if not force and (account.get(f"last_run_{scope.lower()}_date") or "") == today:
        return {
            "ok": True, "skipped": "already_ran_today", "market": scope, "markets": targets,
            "message": f"오늘은 {account_label(scope)} 계좌의 AI 매매가 이미 실행됐습니다(계좌별 하루 1회).",
            "ran_at": paper_store._now(),
        }

    run_id = uuid.uuid4().hex[:10]
    fx = fx_fn() or {"rate": DEFAULT_USDKRW, "source": "fixed_fallback"}
    fx_rate = float(fx["rate"])
    strategy = account.get("strategy") or paper_store.DEFAULT_STRATEGY
    criteria = criteria_for(strategy)

    positions = paper_store.list_positions(db_path, account_id)
    views = _position_views(positions, price_fn, fx_rate)
    view_by_ticker = {row["ticker"]: row for row in views["positions"]}
    cash = float(account["cash_krw"])
    total_value = cash + views["stock_value_krw"]

    sells: list[dict] = []
    buys: list[dict] = []
    skipped: list[dict] = []
    notes: list[str] = []

    # --- 1) 후보 수집(그 계좌의 시장만) ---
    candidates: dict[str, list[dict]] = {}
    for market in targets:
        try:
            candidates[market] = candidate_fn(market, criteria) or []
        except Exception as exc:
            logger.warning("모의투자 후보 수집 실패(%s): %s: %s", market, type(exc).__name__, str(exc)[:200])
            candidates[market] = []
            notes.append(f"{market} 후보 수집 실패 — 이번 실행에서는 매수 판단을 건너뜁니다.")

    # --- 2) 매도 판단: 규칙(손절·익절) 먼저 — 수수료·세금·환차를 반영한 순손익으로 본다 ---
    rule_sells: list[dict] = []
    ai_sells: list[dict] = []
    for position in positions:
        view = view_by_ticker.get(position["ticker"])
        if not view or view["current_price"] <= 0:
            continue
        pnl_pct = view.get("net_pnl_pct")      # 수수료·세금·환차 반영 순수익률
        if pnl_pct is None:
            continue
        gross_pct = view.get("pnl_pct")
        gross_note = f", 주가 {gross_pct:+.1f}%" if gross_pct is not None else ""
        if pnl_pct <= STOP_LOSS_PCT:
            rule_sells.append({"ticker": position["ticker"], "fraction": 1.0,
                               "reason": f"손절 기준({STOP_LOSS_PCT:.0f}%) 도달 — 수수료·세금 반영 "
                                         f"순손익 {pnl_pct:.1f}%{gross_note}"})
        elif pnl_pct >= TAKE_PROFIT_PCT:
            rule_sells.append({"ticker": position["ticker"], "fraction": 1.0,
                               "reason": f"익절 기준(+{TAKE_PROFIT_PCT:.0f}%) 도달 — 수수료·세금 반영 "
                                         f"순손익 {pnl_pct:.1f}%{gross_note}"})

    # --- 3) AI 판단(매도·매수) ---
    ai_source = "quant"
    decision = None
    has_candidates = any(candidates.get(market) for market in targets)
    if ai_call is not None and (has_candidates or positions):
        prompt = build_prompt(account, targets, cash, total_value,
                              list(view_by_ticker.values()), candidates, fx)
        decision, ai_source = _ai_decide(prompt, ai_call)
        if decision:
            notes.append(str(decision.get("market_note") or "")[:300])
            for item in decision.get("sells") or []:
                if isinstance(item, dict) and item.get("ticker"):
                    ai_sells.append(item)

    # --- 4) 매도 체결(규칙 + AI, 같은 종목이면 규칙 우선) ---
    held = {position["ticker"]: position for position in positions}
    sell_plan: dict[str, dict] = {}
    for item, item_source in ([(i, ai_source) for i in ai_sells]
                              + [(i, "rule") for i in rule_sells]):
        symbol = str(item.get("ticker") or "").strip().upper()
        if symbol not in held:
            skipped.append({"ticker": symbol, "reason": "보유하지 않은 종목(매도 무시)"})
            continue
        current = sell_plan.get(symbol)
        if current and float(current.get("fraction") or 1.0) >= float(item.get("fraction") or 1.0):
            continue
        data = dict(item)
        data["ticker"] = symbol
        data["source"] = item_source
        sell_plan[symbol] = data

    for symbol, item in sell_plan.items():
        position = held[symbol]
        view = view_by_ticker.get(symbol) or {}
        price = _num(view.get("current_price")) or 0.0
        if price <= 0:
            skipped.append({"ticker": symbol, "reason": "현재가를 받지 못해 매도 보류"})
            continue
        fraction = _num(item.get("fraction"))
        fraction = 1.0 if fraction is None else max(0.05, min(1.0, fraction))
        reason = str(item.get("reason") or "AI 판단 매도")[:300]
        result = _execute_sell(position, price, fx_rate, fraction, reason, run_id,
                               item.get("source") or ai_source, db_path, account_id)
        if not result.trade:
            skipped.append({"ticker": symbol, "reason": _order_skip_reason(result, "매도")})
            continue
        sells.append(result.trade)
        cash = float(paper_store.get_account(account_id, db_path)["cash_krw"])

    # --- 5) 매수 판단 ---
    buy_plan: list[dict] = []
    if decision and (decision.get("buys") or []):
        held_after = {p["ticker"] for p in paper_store.list_positions(db_path, account_id)}
        for item in decision["buys"]:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("ticker") or "").strip().upper()
            if not symbol:
                continue
            pool = {row["ticker"]: row for market in targets for row in candidates.get(market) or []}
            if symbol not in pool:
                skipped.append({"ticker": symbol, "reason": "후보 목록에 없는 종목(매수 무시)"})
                continue
            if symbol in held_after:
                skipped.append({"ticker": symbol, "reason": "이미 보유 중(중복 매수 생략)"})
                continue
            amount = _num(item.get("amount_krw"))
            if amount is None or amount < MIN_ORDER_KRW:
                skipped.append({"ticker": symbol, "reason": "매수 금액이 최소 주문 금액보다 작음"})
                continue
            row = pool[symbol]
            buy_plan.append({"ticker": symbol, "amount_krw": amount,
                             "reason": str(item.get("reason") or "AI 판단 매수")[:300],
                             "row": row, "source": ai_source})
    else:
        combined = [row for market in targets for row in candidates.get(market) or []]
        held_now = {p["ticker"] for p in paper_store.list_positions(db_path, account_id)}
        for item in _quant_plan(combined, cash, held_now, total_value):
            pool = {row["ticker"]: row for market in targets for row in candidates.get(market) or []}
            row = pool.get(item["ticker"])
            if row:
                buy_plan.append({**item, "row": row, "source": "quant"})

    # --- 6) 매수 체결: 현금·비중·종목 수 한도 ---
    for item in buy_plan:
        row = item["row"]
        symbol = item["ticker"]
        positions_now = paper_store.list_positions(db_path, account_id)
        if len(positions_now) >= MAX_POSITIONS:
            skipped.append({"ticker": symbol, "reason": f"최대 보유 종목 수({MAX_POSITIONS}) 도달"})
            continue
        account_now = paper_store.get_account(account_id, db_path)
        cash_now = float(account_now["cash_krw"])
        if cash_now < MIN_ORDER_KRW:
            skipped.append({"ticker": symbol, "reason": "남은 현금 부족"})
            continue
        currency = row.get("currency") or ("KRW" if market_of(symbol) == "KR" else "USD")
        price = _num(row.get("price")) or 0.0
        if price <= 0:
            skipped.append({"ticker": symbol, "reason": "현재가 없음"})
            continue
        views_now = _position_views(positions_now, price_fn, fx_rate)
        total_now = cash_now + views_now["stock_value_krw"]
        cap_remaining = total_now * MAX_POSITION_WEIGHT - 0.0
        amount = min(float(item["amount_krw"]), cash_now, cap_remaining)
        if amount < MIN_ORDER_KRW:
            skipped.append({"ticker": symbol, "reason": "종목 비중 상한 때문에 금액이 너무 작음"})
            continue
        result = _execute_buy(symbol, market_of(symbol), str(row.get("name") or ""), currency,
                              price, amount, fx_rate, item.get("reason") or "", run_id,
                              item.get("source") or ai_source, db_path, account_id)
        if result.trade:
            buys.append(result.trade)
        else:
            skipped.append({"ticker": symbol, "reason": _order_skip_reason(result, "매수")})

    # --- 6.5) 체결 푸시: 실제 체결이 1건 이상일 때만 등록된 기기로 바로 보낸다 ---
    # 발송은 fcm 안에서 예외·시간 초과를 흡수하므로, 여기서 실패해도 매매·기록은 그대로 끝난다.
    push: dict | None = None
    if sells or buys:
        sender = push_fn or (lambda s, b, p: fcm.notify_fills(s, b, p, user_id=uid))
        try:
            push = sender(sells, buys, db_path)
        except Exception as exc:  # 주입된 발송기까지 막아 둔다
            logger.warning("체결 푸시 생략(%s: %s)", type(exc).__name__, str(exc)[:200])
            push = {"sent": 0, "failed": 0, "error": "push_failed"}

    # --- 7) 스냅샷·실행 기록(그 계좌만) ---
    final_positions = paper_store.list_positions(db_path, account_id)
    final_views = _position_views(final_positions, price_fn, fx_rate)
    final_account = paper_store.get_account(account_id, db_path)
    summary = _summary(final_account, float(final_account["cash_krw"]),
                       final_views["stock_value_krw"])
    paper_store.record_equity(today, summary["total_value_krw"], float(final_account["cash_krw"]),
                              final_views["stock_value_krw"], summary["total_return_pct"],
                              db_path, account_id)
    fields = {"last_run_date": today}
    for market in targets:
        fields[f"last_run_{market.lower()}_date"] = today
    paper_store.update_account(fields, db_path, account_id)

    result = {
        "ok": True,
        "run_id": run_id,
        "trigger": trigger,
        "market": scope,
        "account": {"id": account_id, "market": scope, "label": account_label(scope)},
        "markets": targets,
        "strategy": strategy,
        "ai_source": ai_source,
        "fx": fx,
        "fee_policy": fee_policy(),
        "sells": sells,
        "buys": buys,
        "skipped": skipped,
        "notes": [note for note in notes if note],
        "summary": summary,
        "positions_count": len(final_positions),
        "push": push,
        "ran_at": paper_store._now(),
    }
    logger.info("모의투자 실행 %s(%s 계좌): 매도 %d · 매수 %d · 총자산 ₩%.0f(%s%%) · AI %s",
                run_id, scope, len(sells), len(buys), summary["total_value_krw"],
                summary["total_return_pct"], ai_source)
    return result


# --- 상태 -------------------------------------------------------------------


def _status_cache_enabled() -> bool:
    """`PAPER_STATUS_CACHE=0`이면 캐시 없이 매번 새로 계산한다(진단·테스트 스위치)."""
    return (os.environ.get("PAPER_STATUS_CACHE") or "1").strip() not in ("0", "false", "False")


def _cache_key(db_path: str | None, user_id: int | None = None) -> str:
    """상태 캐시 키 — 유저마다 계좌가 다르므로 유저를 키에 넣는다."""
    return f"{db_path or 'default'}::u{int(user_id or paper_store.DEFAULT_USER_ID)}"


def _cached_snapshot(db_path: str | None, user_id: int | None = None) -> dict | None:
    with _STATUS_LOCK:
        return _STATUS_CACHE.get(_cache_key(db_path, user_id))


def _store_snapshot(db_path: str | None, snapshot: dict,
                    user_id: int | None = None) -> None:
    with _STATUS_LOCK:
        _STATUS_CACHE[_cache_key(db_path, user_id)] = {"ts": time.time(), "snapshot": snapshot}


def invalidate_status_cache(db_path: str | None = None) -> None:
    """상태·랭킹 캐시를 지운다 — 매매·설정 변경 직후엔 항상 최신 값을 보여야 한다."""
    global _STATUS_GENERATION
    with _STATUS_LOCK:
        _STATUS_GENERATION += 1   # 이전에 시작한 백그라운드 갱신은 결과를 버린다
        if db_path is None:
            _STATUS_CACHE.clear()
            _RANK_CACHE.clear()
        else:
            prefix = f"{db_path}::u"
            for key in [k for k in _STATUS_CACHE if k == db_path or k.startswith(prefix)]:
                _STATUS_CACHE.pop(key, None)
            _RANK_CACHE.pop(str(db_path), None)


def _timed_call(fn, *args) -> tuple[object, Exception | None, float]:
    """함수 하나를 부르고 (결과, 예외, 소요 ms)를 돌려준다(단계별 소요 측정용)."""
    start = time.perf_counter()
    try:
        return fn(*args), None, (time.perf_counter() - start) * 1000
    except Exception as exc:  # 호출부가 단계별로 흡수한다
        return None, exc, (time.perf_counter() - start) * 1000


def _build_snapshot(db_path: str | None = None, price_fn=None, fx_fn=None,
                    limit_trades: int = 40, limit_equity: int = 180,
                    user_id: int | None = None) -> dict:
    """한 유저의 두 계좌 평가 스냅샷 — DB 1회 · 환율/시세 병렬 · 계좌별 평가.

    단계별 소요(ms)를 `timings`에 남긴다 — 탭 전환이 느릴 때 어느 단계인지 응답만 보고 안다.
    `price_fn`/`fx_fn`은 테스트·대체 경로 주입점이다(없으면 실시간 시세·네이버 환율).
    """
    started = time.perf_counter()
    data = paper_store.snapshot(limit_trades=limit_trades, limit_equity=limit_equity,
                                db_path=db_path, user_id=user_id)
    db_ms = (time.perf_counter() - started) * 1000
    account_ids = list(data["accounts"])

    tickers: list[str] = []
    for aid in account_ids:
        for row in data["positions"][aid]:
            if row["ticker"] not in tickers:
                tickers.append(row["ticker"])

    # 환율과 시세는 서로 상관이 없다 — 같이 부른다(순차 1.5초 → 병렬 1초 안쪽).
    fx: object = None
    fx_error: Exception | None = None
    fx_ms = 0.0
    prices: dict = {}
    sources: dict = {}
    price_error: Exception | None = None
    prices_ms = 0.0
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="paper-status") as pool:
        fx_future = pool.submit(_timed_call, fx_fn or get_usd_krw)
        price_future = (pool.submit(_timed_call, price_fn or default_prices, tickers)
                        if tickers else None)
        fx, fx_error, fx_ms = fx_future.result()
        if price_future is not None:
            got, price_error, prices_ms = price_future.result()
            if isinstance(got, tuple) and len(got) == 2:  # (가격, 출처)
                prices = got[0] or {}
                sources = got[1] or {}

    if fx_error is not None:
        logger.warning("모의투자 환율 조회 실패: %s: %s",
                       type(fx_error).__name__, str(fx_error)[:160])
    if not isinstance(fx, dict) or not _num(fx.get("rate")):
        fx = {"rate": DEFAULT_USD_KRW, "source": "fixed_fallback"}
    if price_error is not None:  # 시세 장애 → 원가 평가로 계속(응답은 정상)
        logger.warning("모의투자 평가 시세 실패: %s: %s",
                       type(price_error).__name__, str(price_error)[:160])

    fx_rate = float(fx["rate"])
    views = {aid: _valuation(data["positions"][aid], prices, sources, fx_rate)
             for aid in account_ids}

    rows: dict[str, dict] = {}
    for aid in account_ids:
        account = data["accounts"][aid]
        mkt = account["market_scope"]
        summary = _summary(account, account["cash_krw"], views[aid]["stock_value_krw"])
        rows[mkt] = {
            **summary,
            "account_id": aid,
            "market": mkt,
            "label": account_label(mkt),
            "positions_count": len(data["positions"][aid]),
            "realized_pnl_krw": data["realized"][aid],
            "strategy": account["strategy"],
            "strategy_label": paper_store.STRATEGY_LABELS.get(account["strategy"],
                                                              account["strategy"]),
            "auto_enabled": account["auto_enabled"],
            "last_run_date": account.get(f"last_run_{mkt.lower()}_date"),
        }

    return {
        "rows": rows,
        "views": views,
        "accounts": data["accounts"],
        "positions": data["positions"],
        "trades": data["trades"],
        "equity": data["equity"],
        "fx": fx,
        "fx_rate": fx_rate,
        "today": paper_store.kst_today_str(),
        "storage_backend": data["storage_backend"],
        "as_of": paper_store._now(),
        "timings": {
            "db_ms": round(db_ms, 1),
            "fx_ms": round(fx_ms, 1),
            "prices_ms": round(prices_ms, 1),
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    }


def _refresh_in_background(key: str, db_path: str | None, price_fn, fx_fn,
                          limit_trades: int, limit_equity: int,
                          user_id: int | None = None) -> None:
    """만료된 스냅샷을 뒤에서 새로 만든다(기다리던 요청은 옛 값을 바로 받는다)."""
    with _STATUS_LOCK:
        if key in _STATUS_REFRESHING:
            return
        _STATUS_REFRESHING.add(key)
        generation = _STATUS_GENERATION

    def _work() -> None:
        try:
            with _STATUS_BUILD_LOCK:
                # 기본 관리자 경로는 예전 호출 모양(5인자)을 그대로 쓴다 — 기존 주입·테스트 호환.
                if int(user_id or paper_store.DEFAULT_USER_ID) == paper_store.DEFAULT_USER_ID:
                    snapshot = _build_snapshot(db_path, price_fn, fx_fn, limit_trades,
                                               limit_equity)
                else:
                    snapshot = _build_snapshot(db_path, price_fn, fx_fn, limit_trades,
                                               limit_equity, user_id=user_id)
                with _STATUS_LOCK:  # 만드는 사이 매매·설정이 있었다면 옛 상태를 덮어쓰지 않는다
                    stale = generation != _STATUS_GENERATION
                if not stale:
                    _store_snapshot(db_path, snapshot, user_id)
        except Exception as exc:  # 백그라운드 실패는 응답을 막지 않는다
            logger.warning("모의투자 상태 백그라운드 갱신 실패: %s: %s",
                           type(exc).__name__, str(exc)[:160])
        finally:
            with _STATUS_LOCK:
                _STATUS_REFRESHING.discard(key)

    threading.Thread(target=_work, name="paper-status-refresh", daemon=True).start()


def _snapshot(db_path: str | None = None, price_fn=None, fx_fn=None, limit_trades: int = 40,
              limit_equity: int = 180, use_cache: bool = True,
              user_id: int | None = None) -> tuple[dict, bool, float]:
    """`(스냅샷, 캐시 적중 여부, 캐시 나이 초)` — 유저별로 따로 캐시한다.

    - `STATUS_TTL`(25초) 안이면 그대로 재사용 — 미국↔한국 탭을 오가도 즉시 응답.
    - `STATUS_STALE_SEC`(3분) 안이면 옛 값을 먼저 주고 백그라운드에서 새로 채운다.
    - 동시에 여러 요청이 들어와도 만드는 일은 한 번(`_STATUS_BUILD_LOCK`).
    """
    key = _cache_key(db_path, user_id)
    if use_cache:
        item = _cached_snapshot(db_path, user_id)
        if item:
            age = max(0.0, time.time() - float(item.get("ts") or 0.0))
            if age < STATUS_TTL:
                return item["snapshot"], True, age
            if age < STATUS_STALE_SEC:
                _refresh_in_background(key, db_path, price_fn, fx_fn, limit_trades, limit_equity,
                                       user_id)
                return item["snapshot"], True, age
    seen = _cached_snapshot(db_path, user_id) if use_cache else None  # 락 대기 중 새 결과 구분용
    with _STATUS_LOCK:
        generation = _STATUS_GENERATION
    with _STATUS_BUILD_LOCK:
        if use_cache:
            item = _cached_snapshot(db_path, user_id)
            if item is not None and item is not seen:
                age = max(0.0, time.time() - float(item.get("ts") or 0.0))
                if age < STATUS_TTL:
                    return item["snapshot"], True, age
        if int(user_id or paper_store.DEFAULT_USER_ID) == paper_store.DEFAULT_USER_ID:
            # 기본 관리자 경로는 예전 호출 모양(5인자)을 그대로 쓴다 — 기존 주입·테스트 호환.
            snapshot = _build_snapshot(db_path, price_fn, fx_fn, limit_trades, limit_equity)
        else:
            snapshot = _build_snapshot(db_path, price_fn, fx_fn, limit_trades, limit_equity,
                                       user_id=user_id)
        with _STATUS_LOCK:  # 계산 중 매매·설정이 있었으면 이 결과는 버린다(다음 요청이 새로 만든다)
            stale = generation != _STATUS_GENERATION
        if price_fn is None and fx_fn is None and not stale:
            # 주입된 대체 경로 결과는 캐시에 남기지 않는다
            _store_snapshot(db_path, snapshot, user_id)
        return snapshot, False, 0.0


def warm_status_cache(delay: float | None = None) -> None:
    """상태 스냅샷을 한 번 미리 만들어 둔다(테스트·수동용). 계속 돌리려면 `start_status_warm`."""
    if (os.environ.get("PAPER_STATUS_PREWARM") or "1").strip() in ("0", "false", "False"):
        return
    if not _status_cache_enabled():
        return
    wait = STATUS_WARM_DELAY if delay is None else max(0.0, float(delay))

    def _work() -> None:
        if wait > 0:
            time.sleep(wait)
        _warm_once()

    threading.Thread(target=_work, name="paper-status-warm", daemon=True).start()


def _warm_once() -> bool:
    """캐시가 비었거나 TTL이 지났으면 스냅샷을 새로 만들어 둔다. 실패는 삼킨다.

    같은 주기로 지인 랭킹도 미리 계산해 둔다 — 사용자가 랭킹 카드를 열 때 시세·DB를
    기다리지 않고 `PAPER_RANK_TTL`(90초) 안의 결과를 바로 받는다.
    """
    try:
        rebuilt = False
        with _STATUS_BUILD_LOCK:
            item = _cached_snapshot(None)
            age = time.time() - float(item.get("ts") or 0.0) if item else None
            if age is None or age >= STATUS_TTL:
                _store_snapshot(None, _build_snapshot())
                rebuilt = True
        rank = _RANK_CACHE.get("default")
        rank_age = time.time() - float(rank.get("ts") or 0.0) if rank else None
        if rank_age is None or rank_age >= RANK_TTL:
            try:
                leaderboard()
            except Exception as exc:  # 랭킹 예열 실패는 다음 주기에 다시 시도한다
                logger.warning("지인 랭킹 예열 실패: %s: %s", type(exc).__name__, str(exc)[:160])
        return rebuilt
    except Exception as exc:  # 예열 실패는 다음 요청·다음 주기가 다시 시도한다
        logger.warning("모의투자 상태 예열 실패: %s: %s", type(exc).__name__, str(exc)[:160])
        return False


def start_status_warm(delay: float | None = None, interval: float | None = None) -> bool:
    """기동 뒤 상태 스냅샷을 **주기적으로** 미리 채운다 — 앱을 열 때마다 기다리지 않게.

    기본은 20초 뒤 시작해 `PAPER_STATUS_WARM_INTERVAL`(60초)마다 한 번. 백그라운드에서
    만들어 두므로 사용자 요청은 캐시를 바로 받는다(가장 오래된 값도 1분 안쪽).
    `PAPER_STATUS_PREWARM=0`이면 시작하지 않는다 — 단위 테스트·로컬 실행용.
    """
    if (os.environ.get("PAPER_STATUS_PREWARM") or "1").strip() in ("0", "false", "False"):
        return False
    if not _status_cache_enabled():
        return False
    wait = STATUS_WARM_DELAY if delay is None else max(0.0, float(delay))
    period = max(5.0, STATUS_WARM_INTERVAL if interval is None else float(interval))

    def _loop() -> None:
        if wait > 0:
            time.sleep(wait)
        while True:
            _warm_once()
            time.sleep(period)

    threading.Thread(target=_loop, name="paper-status-warm", daemon=True).start()
    return True


def status(db_path: str | None = None, price_fn=None, fx_fn=None,
           limit_trades: int = 40, limit_equity: int = 180, market: str | None = None,
           use_cache: bool | None = None, user_id: int | None = None,
           include_broker: bool | None = None) -> dict:
    """한 계좌의 상세(보유·체결·수익률) + **그 유저의 두 계좌를 나란히 비교하는 요약**.

    반환: `market`이 지정한 계좌(기본 미국)의 `account`·`positions`·`trades`·`equity_history`와
    함께, `accounts`(US·KR 각각의 총자산·수익률·현금·보유 수·마지막 실행일)와 앱이
    순서대로 그리기 쉬운 `comparison` 목록을 담는다.

    두 계좌의 평가를 **한 번에** 만들어 `STATUS_TTL`(기본 25초) 동안 재사용한다 — 앱이
    미국↔한국 탭을 오갈 때마다 시세·DB를 다시 부르지 않는다. `use_cache=False`면 새로
    계산한다(매매·설정 직후 응답). `cache`·`timings`가 어느 단계가 얼마나 걸렸는지 알려준다.
    """
    uid = int(user_id or paper_store.DEFAULT_USER_ID)
    scope = account_scope(market)
    user = paper_auth.get_user(uid, db_path) or {
        "id": uid, "nickname": "", "is_admin": uid == paper_store.DEFAULT_USER_ID}
    account_id = paper_store.account_id_for(uid, scope, db_path)
    if use_cache is None:
        use_cache = _status_cache_enabled() and price_fn is None and fx_fn is None
    snapshot, cache_hit, cache_age = _snapshot(
        db_path, price_fn, fx_fn, limit_trades, limit_equity, use_cache=bool(use_cache),
        user_id=uid)

    accounts = snapshot["accounts"]
    rows = snapshot["rows"]
    views = snapshot["views"]
    today = snapshot["today"]
    account = accounts[account_id]
    summary = _summary(account, account["cash_krw"], views[account_id]["stock_value_krw"])
    schedule = [
        {"market": acc["market_scope"], "account_id": aid,
         "label": f"{ACCOUNT_NAMES[acc['market_scope']]} 장 마감 후",
         "at": f"{RUN_AT[acc['market_scope']][0]:02d}:{RUN_AT[acc['market_scope']][1]:02d}",
         "enabled": acc["auto_enabled"],
         "done_today": (acc.get(f"last_run_{acc['market_scope'].lower()}_date") or "") == today}
        for aid, acc in sorted(accounts.items(), key=lambda item: item[1]["market_scope"] != "KR")
    ]
    is_admin = bool(user.get("is_admin"))
    show_broker = is_admin if include_broker is None else bool(include_broker and is_admin)
    return {
        "market": scope,
        "user": paper_auth.public_user(user),
        "account": {
            **summary,
            "id": account_id,
            "market_scope": scope,
            "markets": scope,
            "label": account_label(scope),
            "strategy": account["strategy"],
            "strategy_label": paper_store.STRATEGY_LABELS.get(account["strategy"],
                                                              account["strategy"]),
            "auto_enabled": account["auto_enabled"],
            "last_run_date": account.get("last_run_date"),
            "last_run_us_date": account.get("last_run_us_date"),
            "last_run_kr_date": account.get("last_run_kr_date"),
            "realized_pnl_krw": rows[scope]["realized_pnl_krw"],
            "fx_rate": round(snapshot["fx_rate"], 2),
            "fx_source": snapshot["fx"].get("source"),
            "created_at": account.get("created_at"),
            "updated_at": account.get("updated_at"),
        },
        "positions": views[account_id]["positions"],
        "trades": snapshot["trades"][account_id],
        "equity_history": snapshot["equity"][account_id],
        "accounts": rows,
        "comparison": [rows["US"], rows["KR"]],
        "auto_schedule": schedule,
        "next_auto": next_auto_info(account),
        "fee_policy": fee_policy(),
        # 실제 계좌(테스트) 카드 — 어댑터 모드·킬 스위치·한도·키 등록 여부(마스킹).
        # 키 원문은 없다. 캐시된 스냅샷과 별개로 매번 만들지만 조회는 20초 캐시를 탄다.
        "broker": broker_credentials.full_status(db_path) if show_broker else None,
        "as_of": snapshot["as_of"],
        "storage_backend": snapshot["storage_backend"],
        "cache": {"hit": cache_hit, "age_sec": round(cache_age, 1), "ttl_sec": STATUS_TTL},
        "timings": snapshot["timings"],
    }

def next_auto_info(account: dict, now: datetime | None = None) -> dict:
    """계좌 하나의 다음 자동 실행 예정(오늘 남은 것 또는 내일 것) — 화면 안내용."""
    if not account.get("auto_enabled"):
        return {"enabled": False, "label": "자동 실행 꺼짐"}
    scope = account_scope(account.get("market_scope") or account.get("markets") or account.get("id"))
    label = ACCOUNT_NAMES[scope]
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    hour, minute = RUN_AT[scope]
    today = current.date().isoformat()
    # 지금 시각이 이미 지난 실행 시각이고 오늘 아직 안 돌았으면, 다음 일정이 아니라 '곧 실행'이다
    # (서버가 그 시각에 꺼져 있었다가 켜지면 자동 루프가 따라잡아 실행한다).
    if (current.hour * 60 + current.minute) >= hour * 60 + minute and \
            (account.get(f"last_run_{scope.lower()}_date") or "") != today and \
            _AUTO_FAILURES.get((scope, today), 0) < 3:
        return {"enabled": True, "market": scope,
                "at": current.isoformat(timespec="minutes"),
                "label": f"{label} 계좌 · 곧 자동 실행(오늘 몫 따라잡기)"}
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    done = (account.get(f"last_run_{scope.lower()}_date") or "") == target.date().isoformat()
    if target <= current or done:
        target += timedelta(days=1)
    return {"enabled": True, "market": scope,
            "at": target.isoformat(timespec="minutes"),
            "label": f"{label} 계좌 · {target.strftime('%m/%d %H:%M')} KST"}


# --- 설정 -------------------------------------------------------------------


def configure(account: str | None = None, initial_cash_krw=None, markets=None, strategy=None,
              auto_enabled=None, reset: bool = False, db_path: str | None = None,
              user_id: int | None = None) -> dict:
    """한 계좌(미국/한국)의 시드머니·전략·자동 실행 설정. `reset=True`면 그 계좌만 초기화한다.

    검증: 시드머니 10만~10억 원, 전략 4종.
    `reset=False`에서 시드머니를 줄이면 현금에서 차감한다. 보유 종목에 이미 들어간 돈은
    건드리지 않으므로, 현금보다 큰 폭으로 줄이면 400(`cash_below_zero`)으로 거절한다.

    `market`: 설정할 계좌(`US`/`KR`). `markets`는 예전 클라이언트가 보내는 값을 받아만 두고
    무시한다 — 계좌의 시장은 계좌가 정한다(미국 계좌는 미국 종목만 산다).
    """
    scope = account_scope(account)
    uid = int(user_id or paper_store.DEFAULT_USER_ID)
    account_id = paper_store.account_id_for(uid, scope, db_path)
    # 계좌 한 행만 필요하다 — 연결 1회로 읽는다(예전 init_store는 연결 5번 = 10초 넘음).
    current = paper_store.ensure_accounts(db_path, uid)[account_id]

    seed = None
    if initial_cash_krw is not None:
        seed = _num(initial_cash_krw)
        if seed is None:
            raise ValueError("initial_cash_krw_invalid")
        if seed < paper_store.MIN_INITIAL_CASH_KRW or seed > paper_store.MAX_INITIAL_CASH_KRW:
            raise ValueError("initial_cash_krw_out_of_range")

    strategy_value = None
    if strategy is not None:
        strategy_value = str(strategy).strip().lower()
        if strategy_value not in paper_store.STRATEGIES:
            raise ValueError("strategy_invalid")

    if reset:
        updated = paper_store.reset(db_path, initial_cash_krw=seed, account_id=account_id)
        patch: dict = {}
        if strategy_value:
            patch["strategy"] = strategy_value
        if auto_enabled is not None:
            patch["auto_enabled"] = 1 if auto_enabled else 0
        if patch:
            updated = paper_store.update_account(patch, db_path, account_id)
        invalidate_status_cache(db_path)  # 초기화 직후 탭은 초기 상태를 보여야 한다
        return {"ok": True, "reset": True, "market": scope, "account": updated}

    patch = {}
    if seed is not None and abs(seed - current["initial_cash_krw"]) > 0.5:
        delta = seed - current["initial_cash_krw"]
        new_cash = float(current["cash_krw"]) + delta
        if new_cash < 0:
            raise ValueError("cash_below_zero")
        patch["initial_cash_krw"] = seed
        patch["cash_krw"] = round(new_cash, 2)
    if strategy_value:
        patch["strategy"] = strategy_value
    if auto_enabled is not None:
        patch["auto_enabled"] = 1 if auto_enabled else 0
    updated = paper_store.update_account(patch, db_path, account_id) if patch else current
    invalidate_status_cache(db_path)  # 설정 변경(시드·전략·자동 실행)도 즉시 반영
    return {"ok": True, "reset": False, "market": scope, "account": updated}


# --- 자동 실행 ---------------------------------------------------------------


def auto_due_markets(now: datetime | None = None) -> list[str]:
    """지금 자동 실행해야 할 시장 계좌 목록(계좌별 설정·시각·오늘 실행 여부).

    계좌별로 따로 본다: 미국 계좌가 오늘 돌았어도 한국 계좌가 아직이면 `KR`은 여기에 들어온다.
    """
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    today = current.date().isoformat()
    minutes = current.hour * 60 + current.minute
    due: list[str] = []
    for market in ACCOUNT_ORDER:
        hour, minute = RUN_AT[market]
        if minutes < hour * 60 + minute:
            continue
        if _AUTO_FAILURES.get((market, today), 0) >= 3:
            continue
        try:
            account = paper_store.get_account(account_id_of(market))
        except Exception as exc:  # 저장소 장애 — 루프는 계속 돈다
            logger.warning("모의투자 자동 실행 판단 실패(%s): %s", market, type(exc).__name__)
            continue
        if not account.get("auto_enabled"):
            continue
        if (account.get(f"last_run_{market.lower()}_date") or "") == today:
            continue
        due.append(market)
    return due


def auto_due_accounts(now: datetime | None = None,
                      include_admin: bool = False) -> list[tuple[int, str]]:
    """지금 자동 실행해야 할 `(user_id, 시장)` 목록 — 가입한 모든 유저의 계좌를 순서대로.

    `include_admin=False`면 기본 관리자(user 1)는 빼고 본다(`_auto_tick`이 기존
    `auto_due_markets()` 결과와 합친다 — 기존 동작·테스트를 그대로 두기 위해서다).
    """
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    today = current.date().isoformat()
    minutes = current.hour * 60 + current.minute
    due: list[tuple[int, str]] = []
    try:
        users = paper_auth.list_users()
    except Exception as exc:  # 사용자 테이블 장애 — 관리자 계좌만 돈다
        logger.warning("모의투자 유저 목록 조회 실패: %s: %s", type(exc).__name__, str(exc)[:160])
        users = []
    user_ids = [int(user["id"]) for user in users]
    if include_admin and paper_store.DEFAULT_USER_ID not in user_ids:
        user_ids.insert(0, paper_store.DEFAULT_USER_ID)
    for uid in user_ids:
        if not include_admin and uid == paper_store.DEFAULT_USER_ID:
            continue
        try:
            accounts = paper_store.ensure_accounts(user_id=uid)
        except Exception as exc:  # 저장소 장애 — 다음 유저로
            logger.warning("모의투자 자동 실행 계좌 조회 실패(user %s): %s: %s",
                           uid, type(exc).__name__, str(exc)[:160])
            continue
        by_market = {acc["market_scope"]: acc for acc in accounts.values()}
        for market in ACCOUNT_ORDER:
            hour, minute = RUN_AT[market]
            if minutes < hour * 60 + minute:
                continue
            if _AUTO_FAILURES.get(_failure_key(uid, market, today), 0) >= 3:
                continue
            account = by_market.get(market)
            if not account or not account.get("auto_enabled"):
                continue
            if (account.get(f"last_run_{market.lower()}_date") or "") == today:
                continue
            due.append((uid, market))
    return due


def _failure_key(user_id: int, market: str, today: str | None = None) -> tuple:
    """자동 실행 실패 카운터 키 — 관리자는 예전 키(`(시장, 날짜)`)를 그대로 쓴다."""
    day = today or paper_store.kst_today_str()
    if int(user_id) == paper_store.DEFAULT_USER_ID:
        return (market, day)
    return (f"u{int(user_id)}", market, day)


def _auto_tick(ai_call=None) -> dict | None:
    """하루 1회씩 밀린 계좌를 **순서대로** 실행한다.

    같은 실행 안에서 같은 `(시장, 전략)`의 스크리너 후보는 한 번만 수집해 모든 유저가 나눠 쓴다
    (`default_candidates`는 S&P 500·코스피 200을 훑는 무거운 작업이라 유저마다 다시 돌리면
    시간·메모리가 배로 든다). 계좌 사이마다 메모리 여유를 확인하고, 부족하면 남은 계좌는
    다음 주기(5분 뒤)로 미룬다.
    """
    legacy_due = [(paper_store.DEFAULT_USER_ID, market) for market in auto_due_markets()]
    due = legacy_due + [pair for pair in auto_due_accounts() if pair not in legacy_due]
    AUTO_STATE["last_check"] = paper_store._now()
    if not due:
        return None

    candidate_cache: dict[tuple[str, str], list[dict]] = {}

    def memo_candidates(market: str, criteria: str) -> list[dict]:
        key = (str(market), str(criteria))
        if key not in candidate_cache:
            candidate_cache[key] = default_candidates(market, criteria) or []
        return candidate_cache[key]

    results: list[dict] = []
    for index, (uid, market) in enumerate(due):
        if index and not memory_guard.headroom(need_mb=32, wait=5.0):
            AUTO_STATE["last_error"] = {"user_id": uid, "market": market,
                                        "at": paper_store._now(), "type": "MemoryGuard",
                                        "message": "메모리 여유 부족 — 남은 계좌는 다음 주기로 미룸"}
            logger.warning("모의투자 자동 실행: 메모리 여유 부족 — 남은 %d개 계좌를 다음 주기로 미룸",
                           len(due) - index)
            break
        try:
            result = run(market=market, force=False, trigger="auto", ai_call=ai_call,
                         candidate_fn=memo_candidates, user_id=uid)
            results.append(result)
            AUTO_STATE["last_run"] = {"user_id": uid, "market": market,
                                      "label": account_label(market),
                                      "at": paper_store._now(),
                                      "summary": result.get("summary"),
                                      "buys": len(result.get("buys") or []),
                                      "sells": len(result.get("sells") or [])}
            AUTO_STATE["last_error"] = None
        except Exception as exc:
            today = paper_store.kst_today_str()
            key = _failure_key(uid, market, today)
            _AUTO_FAILURES[key] = _AUTO_FAILURES.get(key, 0) + 1
            AUTO_STATE["last_error"] = {"user_id": uid, "market": market,
                                        "at": paper_store._now(), "type": type(exc).__name__,
                                        "message": str(exc)[:300]}
            logger.exception("모의투자 자동 실행 실패(user %s, %s, %d/3)", uid, market,
                             _AUTO_FAILURES[key])
    if len(results) <= 1:
        return results[0] if results else None
    return {"ok": True, "runs": results, "ran_at": paper_store._now()}


def _auto_loop(ai_call=None) -> None:
    if _AUTO_STOP.wait(AUTO_START_GRACE_SEC):
        return
    while not _AUTO_STOP.is_set():
        try:
            _auto_tick(ai_call)
        except Exception:  # 루프는 절대 죽지 않는다
            logger.exception("모의투자 자동 루프 오류")
        if _AUTO_STOP.wait(AUTO_INTERVAL_SEC):
            return


def start_auto(ai_call=None) -> bool:
    """자동 실행 루프 시작(이미 돌고 있으면 아무 것도 하지 않는다). `PAPER_AUTO=0`이면 끔."""
    global _AUTO_THREAD
    if (os.environ.get("PAPER_AUTO") or "1").strip() in ("0", "false", "False"):
        return False
    with _FX_LOCK:  # 다른 락 하나로 스레드 생성만 직렬화(단순화)
        if _AUTO_THREAD and _AUTO_THREAD.is_alive():
            return False
        _AUTO_STOP.clear()
        _AUTO_THREAD = threading.Thread(target=_auto_loop, args=(ai_call,), name="paper-auto",
                                        daemon=True)
        _AUTO_THREAD.start()
        AUTO_STATE["started"] = True
        logger.info("모의투자 자동 실행 루프 시작(한국 %02d:%02d · 미국 %02d:%02d KST)",
                    *KR_RUN_AT, *US_RUN_AT)
        return True


def stop_auto() -> None:
    _AUTO_STOP.set()
    AUTO_STATE["started"] = False


def auto_status() -> dict:
    return {
        "enabled": (os.environ.get("PAPER_AUTO") or "1").strip() not in ("0", "false", "False"),
        "running": bool(_AUTO_THREAD and _AUTO_THREAD.is_alive()),
        "kr_run_at": f"{KR_RUN_AT[0]:02d}:{KR_RUN_AT[1]:02d}",
        "us_run_at": f"{US_RUN_AT[0]:02d}:{US_RUN_AT[1]:02d}",
        **{key: AUTO_STATE.get(key) for key in ("last_check", "last_run", "last_error")},
    }


def leaderboard(db_path: str | None = None, price_fn=None, fx_fn=None) -> dict:
    """지인 수익률 랭킹 — 모든 유저의 미국·한국 계좌를 한 번에 평가해 합산 수익률 순으로 돌려준다.

    시세·환율은 **한 번만** 조회해 모든 유저의 보유 종목을 한꺼번에 평가한다(유저마다 부르면
    지인 5명이면 시세를 5번 받는다). 응답에는 닉네임·수익률·총자산만 넣고 PIN·토큰·계좌
    비밀값은 절대 넣지 않는다. `PAPER_RANK_TTL`(기본 30초) 동안 결과를 재사용한다.
    """
    cache_key = str(db_path or "default")
    if price_fn is None and fx_fn is None:
        cached = _RANK_CACHE.get(cache_key)
        if cached and time.time() - float(cached.get("ts") or 0.0) < RANK_TTL:
            return {**cached["data"], "cache": {"hit": True,
                                                "age_sec": round(time.time() - cached["ts"], 1)}}
    started = time.perf_counter()
    bulk = paper_store.accounts_bulk(db_path, include_users=True)
    users = bulk.get("users") or paper_auth.list_users(db_path)
    accounts = bulk["accounts"]
    positions = bulk["positions"]

    tickers: list[str] = []
    for rows in positions.values():
        for row in rows:
            if row["ticker"] not in tickers:
                tickers.append(row["ticker"])

    fx: object = None
    prices: dict = {}
    sources: dict = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="paper-rank") as pool:
        fx_future = pool.submit(_timed_call, fx_fn or get_usd_krw)
        price_future = (pool.submit(_timed_call, price_fn or default_prices, tickers)
                        if tickers else None)
        fx, fx_error, _ = fx_future.result()
        if fx_error is not None:
            logger.warning("랭킹 환율 조회 실패: %s: %s", type(fx_error).__name__, str(fx_error)[:160])
        if price_future is not None:
            got, price_error, _ = price_future.result()
            if isinstance(got, tuple) and len(got) == 2:
                prices, sources = got[0] or {}, got[1] or {}
            if price_error is not None:
                logger.warning("랭킹 시세 조회 실패: %s: %s",
                               type(price_error).__name__, str(price_error)[:160])
    if not isinstance(fx, dict) or not _num(fx.get("rate")):
        fx = {"rate": DEFAULT_USD_KRW, "source": "fixed_fallback"}
    fx_rate = float(fx["rate"])

    rows: list[dict] = []
    for user in users:
        uid = int(user["id"])
        per: dict[str, dict] = {}
        total_value = 0.0
        total_initial = 0.0
        positions_count = 0
        for aid, account in (accounts.get(uid) or {}).items():
            mkt = account["market_scope"]
            view = _valuation(positions.get(aid, []), prices, sources, fx_rate)
            summary = _summary(account, account["cash_krw"], view["stock_value_krw"])
            per[mkt] = {**summary, "account_id": aid,
                        "label": account_label(mkt),
                        "positions_count": len(positions.get(aid, []))}
            total_value += summary["total_value_krw"]
            total_initial += summary["initial_cash_krw"]
            positions_count += len(positions.get(aid, []))
        pnl = total_value - total_initial
        rows.append({
            "user_id": uid,
            "nickname": user.get("nickname") or "",
            "is_admin": bool(user.get("is_admin")),
            "total_value_krw": round(total_value, 2),
            "total_pnl_krw": round(pnl, 2),
            "total_return_pct": (round(pnl / total_initial * 100, 2)
                                 if total_initial > 0 else None),
            "positions_count": positions_count,
            "us": per.get("US"),
            "kr": per.get("KR"),
        })
    rows.sort(key=lambda row: (row["total_return_pct"] is None,
                               -(row["total_return_pct"] or 0.0)))
    previous: float | None = None
    rank = 0
    for index, row in enumerate(rows, start=1):
        value = row["total_return_pct"]
        if value is None or value != previous:
            rank = index
            previous = value
        row["rank"] = rank
    body = {"ok": True, "rows": rows, "count": len(rows), "fx_rate": round(fx_rate, 2),
            "fx_source": fx.get("source"), "as_of": paper_store._now(),
            "storage_backend": bulk.get("storage_backend"),
            "took_ms": round((time.perf_counter() - started) * 1000, 1),
            "cache": {"hit": False}}
    if price_fn is None and fx_fn is None:
        _RANK_CACHE[cache_key] = {"ts": time.time(), "data": body}
    return body


def clear_caches() -> None:
    """테스트용 캐시 초기화."""
    reset_fx_cache()
    _RANK_CACHE.clear()
    invalidate_status_cache()
    broker.clear_cache()
    _AUTO_FAILURES.clear()
    if extended_quotes and hasattr(extended_quotes, "clear_cache"):
        try:
            extended_quotes.clear_cache()
        except Exception:
            pass
    try:
        realtime_quotes.clear_cache()
    except Exception:
        pass
