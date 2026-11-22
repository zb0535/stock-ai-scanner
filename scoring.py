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

복합 조건(2026-11 추가)
----------------------
전략 하나만 고르면 `"상승추세(정배열) 이면서 PER이 낮은 가치주"`처럼 **전략 + 세부 조건**이
섞인 문장이 기본 `value` 프리셋과 똑같이 채점됐다(키워드 수가 2:2로 비겨 value). 전략은
여전히 '무엇으로 점수를 매기나'를 정하고, 그 위에 `detect_intents`가 읽은 **세부 조건**을
가감점으로 얹는다:
- `uptrend`     상승추세·정배열 → 정배열 종목 가점, 역배열/혼조 감점 (후보 선별에서도 우선)
- `downtrend`   하락·역배열·눌림목·과매도 → 역배열 종목 가점, 정배열 감점
- `golden_cross` 골든크로스 → MACD 골든크로스 가점, 데드크로스 감점
- `volume_surge` 거래량 급증 → 거래량 배수 가점, 부진 감점
- `low_pe`      저PER·PER 낮은 → 프롬프트 해석용(점수는 value 전략이 이미 반영)
- `quality`     영업이익률·부채비율 안정 → 프롬프트 해석용(value 전략이 이미 반영)

동종업계 PER(2026-11 추가)
------------------------
`industry_pe`(업종 기준 PER)가 있으면 절대 PER만 보지 않고 **업종 대비 할인율**을 가감점에
넣는다(`업종대비저PER`). 값이 없으면 기존 절대값 기준만 쓴다.
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

# --- 세부 조건(복합 조건) 키워드 ---------------------------------------------
# 전략('어떤 공식으로 점수를 매기나')과 별개로, 사용자가 문장에 넣은 **필수 조건**을 읽는다.
# 예: "상승추세(정배열) 이면서 ... 가치주" → strategy=value + intents={"uptrend", "low_pe"}
INTENT_LABELS = {
    "uptrend": "상승추세(정배열)",
    "downtrend": "하락/역배열·눌림목",
    "golden_cross": "골든크로스",
    "volume_surge": "거래량 급증",
    "low_pe": "저PER",
    "quality": "재무 안정",
}
INTENT_ORDER = ("uptrend", "downtrend", "golden_cross", "volume_surge", "low_pe", "quality")

_INTENT_KEYWORDS = {
    "uptrend": (
        "상승추세", "상승 추세", "정배열", "우상향", "상승세", "상향추세",
        "20일선 위", "20일선 상회", "이평선 위",
        r"(?<![a-z])uptrend(?![a-z])", r"(?<![a-z])above\s+ma(?![a-z])",
    ),
    "downtrend": (
        "하락추세", "하락 추세", "역배열", "눌림목", "과매도", "급락", "하락세", "조정장",
        r"(?<![a-z])downtrend(?![a-z])",
    ),
    "golden_cross": ("골든크로스", r"golden\s*cross"),
    "volume_surge": ("거래량 급증", "거래량급증", "거래량 증가", "거래량이 늘", "거래량 폭증",
                     r"volume\s*surge"),
    "low_pe": ("저per", "per이 낮", "per 낮", "낮은 per", "per 대비",
               "trailing per", "forward per", "저평가"),
    "quality": ("영업이익률", "부채비율", "이익률", "재무 안정", "재무가 안정", "안정적"),
}

# 상승/하락이 동시에 걸리면(예: "상승추세가 아닌 눌림목") 더 구체적인 쪽만 남긴다.
_OPPOSING_INTENTS = (("uptrend", "downtrend"),)

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


def parse_criteria(criteria: str | None) -> dict:
    """조건 문장 → `{"strategy": 전략, "intents": [세부 조건...]}`.

    전략은 기존 `detect_strategy` 그대로(점수 공식 선택)이고, `intents`가 사용자가 문장에
    적은 필수 조건이다. 점수·후보 선별·프롬프트가 모두 이 한 번의 해석을 공유한다.
    """
    intents = detect_intents(criteria)
    return {"strategy": detect_strategy(criteria), "intents": intents}


def detect_intents(criteria: str | None) -> list[str]:
    """`criteria`에서 세부 조건(복합 조건)을 읽습니다. 없으면 빈 목록.

    키워드가 하나라도 걸린 조건만 `INTENT_ORDER` 순서로 돌려준다(재현 가능). 상승과 하락이
    동시에 걸리면 적중 수가 많은 쪽을 남기고, 같으면 '눌림목·과매도'처럼 더 구체적인
    `downtrend`를 남긴다.
    """
    text = (criteria or "").strip().lower()
    if not text:
        return []
    hits = {name: sum(1 for pattern in patterns if re.search(pattern, text))
            for name, patterns in _INTENT_KEYWORDS.items()}
    found = [name for name in INTENT_ORDER if hits.get(name, 0) > 0]
    for left, right in _OPPOSING_INTENTS:
        if left in found and right in found:
            drop = left if hits[right] >= hits[left] else right
            found = [name for name in found if name != drop]
    return found


def trend_state(stock: dict) -> str | None:
    """이평선 배열 상태 — `"up"`(정배열) / `"down"`(역배열) / `"mixed"`(혼조) / None(판단 불가).

    `trend` 문구를 먼저 보고, 문구가 없으면 현재가·20일선·60일선으로 직접 판정한다.
    값이 모자라면 None — 결측은 벌하지 않는다.
    """
    if not isinstance(stock, dict):
        return None
    trend = str(stock.get("trend") or "")
    if "정배열" in trend:
        return "up"
    if "역배열" in trend:
        return "down"
    price = _number(stock.get("price"))
    ma20 = _number(stock.get("ma20"))
    ma60 = _number(stock.get("ma60"))
    if price is None or ma20 is None or ma60 is None:
        return None
    if price > ma20 > ma60:
        return "up"
    if price < ma20 < ma60:
        return "down"
    return "mixed"


def requires_trend(intents) -> str | None:
    """세부 조건이 추세를 요구하면 그 방향(`"up"`/`"down"`), 아니면 None. 후보 선별용."""
    names = tuple(intents or ())
    if "uptrend" in names:
        return "up"
    if "downtrend" in names:
        return "down"
    return None


def score_stock(stock: dict, strategy: str = "balanced",
                intents=None) -> tuple[float, list[str]]:
    """종목 하나의 `(quant_score, signals)`를 계산합니다.

    quant_score: 0~100 (기준 50점에서 전략별 가감, 1자리 반올림)
    signals: 크기가 큰 순서의 사람이 읽을 설명 목록(최대 6개, 예: "저PER(14.8)", "정배열일치")
    intents: `detect_intents(criteria)`가 읽은 세부 조건(없으면 전략 점수만).
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
    _apply_intents(scorer, stock, intents)
    return scorer.score(), scorer.signals()


def score_prescan(stock: dict, strategy: str = "balanced",
                  intents=None) -> tuple[float, list[str]]:
    """재무 지표가 아직 없는 **프리 스캔**(S&P 500 500종목)용 `(quant_score, signals)`.

    `value`만 다르게 채점한다. 가치 전략의 핵심 근거인 PER·성장률·이익률은 500종목
    전부에 대해 받아 올 수 없기 때문이다(그 호출이 스캔을 수 분으로 늘린다). 대신
    '실적이 무너지지 않은 종목의 적당한 조정'을 기술적으로 대신 본다:
    - 고점 대비 -8~-25% 조정은 매수 기회, -45% 이하 붕괴는 리스크로 감점
    - RSI 30 안팎의 식음에 가점, 65 이상 과열은 진입 위치가 나쁘다고 감점
    - 60일선에서 -10% 이내(추세 유지)에 가점, -20% 이상 이탈은 감점
    다른 전략(oversold/momentum/balanced)은 `score_stock`과 동일하다.
    """
    strategy = strategy if strategy in STRATEGIES else "balanced"
    if not isinstance(stock, dict):
        return 50.0, []
    # 상승추세(정배열) 조건이면 '품질 조정' 프록시 대신 **추세 프록시**로 후보를 고른다.
    # value 프록시는 RSI 조정·고점 대비 눌림·60일선 근접을 가점해 정배열 종목을 오히려
    # 뒤로 보내므로, 이 조건에서는 정면으로 충돌한다(후보가 기본 value 프리셋과 같아지던 원인).
    if requires_trend(intents) == "up":
        scorer = _Scorer()
        _score_uptrend_prescan(scorer, stock)
        return scorer.score(), scorer.signals()
    if strategy != "value":
        return score_stock(stock, strategy, intents)
    scorer = _Scorer()
    _score_value_prescan(scorer, stock)
    _apply_intents(scorer, stock, intents)
    return scorer.score(), scorer.signals()


def rank_stocks(stocks: list[dict], criteria: str | None = None,
                strategy: str | None = None) -> tuple[list[dict], str]:
    """후보 종목에 quant_score·signals를 붙이고 점수 내림차순으로 정렬해 돌려줍니다.

    반환: (정렬된 새 리스트, 사용한 전략). 입력 dict는 복사본을 만들어 쓰므로
    호출자가 넘긴 리스트/딕셔너리는 바뀌지 않는다. 동점은 입력 순서를 유지한다
    (Python 정렬이 안정 정렬이라 순위가 흔들리지 않는다).

    `criteria`에 세부 조건(`상승추세`/`정배열` 등)이 있으면 그 조건의 가감점까지 반영한다.
    `all_stocks`는 후보를 빼지 않고 전부 점수와 함께 돌려준다(화면 표가 비지 않게).
    """
    parsed = parse_criteria(criteria)
    used = strategy if strategy in STRATEGIES else parsed["strategy"]
    intents = parsed["intents"]
    annotated: list[dict] = []
    for stock in stocks or []:
        if not isinstance(stock, dict):
            annotated.append(stock)
            continue
        row = dict(stock)
        score, signals = score_stock(row, used, intents=intents)
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
#   volume_ratio: 배수, drawdown_from_high_pct: %(음수), range_position_pct: 0~100,
#   industry_pe: 배(업종 기준 PER — 없으면 동종업계 비교를 건너뛴다)

def _industry_pe_gap(stock: dict, pe: float | None) -> tuple[float, float] | None:
    """(내 PER, 업종 기준 PER) — 둘 다 양수일 때만. 동종업계 비교가 가능한지 판단하는 곳."""
    industry_pe = _number(stock.get("industry_pe"))
    if pe is None or pe <= 0 or industry_pe is None or industry_pe <= 0:
        return None
    return pe, industry_pe


def _score_industry_pe(s: _Scorer, stock: dict, pe: float | None, scale: float = 1.0) -> None:
    """동종업계 PER 대비 할인/할증 가감점 — 업종 평균보다 15%·30% 낮으면 가점, 높으면 감점.

    `industry_pe`가 없으면(업종 정보 미수신) 아무것도 더하지 않는다 — 호출부의 절대 PER
    기준만 남아 기존 동작과 같다.
    """
    pair = _industry_pe_gap(stock, pe)
    if pair is None:
        return
    my_pe, industry_pe = pair
    gap = (my_pe - industry_pe) / industry_pe          # 음수 = 업종보다 싸다
    tag = f"{my_pe:.1f} vs 업종{industry_pe:.1f}"
    if gap <= -0.30:
        s.add(10 * scale, f"업종대비저PER({tag})")
    elif gap <= -0.15:
        s.add(6 * scale, f"업종대비저PER({tag})")
    elif gap < 0.15:
        s.add(2 * scale, f"업종PER수준({tag})")
    elif gap < 0.40:
        s.add(-3 * scale, f"업종대비고PER({tag})")
    else:
        s.add(-7 * scale, f"업종대비고PER({tag})")


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
        # 절대 PER만으로는 '비싼 업종이라 싸 보이는 종목'을 놓친다 — 업종 기준 PER과 비교한다.
        _score_industry_pe(s, stock, pe)

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


def _score_value_prescan(s: _Scorer, stock: dict) -> None:
    """가치 전략의 프리 스캔 점수: '품질 있는 조정'을 기술 지표로 대신 본다."""
    drawdown = _number(stock.get("drawdown_from_high_pct"))
    if drawdown is not None:
        if drawdown <= -45:
            s.add(-12, f"고점{_pct(drawdown, 0)}% 붕괴위험")
        elif drawdown <= -25:
            s.add(6, f"고점{_pct(drawdown, 0)}% 조정")
        elif drawdown <= -8:
            s.add(14, f"고점{_pct(drawdown, 0)}% 눌림")
        elif drawdown <= -3:
            s.add(4, f"고점{_pct(drawdown, 0)}%")
        else:
            s.add(-6, "고점근접(조정없음)")

    rsi = _number(stock.get("rsi14"))
    if rsi is not None:
        if rsi < 20:
            s.add(-4, f"RSI{rsi:.1f} 급락")
        elif rsi < 30:
            s.add(8, f"RSI{rsi:.1f} 저점")
        elif rsi < 45:
            s.add(12, f"RSI{rsi:.1f} 조정")
        elif rsi < 55:
            s.add(6, f"RSI{rsi:.1f} 중립")
        elif rsi < 65:
            s.add(2, f"RSI{rsi:.1f}")
        else:
            s.add(-6, f"RSI{rsi:.1f} 과열")

    position = _number(stock.get("range_position_pct"))
    if position is not None:
        if position < 10:
            s.add(-4, f"저점권({_pct(position)}%)")
        elif position < 50:
            s.add(8, f"구간하단({_pct(position)}%)")
        elif position < 70:
            s.add(2)
        elif position >= 85:
            s.add(-6, f"고점권({_pct(position)}%)")

    price = _number(stock.get("price"))
    ma60 = _number(stock.get("ma60"))
    if price and ma60 and ma60 > 0:
        gap = (price - ma60) / ma60 * 100.0
        if gap >= 20:
            s.add(-3, f"60일선+{_pct(gap)}%")
        elif gap >= 0:
            s.add(4, f"60일선+{_pct(gap)}%")
        elif gap >= -10:
            s.add(6, f"60일선{_pct(gap)}% 근접")
        elif gap >= -20:
            s.add(0, f"60일선{_pct(gap)}%")
        else:
            s.add(-8, f"60일선{_pct(gap)}% 이탈")

    cross = str(stock.get("macd_cross") or "none").lower()
    if cross == "bullish":
        s.add(5, "MACD골든크로스")
    elif cross == "bearish":
        s.add(-3, "MACD데드크로스")

    volume_ratio = _number(stock.get("volume_ratio"))
    if volume_ratio is not None:
        if 1.0 <= volume_ratio < 2.0:
            s.add(3, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio < 0.6:
            s.add(-3, f"거래량{volume_ratio:.1f}x 부진")


def _score_uptrend_prescan(s: _Scorer, stock: dict) -> None:
    """`상승추세(정배열)` 조건의 프리 스캔 점수 — 재무가 없는 단계에서는 추세의 '질'로 고른다.

    기본 `value` 프록시(RSI 조정·고점 눌림·60일선 근접 가점)는 정배열 종목을 구조적으로
    뒤로 보내, 조건에 `상승추세`가 붙어도 후보가 기본 가치주 프리셋과 같아졌다. 이 조건에서는
    정배열을 먼저 보고, 과열되지 않은 상승(추세 유지 + 적당한 탄력)을 가점한다.
    """
    state = trend_state(stock)
    if state == "up":
        s.add(20, "정배열일치")
    elif state == "down":
        s.add(-22, "역배열(조건밖)")
    elif state == "mixed":
        # 20일선 위에 있거나 20일선이 60일선 위인 '추세 유지 중 조정'은 작게 인정한다.
        price = _number(stock.get("price"))
        ma20 = _number(stock.get("ma20"))
        ma60 = _number(stock.get("ma60"))
        holding = (price is not None and ma20 is not None and price >= ma20) or \
                  (ma20 is not None and ma60 is not None and ma20 >= ma60)
        if holding:
            s.add(2, "추세유지(혼조)")
        else:
            s.add(-6, "정배열미충족")

    price = _number(stock.get("price"))
    ma60 = _number(stock.get("ma60"))
    if price is not None and ma60 and ma60 > 0:
        gap = (price - ma60) / ma60 * 100.0
        if gap >= 0:
            s.add(6, f"60일선+{_pct(gap, 1)}%")
        elif gap >= -10:
            s.add(2, f"60일선{_pct(gap, 1)}% 근접")
        elif gap < -20:
            s.add(-8, f"60일선{_pct(gap, 1)}% 이탈")

    rsi = _number(stock.get("rsi14"))
    if rsi is not None:
        if 50 <= rsi < 70:
            s.add(8, f"RSI{rsi:.1f} 상승탄력")
        elif 70 <= rsi < 80:
            s.add(2, f"RSI{rsi:.1f} 과열권")
        elif rsi >= 80:
            s.add(-8, f"RSI{rsi:.1f} 과열")
        elif rsi < 40:
            s.add(-8, f"RSI{rsi:.1f} 약세")

    cross = str(stock.get("macd_cross") or "none").lower()
    if cross == "bullish":
        s.add(8, "MACD골든크로스")
    elif cross == "bearish":
        s.add(-6, "MACD데드크로스")

    volume_ratio = _number(stock.get("volume_ratio"))
    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            s.add(6, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio >= 1.1:
            s.add(3, f"거래량{volume_ratio:.1f}x")
        elif volume_ratio < 0.6:
            s.add(-3, f"거래량{volume_ratio:.1f}x 부진")

    position = _number(stock.get("range_position_pct"))
    if position is not None:
        if position >= 75:
            s.add(6, f"고점권({_pct(position)}%)")
        elif position < 30:
            s.add(-4, f"저점권({_pct(position)}%)")

    drawdown = _number(stock.get("drawdown_from_high_pct"))
    if drawdown is not None:
        if drawdown <= -30:
            s.add(-8, f"고점{_pct(drawdown, 0)}% 붕괴")
        elif drawdown >= -15:
            s.add(4, f"고점{_pct(drawdown, 0)}% (추세유지)")


def _apply_intents(s: _Scorer, stock: dict, intents) -> None:
    """세부 조건(복합 조건) 가감점 — 전략 점수 위에 얹는다.

    `상승추세`/`정배열`은 점수 폭을 크게 잡는다(가점 14 / 감점 24). 조건이 붙었을 때 순위가
    기본 가치주 프리셋과 같아지지 않게 하는 것이 목적이므로, 기본 공식의 잔차보다 커야 한다.
    데이터가 모자라 판단 불가(`trend_state`가 None)면 아무것도 더하지 않는다.
    """
    names = tuple(intents or ())
    if not names:
        return
    direction = requires_trend(names)
    if direction:
        state = trend_state(stock)
        if state is not None:
            if direction == "up":
                if state == "up":
                    s.add(14, "정배열일치")
                elif state == "down":
                    s.add(-24, "역배열(조건밖)")
                else:
                    s.add(-10, "정배열미충족")
            else:
                if state == "down":
                    s.add(12, "역배열일치")
                elif state == "up":
                    s.add(-14, "정배열(조건밖)")

    if "golden_cross" in names:
        cross = str(stock.get("macd_cross") or "none").lower()
        if cross == "bullish":
            s.add(10, "골든크로스일치")
        elif cross == "bearish":
            s.add(-8, "데드크로스(조건밖)")

    if "volume_surge" in names:
        volume_ratio = _number(stock.get("volume_ratio"))
        if volume_ratio is not None:
            if volume_ratio >= 1.5:
                s.add(8, f"거래량급증일치({volume_ratio:.1f}x)")
            elif volume_ratio < 0.8:
                s.add(-4, f"거래량부진(조건밖, {volume_ratio:.1f}x)")


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
        # 균형 전략에서는 업종 대비 평가를 작게만 반영한다(전략별 우열을 흔들지 않게).
        _score_industry_pe(s, stock, pe, scale=0.4)

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
