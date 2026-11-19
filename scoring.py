"""정량 스코어링 엔진 — AI 선정 **전** 단계에서 후보 종목을 결정적으로 줄 세운다.

왜 필요한가
-----------
`/api/screen`은 지금까지 15개 종목을 순서대로 Gemini에 던지고 모델이 알아서 고르게 했다.
같은 데이터라도 모델 출력이 매번 달라지고, 조건("저PER", "눌림목")이 수치와 어떻게
연결됐는지 검증할 수도 없다. 이 모듈은 ① `criteria`에서 전략 의도를 읽고
② 이미 받아 둔 지표(이평선·RSI·재무 + MACD·볼린저·거래량·고점대비·구간위치)만으로
0~100 `quant_score`와 사람이 읽을 수 있는 `signals`를 계산한다.

원칙
----
- **네트워크 호출 0회**: `stock_tools.get_stock_metrics()`가 돌려준 dict만 쓴다.
- **결정적**: 같은 입력이면 항상 같은 점수·같은 순서(난수·시각·딕셔너리 순서 의존 없음).
- **결측은 벌하지 않는다**: Yahoo/SEC/Finnhub 어느 경로로도 값이 없는 지표는 0점(중립)으로
  두고 signal도 만들지 않는다. (API 제한 때문에 종목이 부당하게 밀리지 않게)
- **기존 클라이언트 호환**: 점수·signal은 *추가* 필드다. 기존 키의 이름·의미·형식은 그대로.

전략 4종
--------
- `value`     저PER·가치·저평가·배당 → 재무(밸류에이션) 중심
- `oversold`  눌림목·과매도·반등·급락·RSI·볼린저·하락 → 저점 반등 신호 중심
- `momentum`  모멘텀·돌파·골든크로스·상승·추세·성장·매출 → 추세·거래량 중심
- `balanced`  (기본값) 위 항목을 고르게 조금씩
"""
import math
import re

STRATEGIES = ("value", "oversold", "momentum", "balanced")

STRATEGY_LABELS = {
    "value": "가치(밸류에이션)",
    "oversold": "과매도 반등",
    "momentum": "모멘텀(추세 추종)",
    "balanced": "균형",
}

# criteria에서 전략을 읽는 키워드. 영문은 단어 경계를 봐서 "PER"가 "Superior" 같은
# 단어 안에서 걸리지 않게 한다(한글은 부분 문자열로 충분).
_STRATEGY_KEYWORDS = {
    "value": (
        "저per", "가치", "저평가", "배당",
        r"(?<![a-z])per(?![a-z])", r"(?<![a-z])p/e(?![a-z])", r"(?<![a-z])value(?![a-z])",
    ),
    "oversold": (
        "눌림목", "과매도", "반등", "급락", "볼린저",
        r"(?<![a-z])rsi(?![a-z])", "하락",
    ),
    "momentum": (
        "모멘텀", "돌파", "골든크로스", "상승", "추세", "성장", "매출",
        r"(?<![a-z])momentum(?![a-z])", r"(?<![a-z])breakout(?![a-z])",
    ),
}

# 동률일 때의 우선순위: "저PER 가치주 급락 반등"처럼 섞이면 value → oversold → momentum.
_STRATEGY_ORDER = ("value", "oversold", "momentum")

MAX_SIGNALS = 6


# --- 공개 API ---------------------------------------------------------------

def detect_strategy(criteria: str | None) -> str:
    """`criteria` 문자열에서 전략 의도를 고릅니다. 아무 키워드도 없으면 `balanced`.

    키워드 적중 개수가 가장 많은 전략을 쓰고, 동률이면 value > oversold > momentum
    순으로 앞선 것을 고른다(재현 가능).
    """
    text = (criteria or "").strip().lower()
    if not text:
        return "balanced"
    hits = {
        name: sum(1 for pattern in patterns if re.search(pattern, text))
        for name, patterns in _STRATEGY_KEYWORDS.items()
    }
    best = max(hits.values())
    if best == 0:
        return "balanced"
    for name in _STRATEGY_ORDER:
        if hits[name] == best:
            return name
    return "balanced"


def score_stock(stock: dict, strategy: str = "balanced") -> tuple[float, list[str]]:
    """종목 하나의 `(quant_score, signals)`를 계산합니다.

    quant_score: 0~100 (기준 50점에서 전략별 가감, 1자리 반올림)
    signals: 크기가 큰 순서의 사람이 읽을 설명 목록(최대 6개, 예: "저PER(14.8)", "거래량1.9x")
    """
    strategy = strategy if strategy in STRATEGIES else "balanced"
    scorer = _Scorer()
    if not isinstance(stock, dict):
        return 50.0, []
    if strategy == "value":
        _score_value(scorer, stock)
    elif strategy == "oversold":
        _score_oversold(scorer, stock)
    elif strategy == "momentum":
        _score_momentum(scorer, stock)
    else:
        _score_balanced(scorer, stock)
    return scorer.score(), scorer.signals()


def rank_stocks(stocks: list[dict], criteria: str | None = None,
                strategy: str | None = None) -> tuple[list[dict], str]:
    """후보 종목에 quant_score·signals를 붙이고 점수 내림차순으로 정렬해 돌려줍니다.

    반환: (정렬된 새 리스트, 사용한 전략). 입력 dict는 복사본을 만들어 쓰므로
    호출자가 넘긴 리스트/딕셔너리는 바뀌지 않는다. 동점은 입력 순서를 유지한다
    (Python 정렬이 안정 정렬이라 순위가 흔들리지 않는다).
    """
    used = strategy if strategy in STRATEGIES else detect_strategy(criteria)
    annotated: list[dict] = []
    for stock in stocks or []:
        if not isinstance(stock, dict):
            annotated.append(stock)
            continue
        row = dict(stock)
        score, signals = score_stock(row, used)
        row["quant_score"] = score
        row["signals"] = signals
        annotated.append(row)
    # 점수가 없는(에러) 행은 0점 취급해 뒤로 보낸다.
    def _sort_key(row):
        if not isinstance(row, dict):
            return 0.0
        return -_number(row.get("quant_score"), default=0.0)

    annotated.sort(key=_sort_key)
    return annotated, used


# --- 내부: 점수 누적기 -------------------------------------------------------

class _Scorer:
    """기준 50점 + 가감점. signal은 (가중치, 문구)로 모아 크기 순으로 자른다."""

    BASE = 50.0

    def __init__(self):
        self._total = self.BASE
        self._signals: list[tuple[float, str]] = []

    def add(self, weight: float, label: str | None = None) -> None:
        self._total += weight
        if label:
            self._signals.append((float(weight), label))

    def score(self) -> float:
        return round(min(100.0, max(0.0, self._total)), 1)

    def signals(self) -> list[str]:
        # 큰 영향을 준 항목부터. 같은 크기면 먼저 계산된(=핵심 지표) 것이 앞선다.
        ordered = sorted(self._signals, key=lambda item: -abs(item[0]))
        out: list[str] = []
        for _, label in ordered:
            if label not in out:
                out.append(label)
            if len(out) >= MAX_SIGNALS:
                break
        return out


def _number(value, default=None):
    """지표 값을 float으로. None/NaN/inf/불리언은 default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _pct(value: float, digits: int = 0) -> str:
    return f"{value:.{digits}f}"


# --- 전략별 가감점 -----------------------------------------------------------
# 모든 임계값은 stock_tools가 내보내는 단위를 따른다.
#   trailing_pe/forward_pe: 배, revenue_growth/operating_margins: 비율(0.15 = 15%),
#   debt_to_equity: 퍼센트(24.6 = 24.6%), rsi14: 0~100, bb_pct_b: 밴드 내 위치(0~1),
#   volume_ratio: 배수, drawdown_from_high_pct: %(음수), range_position_pct: 0~100

def _score_value(s: _Scorer, stock: dict) -> None:
    """가치 전략: PER이 낮고, 성장·이익률이 받쳐 주고, 부채가 과하지 않은 종목."""
    pe = _number(stock.get("trailing_pe"))
    label = "PER"
    if pe is None or pe <= 0:
        pe = _number(stock.get("forward_pe"))
        label = "예상PER"
    if pe is not None and pe > 0:
        if pe < 10:
            s.add(18, f"저{label}({pe:.1f})")
        elif pe < 15:
            s.add(12, f"저{label}({pe:.1f})")
        elif pe < 20:
            s.add(6, f"{label}적정({pe:.1f})")
        elif pe < 30:
            s.add(0, f"{label}({pe:.1f})")
        else:
            s.add(-12, f"고{label}({pe:.1f})")

    growth = _number(stock.get("revenue_growth"))
    if growth is not None:
        if growth >= 0.20:
            s.add(10, f"매출성장{_pct(growth * 100)}%")
        elif growth >= 0.08:
            s.add(6, f"매출성장{_pct(growth * 100)}%")
        elif growth >= 0:
            s.add(2)
        else:
            s.add(-8, f"매출감소{_pct(growth * 100)}%")

    margin = _number(stock.get("operating_margins"))
    if margin is not None:
        if margin >= 0.25:
            s.add(8, f"영업이익률{_pct(margin * 100)}%")
        elif margin >= 0.15:
            s.add(5, f"영업이익률{_pct(margin * 100)}%")
        elif margin < 0.05:
            s.add(-6, f"저마진{_pct(margin * 100, 1)}%")

    debt = _number(stock.get("debt_to_equity"))
    if debt is not None:
        if debt <= 50:
            s.add(4, f"저부채({_pct(debt)}%)")
        elif debt > 150:
            s.add(-8, f"고부채({_pct(debt)}%)")

    rsi = _number(stock.get("rsi14"))
    if rsi is not None:
        if rsi < 35:
            s.add(3, f"RSI{rsi:.1f} 저점")
        elif rsi > 75:
            s.add(-4, f"RSI{rsi:.1f} 과열")

    drawdown = _number(stock.get("drawdown_from_high_pct"))
    if drawdown is not None and drawdown <= -20:
        s.add(3, f"고점{_pct(drawdown, 1)}%")


def _score_oversold(s: _Scorer, stock: dict) -> None:
    """과매도/눌림목 전략: RSI·볼린저 하단·고점 대비 조정 폭이 큰 종목, 반등 조짐에 가점."""
    rsi = _number(stock.get("rsi14"))
    if rsi is not None:
        if rsi < 25:
            s.add(22, f"RSI{rsi:.1f} 과매도")
        elif rsi < 30:
            s.add(18, f"RSI{rsi:.1f} 과매도")
        elif rsi < 35:
            s.add(12, f"RSI{rsi:.1f} 저RSI")
        elif rsi < 40:
            s.add(6, f"RSI{rsi:.1f} 약세")
        elif rsi < 55:
            s.add(0)
        elif rsi < 70:
            s.add(-6, f"RSI{rsi:.1f} 과열권")
        else:
            s.add(-12, f"RSI{rsi:.1f} 과열")

    pct_b = _number(stock.get("bb_pct_b"))
    if pct_b is not None:
        if pct_b < 0:
            s.add(16, "볼린저하단이탈")
        elif pct_b < 0.15:
            s.add(10, "볼린저하단")
        elif pct_b < 0.35:
            s.add(4, "볼린저하단부근")
        elif pct_b > 1:
            s.add(-10, "볼린저상단초과")

    drawdown = _number(stock.get("drawdown_from_high_pct"))
    if drawdown is not None:
        if drawdown <= -25:
            s.add(10, f"고점{_pct(drawdown, 1)}%")
        elif drawdown <= -15:
            s.add(7, f"고점{_pct(drawdown, 1)}%")
        elif drawdown <= -8:
            s.add(3, f"고점{_pct(drawdown, 1)}%")
        elif drawdown > -3:
            s.add(-5, "고점근접")

    cross = str(stock.get("macd_cross") or "none").lower()
    if cross == "bullish":
        s.add(8, "MACD골든크로스")
    elif cross == "bearish":
        s.add(-4, "MACD데드크로스")

    volume_ratio = _number(stock.get("volume_ratio"))
    if volume_ratio is not None:
        if volume_ratio >= 2.0:
            s.add(6, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio >= 1.3:
            s.add(4, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio < 0.5:
            s.add(-2)

    trend = str(stock.get("trend") or "")
    if "역배열" in trend:
        s.add(-4, "역배열 하락추세")
    elif "정배열" in trend:
        s.add(-3)  # 이미 오른 종목은 '눌림목'이 아니다


def _score_momentum(s: _Scorer, stock: dict) -> None:
    """모멘텀 전략: 정배열·MACD 골든크로스·거래량 급증·신고가권에 가점."""
    cross = str(stock.get("macd_cross") or "none").lower()
    if cross == "bullish":
        s.add(15, "MACD골든크로스")
    elif cross == "bearish":
        s.add(-10, "MACD데드크로스")

    price = _number(stock.get("price"))
    ma20 = _number(stock.get("ma20"))
    ma60 = _number(stock.get("ma60"))
    if price is not None and ma20 is not None and ma60 is not None:
        if price > ma20 > ma60:
            s.add(12, "정배열")
        elif price < ma20 < ma60:
            s.add(-10, "역배열")

    volume_ratio = _number(stock.get("volume_ratio"))
    if volume_ratio is not None:
        if volume_ratio >= 2.5:
            s.add(15, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio >= 1.8:
            s.add(12, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio >= 1.3:
            s.add(7, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio >= 1.0:
            s.add(3)
        elif volume_ratio < 0.6:
            s.add(-5, f"거래량{volume_ratio:.1f}x 부진")

    rsi = _number(stock.get("rsi14"))
    if rsi is not None:
        if 55 <= rsi < 70:
            s.add(8, f"RSI{rsi:.1f} 상승탄력")
        elif 70 <= rsi <= 80:
            s.add(2, f"RSI{rsi:.1f} 과열권")
        elif rsi > 80:
            s.add(-6, f"RSI{rsi:.1f} 과열")
        elif rsi < 40:
            s.add(-8, f"RSI{rsi:.1f} 약세")

    position = _number(stock.get("range_position_pct"))
    if position is not None:
        if position >= 90:
            s.add(10, f"신고가권({_pct(position)}%)")
        elif position >= 75:
            s.add(5, f"고점권({_pct(position)}%)")
        elif position < 40:
            s.add(-5, f"저점권({_pct(position)}%)")

    growth = _number(stock.get("revenue_growth"))
    if growth is not None and growth >= 0.15:
        s.add(6, f"매출성장{_pct(growth * 100)}%")

    drawdown = _number(stock.get("drawdown_from_high_pct"))
    if drawdown is not None and drawdown > -3:
        s.add(3, "고점부근")


def _score_balanced(s: _Scorer, stock: dict) -> None:
    """균형 전략: 밸류에이션·추세·과열/침체를 고르게 조금씩 본다(가장 자주 쓰이는 기본값)."""
    pe = _number(stock.get("trailing_pe"))
    if pe is not None and pe > 0:
        if pe < 15:
            s.add(8, f"저PER({pe:.1f})")
        elif pe < 25:
            s.add(4, f"PER({pe:.1f})")
        elif pe > 45:
            s.add(-6, f"고PER({pe:.1f})")

    growth = _number(stock.get("revenue_growth"))
    if growth is not None:
        if growth >= 0.15:
            s.add(6, f"매출성장{_pct(growth * 100)}%")
        elif growth < 0:
            s.add(-6, f"매출감소{_pct(growth * 100)}%")

    margin = _number(stock.get("operating_margins"))
    if margin is not None and margin >= 0.20:
        s.add(4, f"영업이익률{_pct(margin * 100)}%")

    price = _number(stock.get("price"))
    ma20 = _number(stock.get("ma20"))
    ma60 = _number(stock.get("ma60"))
    if price is not None and ma20 is not None and ma60 is not None:
        if price > ma20 > ma60:
            s.add(6, "정배열")
        elif price < ma20 < ma60:
            s.add(-6, "역배열")

    cross = str(stock.get("macd_cross") or "none").lower()
    if cross == "bullish":
        s.add(6, "MACD골든크로스")
    elif cross == "bearish":
        s.add(-4, "MACD데드크로스")

    rsi = _number(stock.get("rsi14"))
    if rsi is not None:
        if rsi < 30:
            s.add(4, f"RSI{rsi:.1f} 과매도")
        elif 45 <= rsi < 65:
            s.add(4, f"RSI{rsi:.1f} 중립")
        elif rsi > 75:
            s.add(-6, f"RSI{rsi:.1f} 과열")

    pct_b = _number(stock.get("bb_pct_b"))
    if pct_b is not None and pct_b < 0:
        s.add(3, "볼린저하단이탈")

    volume_ratio = _number(stock.get("volume_ratio"))
    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            s.add(4, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio < 0.5:
            s.add(-3)

    drawdown = _number(stock.get("drawdown_from_high_pct"))
    if drawdown is not None:
        if -30 <= drawdown <= -10:
            s.add(2, f"고점{_pct(drawdown, 1)}% 조정")
        elif drawdown > -2:
            s.add(-2)
