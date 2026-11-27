"""브로커 추상화 — AI 모의투자와 실계좌(한투·토스·미래에셋)를 **같은 주문 경로**로 묶는다.

구조
----
전략·후보 선정·현금 한도 같은 **판단**은 `paper_trading`에 그대로 두고, **주문을 실제로
체결하는 일**만 `BrokerAdapter` 뒤로 뺐다. 지금 실제로 주문을 처리하는 어댑터는
`PaperBroker`(기존 모의투자 원장) 하나이고, 증권사 어댑터는 키만 넣으면 붙일 수 있도록
자리(`KisBroker`·`TossBroker`·`MiraeBroker`)만 잡아 두었다.

    paper_trading (전략·후보·현금 한도 판단)
        └─ get_adapter(market) ── GuardedBroker
                                    ├─ OrderGuard  (킬 스위치·1회/1일 한도·중복 주문·드라이런)
                                    └─ PaperBroker / KisBroker / TossBroker / MiraeBroker
                                           └─ 체결 엔진(`paper_trading._fill_buy`/`_fill_sell`)
                                              → paper_store (포지션·현금·체결 기록)

무엇이 바뀌나
-------------
- `BROKER_MODE`(기본 `paper`)가 어느 어댑터를 쓸지 정한다. 기본값에서는 지금까지와
  **똑같이** 모의투자 계좌로만 매매한다(회계·수수료·환차 계산은 `paper_trading`이 그대로 한다).
- 실계좌 어댑터는 `REAL_TRADING_ENABLED=1` **그리고** `REAL_DRY_RUN=0` **그리고** 증권사 키가
  모두 있을 때만 주문이 나간다. 그 전에는 `OrderGuard`가 모든 실주문을 막는다.
- 실주문 안전장치(기본값은 모두 **막는 쪽**):
  - 킬 스위치 `REAL_TRADING_ENABLED=false` → 실주문 전부 차단
  - 1회 주문 상한 `REAL_ORDER_MAX_KRW`(기본 10만원)
  - 1일 누적 상한 `REAL_DAILY_MAX_KRW`(기본 100만원)
  - 중복 주문 차단 — 같은 `(run_id, market, side, ticker)`는 한 번만
  - 드라이런 `REAL_DRY_RUN=true`(기본) — 주문을 보내지 않고 "보냈다면 어떻게 됐을지"만 돌려준다
- 가드 상태(1일 누적액·중복 키)는 `BROKER_GUARD_STATE`(기본: SQLite 파일 옆 `broker_guard.json`)에
  남겨 서버가 재기동돼도 1일 한도가 초기화되지 않는다. `BROKER_GUARD_STATE=`(빈 값)이면 메모리만 쓴다.

실계좌를 붙이는 방법은 `docs/BROKER_INTEGRATION.md` 참고.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace

import paper_store

logger = logging.getLogger("broker")

# --- 모드·상태 코드 -----------------------------------------------------------

SUPPORTED_MODES = ("paper", "kis", "toss", "mirae")
DEFAULT_MODE = "paper"
MODE_LABELS = {
    "paper": "모의투자(가상 계좌)",
    "kis": "한국투자증권",
    "toss": "토스증권",
    "mirae": "미래에셋증권",
}

STATUS_APPROVED = "approved"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"
STATUS_DRY_RUN = "dry_run"
STATUS_NOT_CONFIGURED = "broker_not_configured"

REASON_INVALID = "invalid_order"
REASON_DUPLICATE = "duplicate_order"
REASON_KILL_SWITCH = "real_trading_disabled"
REASON_ORDER_LIMIT = "order_limit_exceeded"
REASON_DAILY_LIMIT = "daily_limit_exceeded"
REASON_NOT_CONFIGURED = "broker_not_configured"

REASON_LABELS = {
    REASON_INVALID: "주문 내용이 올바르지 않습니다",
    REASON_DUPLICATE: "같은 실행에서 같은 주문이 이미 처리됐습니다(중복 주문)",
    REASON_KILL_SWITCH: "실주문 킬 스위치(REAL_TRADING_ENABLED)가 꺼져 있습니다",
    REASON_ORDER_LIMIT: "1회 주문 상한(REAL_ORDER_MAX_KRW)을 넘었습니다",
    REASON_DAILY_LIMIT: "1일 누적 주문 상한(REAL_DAILY_MAX_KRW)을 넘었습니다",
    REASON_NOT_CONFIGURED: "증권사 API 키/연동 코드가 없습니다",
}

DEFAULT_ORDER_MAX_KRW = 100_000.0       # 1회 실주문 상한(원)
DEFAULT_DAILY_MAX_KRW = 1_000_000.0     # 1일 누적 실주문 상한(원)


# --- 환경변수 -----------------------------------------------------------------


def _clean(value) -> str:
    return str(value or "").strip()


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def env_flag(name: str, default: bool = False) -> bool:
    """`1/true/yes/on`만 참. 빈 값이면 `default`. 읽을 때마다 새로 본다(런타임에 끄면 바로 적용)."""
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off", "none")


def _env_money(name: str, default: float) -> float:
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    value = _num(raw.replace(",", "").replace("_", ""))
    if value is None or value <= 0:
        logger.warning("%s 값이 올바르지 않아 기본값 %.0f원을 씁니다: %r", name, default, raw)
        return default
    return value


def requested_mode() -> str:
    """환경변수에 적힌 그대로의 모드(진단용)."""
    return _clean(os.environ.get("BROKER_MODE")).lower() or DEFAULT_MODE


_WARNED_MODES: set[str] = set()


def broker_mode() -> str:
    """이번 실행에 쓸 어댑터 이름. 모르는 값이면 안전하게 `paper`로 내려간다."""
    mode = requested_mode()
    if mode in ADAPTERS or mode in SUPPORTED_MODES:
        return mode
    if mode not in _WARNED_MODES:
        _WARNED_MODES.add(mode)
        logger.warning("BROKER_MODE=%r 는 지원하지 않는 값입니다 — paper로 실행합니다.", mode)
    return DEFAULT_MODE


def real_trading_enabled() -> bool:
    """실주문 킬 스위치(기본 꺼짐)."""
    return env_flag("REAL_TRADING_ENABLED", False)


def dry_run_enabled() -> bool:
    """실주문 드라이런(기본 켜짐) — 켜져 있으면 실제 주문을 보내지 않는다."""
    return env_flag("REAL_DRY_RUN", True)


def order_max_krw() -> float:
    return _env_money("REAL_ORDER_MAX_KRW", DEFAULT_ORDER_MAX_KRW)


def daily_max_krw() -> float:
    return _env_money("REAL_DAILY_MAX_KRW", DEFAULT_DAILY_MAX_KRW)


# --- 주문·응답 모델 -----------------------------------------------------------


@dataclass(frozen=True)
class OrderRequest:
    """어댑터에 넘기는 주문 한 건 — 증권사 Open API의 주문 요청과 같은 모양.

    - `side`: `"BUY"` | `"SELL"`
    - `shares`: 주문 수량. 매도는 필수. 매수에서 0이면 어댑터가 `amount_krw`로 수량을 정한다
      (모의투자는 예산에 수수료를 넣고 살 수 있는 만큼 계산한다).
    - `price`: **현지 통화** 가격(미국은 USD, 한국은 KRW).
    - `amount_krw`: 매수 예산(원화, 수수료 포함) 또는 매도 대금 참고값.
    - `fx_rate`: 미국 종목의 USD/KRW. 한국 종목은 1.0.
    - `extra`: 어댑터별 부가 정보(모의투자: `position`·`fraction`·`source`).
    """

    side: str
    ticker: str
    market: str
    shares: float = 0.0
    price: float = 0.0
    currency: str = ""
    amount_krw: float = 0.0
    fx_rate: float = 1.0
    reason: str = ""
    run_id: str = ""
    name: str = ""
    account_id: int = paper_store.ACCOUNT_US
    db_path: str | None = None
    dry_run: bool | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 대소문자·공백 때문에 같은 주문이 다른 주문으로 보이지 않게 정규화한다.
        object.__setattr__(self, "side", _clean(self.side).upper())
        object.__setattr__(self, "ticker", _clean(self.ticker).upper())
        object.__setattr__(self, "market", _clean(self.market).upper())
        object.__setattr__(self, "currency", _clean(self.currency).upper())

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"

    @property
    def is_sell(self) -> bool:
        return self.side == "SELL"

    def notional_krw(self) -> float:
        """주문 금액(원화). 매수는 예산, 매도는 수량×가격×환율."""
        if self.is_buy and self.amount_krw > 0:
            return round(float(self.amount_krw), 2)
        rate = 1.0 if self.currency == "KRW" else (float(self.fx_rate) or 1.0)
        return round(abs(float(self.shares)) * abs(float(self.price)) * rate, 2)

    def order_key(self) -> str:
        """중복 주문 판정 키 — 같은 `run_id`의 같은 (시장·방향·종목)은 한 번만.

        `run_id`가 비어 있으면 날짜를 쓴다(그날 그 종목·방향은 한 번만).
        """
        scope = _clean(self.run_id) or f"day:{paper_store.kst_today_str()}"
        return f"{scope}|{self.market}:{self.side}:{self.ticker}"

    def client_order_id(self) -> str:
        """증권사 API에 넘길 멱등 키(같은 주문을 두 번 보내지 않게). 어댑터가 그대로 쓴다."""
        raw = self.order_key()
        return "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw)[:40]

    def as_dict(self) -> dict:
        return {
            "side": self.side, "ticker": self.ticker, "market": self.market,
            "shares": round(float(self.shares), 6), "price": round(float(self.price), 4),
            "currency": self.currency, "amount_krw": round(float(self.amount_krw), 2),
            "fx_rate": round(float(self.fx_rate), 4), "run_id": self.run_id,
            "account_id": self.account_id, "reason": self.reason,
            "client_order_id": self.client_order_id(),
        }


@dataclass
class OrderResult:
    """주문 결과 — `status` 하나로 체결/차단/드라이런/미연결을 구분한다."""

    ok: bool
    status: str
    adapter: str
    live: bool = False
    trade: dict | None = None          # 모의투자 체결 원장(실계좌는 None)
    order_id: str | None = None
    reason: str = ""
    guard: dict | None = None
    raw: dict | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "status": self.status, "adapter": self.adapter, "live": self.live,
            "order_id": self.order_id, "reason": self.reason, "guard": self.guard,
            "trade": self.trade,
        }


@dataclass
class BrokerAccountInfo:
    """계좌 잔고 — 모의투자는 원가 기준, 실계좌는 증권사 응답 기준."""

    adapter: str
    live: bool = False
    ok: bool = True
    status: str = "ok"
    market: str = ""
    account_id: int = paper_store.ACCOUNT_US
    cash_krw: float = 0.0
    initial_cash_krw: float | None = None
    positions: list[dict] = field(default_factory=list)
    message: str = ""
    as_of: str | None = None

    @property
    def positions_count(self) -> int:
        return len(self.positions)

    def as_dict(self) -> dict:
        return {
            "adapter": self.adapter, "live": self.live, "ok": self.ok, "status": self.status,
            "market": self.market, "account_id": self.account_id,
            "cash_krw": round(float(self.cash_krw), 2),
            "initial_cash_krw": (None if self.initial_cash_krw is None
                                 else round(float(self.initial_cash_krw), 2)),
            "positions": self.positions, "positions_count": self.positions_count,
            "message": self.message, "as_of": self.as_of,
        }


@dataclass
class BrokerHealth:
    """어댑터 연결 상태 — 키가 없으면 어떤 환경변수가 비었는지까지 알려준다."""

    adapter: str
    label: str = ""
    live: bool = False
    configured: bool = False
    implemented: bool = True
    status: str = "ok"
    missing_env: list[str] = field(default_factory=list)
    real_trading_enabled: bool = False
    dry_run: bool = True
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "adapter": self.adapter, "label": self.label, "live": self.live,
            "configured": self.configured, "implemented": self.implemented,
            "status": self.status, "missing_env": self.missing_env,
            "real_trading_enabled": self.real_trading_enabled, "dry_run": self.dry_run,
            "message": self.message,
        }


# --- 어댑터 인터페이스 --------------------------------------------------------


class BrokerAdapter(ABC):
    """증권사 하나를 표현하는 최소 인터페이스.

    새 증권사를 붙이는 일은 이 클래스를 상속해 `place_buy`·`place_sell`·`get_balance`를
    채우고 아래 `ADAPTERS`에 등록하는 것뿐이다(`docs/BROKER_INTEGRATION.md`).
    """

    name: str = "base"
    label: str = ""
    is_live: bool = False
    implemented: bool = True
    api_key_envs: tuple[str, ...] = ()

    def missing_env(self) -> list[str]:
        """필수 환경변수 중 비어 있는 것들."""
        return [key for key in self.api_key_envs if not _clean(os.environ.get(key))]

    @property
    def configured(self) -> bool:
        return not self.missing_env()

    def health(self) -> BrokerHealth:
        missing = self.missing_env()
        status = "ok" if self.configured and self.implemented else STATUS_NOT_CONFIGURED
        return BrokerHealth(
            adapter=self.name, label=self.label or MODE_LABELS.get(self.name, self.name),
            live=self.is_live, configured=self.configured, implemented=self.implemented,
            status=status, missing_env=missing,
            real_trading_enabled=real_trading_enabled(), dry_run=dry_run_enabled(),
            message=("" if status == "ok" else self._not_configured_message(missing)),
        )

    def _not_configured_message(self, missing: list[str] | None = None) -> str:
        missing = self.missing_env() if missing is None else missing
        if missing:
            return "증권사 API 키가 없습니다 — 환경변수 " + ", ".join(missing) + " 를 설정하세요."
        return "증권사 API 연동 코드가 아직 없습니다 — docs/BROKER_INTEGRATION.md 참고."

    @abstractmethod
    def place_buy(self, order: OrderRequest) -> OrderResult:
        """매수 주문 1건. 실계좌에서는 **실제로 돈이 나가는 요청**이다."""

    @abstractmethod
    def place_sell(self, order: OrderRequest) -> OrderResult:
        """매도 주문 1건. 실계좌에서는 **실제로 주식이 나가는 요청**이다."""

    @abstractmethod
    def get_balance(self, db_path: str | None = None,
                    account_id: int | None = None) -> BrokerAccountInfo:
        """계좌 현금·보유. 실계좌는 증권사 잔고 API, 모의투자는 원장의 원가 기준."""


def _paper_engine():
    """`paper_trading` 지연 임포트 — paper_trading이 이 모듈을 임포트하므로 순환을 피한다."""
    import paper_trading  # noqa: PLC0415 (순환 임포트 회피용 지연 임포트)
    return paper_trading


class PaperBroker(BrokerAdapter):
    """모의투자 어댑터 — 기존 체결 엔진(`paper_trading`)을 어댑터 뒤에 그대로 세운다.

    체결 회계(수수료·증권거래세·환차·수량 단위·현금 방어선)는 `paper_trading._fill_buy`/
    `_fill_sell`에 있고 이 클래스는 **연결만** 한다 — 그래서 모의투자 결과는 예전과 100% 같다.
    """

    name = "paper"
    label = MODE_LABELS["paper"]
    is_live = False
    api_key_envs = ()

    def __init__(self, market=None, db_path: str | None = None, account_id: int | None = None):
        engine = _paper_engine()
        scope = engine.account_scope(market)
        self.market = scope
        self.db_path = db_path
        self.account_id = int(account_id) if account_id is not None else engine.account_id_of(scope)

    def _path(self, order: OrderRequest) -> str | None:
        return order.db_path if order.db_path is not None else self.db_path

    def _account(self, order: OrderRequest) -> int:
        return int(order.account_id or self.account_id)

    def place_buy(self, order: OrderRequest) -> OrderResult:
        engine = _paper_engine()
        market = order.market or self.market
        currency = order.currency or ("KRW" if market == "KR" else "USD")
        trade = engine._fill_buy(
            order.ticker, market, order.name, currency, float(order.price),
            float(order.amount_krw), float(order.fx_rate), order.reason, order.run_id,
            _clean(order.extra.get("source")) or "broker",
            self._path(order), self._account(order),
        )
        if not trade:
            return OrderResult(ok=False, status=STATUS_REJECTED, adapter=self.name, live=False,
                               reason="모의투자 체결 조건을 만족하지 못했습니다(현금·최소 주문·수량).")
        return OrderResult(ok=True, status=STATUS_FILLED, adapter=self.name, live=False,
                           trade=trade, order_id=_paper_order_id(trade),
                           reason="모의투자 체결")

    def place_sell(self, order: OrderRequest) -> OrderResult:
        engine = _paper_engine()
        position = dict(order.extra.get("position") or {})
        if not position:
            return OrderResult(ok=False, status=STATUS_REJECTED, adapter=self.name, live=False,
                               reason="보유 포지션 정보가 없어 모의투자 매도를 처리하지 못했습니다.")
        trade = engine._fill_sell(
            position, float(order.shares), float(order.price), float(order.fx_rate),
            order.reason, order.run_id, _clean(order.extra.get("source")) or "broker",
            self._path(order), self._account(order),
        )
        return OrderResult(ok=True, status=STATUS_FILLED, adapter=self.name, live=False,
                           trade=trade, order_id=_paper_order_id(trade),
                           reason="모의투자 체결")

    def get_balance(self, db_path: str | None = None,
                    account_id: int | None = None) -> BrokerAccountInfo:
        path = self.db_path if db_path is None else db_path
        aid = self.account_id if account_id is None else int(account_id)
        account = paper_store.get_account(aid, path)
        positions = paper_store.list_positions(path, aid)
        rows = [{
            "ticker": row["ticker"],
            "market": row.get("market") or paper_store.account_market(aid),
            "shares": round(float(row["shares"]), 6),
            # 현재가 조회 없이 원가로만 평가한다(잔고 API는 시세를 부르지 않는다).
            "cost_krw": round(float(row.get("cost_krw") or 0.0), 2),
            "avg_price": round(float(row.get("avg_price") or 0.0), 4),
            "currency": row.get("currency") or "KRW",
        } for row in positions]
        return BrokerAccountInfo(
            adapter=self.name, live=False, ok=True, status="ok",
            market=paper_store.account_market(aid), account_id=aid,
            cash_krw=float(account["cash_krw"]),
            initial_cash_krw=float(account["initial_cash_krw"]),
            positions=rows, message="모의투자 원장(평가는 원가 기준)",
            as_of=paper_store._now(),
        )


def _paper_order_id(trade: dict | None) -> str | None:
    if not trade:
        return None
    row_id = trade.get("id")
    if row_id is None:
        return None
    return f"paper-{row_id}"


class _LiveStubBroker(BrokerAdapter):
    """실계좌 어댑터의 자리 — 키와 연동 코드가 없으므로 어떤 주문도 내보내지 않는다."""

    is_live = True
    implemented = False

    def _not_configured(self, order: OrderRequest | None = None) -> OrderResult:
        message = self._not_configured_message()
        logger.info("%s 주문 보류(%s): %s", self.name, (order.ticker if order else "-"), message)
        return OrderResult(ok=False, status=STATUS_NOT_CONFIGURED, adapter=self.name, live=True,
                           reason=message)

    def place_buy(self, order: OrderRequest) -> OrderResult:
        return self._not_configured(order)

    def place_sell(self, order: OrderRequest) -> OrderResult:
        return self._not_configured(order)

    def get_balance(self, db_path: str | None = None,
                    account_id: int | None = None) -> BrokerAccountInfo:
        return BrokerAccountInfo(adapter=self.name, live=True, ok=False,
                                 status=STATUS_NOT_CONFIGURED,
                                 message=self._not_configured_message())

    def health(self) -> BrokerHealth:
        info = super().health()
        info.message = self._not_configured_message()
        return info


class KisBroker(_LiveStubBroker):
    """한국투자증권 Open API 자리 — 키 발급 후 `place_*`를 채운다."""

    name = "kis"
    label = MODE_LABELS["kis"]
    api_key_envs = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO")


class TossBroker(_LiveStubBroker):
    """토스증권 API 자리 — 키 발급 후 `place_*`를 채운다."""

    name = "toss"
    label = MODE_LABELS["toss"]
    api_key_envs = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO")


class MiraeBroker(_LiveStubBroker):
    """미래에셋증권 API 자리 — 키 발급 후 `place_*`를 채운다."""

    name = "mirae"
    label = MODE_LABELS["mirae"]
    api_key_envs = ("MIRAE_API_KEY", "MIRAE_ACCOUNT_NO")


ADAPTERS: dict[str, type[BrokerAdapter]] = {
    "paper": PaperBroker,
    "kis": KisBroker,
    "toss": TossBroker,
    "mirae": MiraeBroker,
}


# --- 실주문 안전장치 ----------------------------------------------------------


@dataclass
class GuardDecision:
    """주문 전 검사 결과. `allowed`가 False면 절대 보내지 않는다."""

    allowed: bool
    status: str
    reason: str = ""            # 차단 사유 코드(허용이면 빈 값)
    blocked_by: str = ""        # 드라이런에서 "실주문이었다면" 막혔을 사유
    dry_run: bool = False
    live: bool = False
    notional_krw: float = 0.0
    order_max_krw: float = 0.0
    daily_max_krw: float = 0.0
    daily_used_krw: float = 0.0

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed, "status": self.status, "reason": self.reason,
            "reason_label": REASON_LABELS.get(self.reason or self.blocked_by, ""),
            "blocked_by": self.blocked_by, "dry_run": self.dry_run, "live": self.live,
            "notional_krw": round(float(self.notional_krw), 2),
            "order_max_krw": round(float(self.order_max_krw), 2),
            "daily_max_krw": round(float(self.daily_max_krw), 2),
            "daily_used_krw": round(float(self.daily_used_krw), 2),
        }


class OrderLedger:
    """중복 주문 키와 1일 실주문 누적액을 기억한다.

    `path`를 주면 JSON 파일로 남겨 **재기동 뒤에도** 1일 한도와 중복 키가 유지된다
    (실계좌에서 서버가 재시작돼도 하루 한도를 새로 채울 수 없게). 파일을 못 써도
    매매를 막지 않는다 — 경고만 남기고 메모리로 계속 간다.
    """

    KEY_TTL_SEC = 2 * 24 * 3600   # 중복 키는 이틀만 기억한다(파일이 무한히 커지지 않게)

    def __init__(self, path: str | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._day = paper_store.kst_today_str()
        self._daily_krw = 0.0
        self._keys: dict[str, float] = {}
        self._load()

    # --- 읽기 ---
    def daily_used(self) -> float:
        with self._lock:
            self._roll()
            return round(self._daily_krw, 2)

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            self._roll()
            return key in self._keys

    def tracked(self) -> int:
        with self._lock:
            self._roll()
            return len(self._keys)

    # --- 쓰기 ---
    def add(self, key: str, notional_krw: float = 0.0) -> None:
        with self._lock:
            self._roll()
            self._keys[key] = time.time()
            if notional_krw > 0:
                self._daily_krw = round(self._daily_krw + float(notional_krw), 2)
            self._save()

    # --- 내부 ---
    def _roll(self) -> None:
        """날이 바뀌면 1일 누적을 0으로, 오래된 키는 버린다(락 안에서만 부른다)."""
        today = paper_store.kst_today_str()
        if today != self._day:
            self._day = today
            self._daily_krw = 0.0
        cutoff = time.time() - self.KEY_TTL_SEC
        for key in [k for k, ts in self._keys.items() if ts < cutoff]:
            self._keys.pop(key, None)

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except Exception as exc:  # 깨진 파일·권한 — 메모리로 계속
            logger.warning("브로커 가드 상태 읽기 실패(%s): %s", type(exc).__name__, str(exc)[:160])
            return
        if not isinstance(data, dict):
            return
        keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
        with self._lock:
            self._keys = {str(k): float(v) for k, v in keys.items() if _num(v) is not None}
            self._daily_krw = _num(data.get("daily_krw")) or 0.0
            self._day = _clean(data.get("day")) or self._day
            self._roll()

    def _save(self) -> None:
        if not self.path:
            return
        payload = {"day": self._day, "daily_krw": round(self._daily_krw, 2), "keys": self._keys}
        tmp_path = None
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".broker-guard-", dir=directory, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_path, self.path)
            tmp_path = None
        except Exception as exc:
            logger.warning("브로커 가드 상태 저장 실패(%s): %s", type(exc).__name__, str(exc)[:160])
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def _state_path(db_path: str | None = None) -> str | None:
    """가드 상태 파일 경로. `BROKER_GUARD_STATE=`(빈 값)이면 None(메모리만)."""
    raw = os.environ.get("BROKER_GUARD_STATE")
    if raw is not None:
        value = raw.strip()
        if value.lower() in ("", "0", "off", "false", "none"):
            return None
        return os.path.abspath(value)
    base = db_path or os.environ.get("HISTORY_DB_PATH") or ""
    if base:
        return os.path.join(os.path.dirname(os.path.abspath(base)), "broker_guard.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "broker_guard.json")


class OrderGuard:
    """실주문 안전장치 — 킬 스위치·1회/1일 한도·중복 주문·드라이런.

    검사 순서(먼저 걸리는 것이 답이 된다):
    1. 주문 내용(종목·가격·금액)      2. 키/연동 여부(실계좌)
    3. 킬 스위치(실계좌)              4. 1회 상한(실계좌)
    5. 1일 누적 상한(실계좌)          6. 중복 주문(모든 어댑터)

    1회·1일 한도는 **매수에만** 건다 — 주문 금액이 곧 새로 투입하는 돈이고, 매도까지 막으면
    손절 출구를 막는 셈이라 더 위험하다. 중복 주문은 매수·매도 모두 본다.
    모의투자는 `is_live=False`라 킬 스위치·한도 검사를 건너뛴다(가상 돈이라 상한이 없다).
    드라이런이면 어떤 경우에도 주문을 보내지 않고, 막혔을 사유를 `blocked_by`에 담는다.
    """

    def __init__(self, ledger: OrderLedger | None = None):
        self.ledger = ledger or OrderLedger()

    def evaluate(self, order: OrderRequest, adapter: BrokerAdapter) -> GuardDecision:
        live = bool(adapter.is_live)
        dry_run = False
        if live:
            dry_run = dry_run_enabled() if order.dry_run is None else bool(order.dry_run)

        decision = GuardDecision(
            allowed=True, status=STATUS_APPROVED, dry_run=dry_run, live=live,
            notional_krw=order.notional_krw(), order_max_krw=order_max_krw(),
            daily_max_krw=daily_max_krw(), daily_used_krw=self.ledger.daily_used(),
        )
        blocked_by = self._first_block(order, adapter, decision)
        if dry_run:
            # 드라이런은 아무것도 보내지 않으므로 허용하되, 실주문이었다면 막혔을 사유를 남긴다.
            return replace(decision, allowed=True, status=STATUS_DRY_RUN,
                           reason="", blocked_by=blocked_by)
        if blocked_by:
            status = (STATUS_NOT_CONFIGURED if blocked_by == REASON_NOT_CONFIGURED
                      else STATUS_BLOCKED)
            return replace(decision, allowed=False, status=status, reason=blocked_by,
                           blocked_by=blocked_by)
        return decision

    def _first_block(self, order: OrderRequest, adapter: BrokerAdapter,
                     decision: GuardDecision) -> str:
        if not order.ticker or float(order.price) <= 0 or decision.notional_krw <= 0:
            return REASON_INVALID
        if order.is_sell and float(order.shares) <= 0:
            return REASON_INVALID
        if not order.is_buy and not order.is_sell:
            return REASON_INVALID
        if decision.live and not adapter.configured:
            return REASON_NOT_CONFIGURED
        if decision.live and not real_trading_enabled():
            return REASON_KILL_SWITCH
        if decision.live and order.is_buy and decision.notional_krw > decision.order_max_krw:
            return REASON_ORDER_LIMIT
        if (decision.live and order.is_buy
                and (decision.daily_used_krw + decision.notional_krw) > decision.daily_max_krw):
            return REASON_DAILY_LIMIT
        if self.ledger.is_duplicate(order.order_key()):
            return REASON_DUPLICATE
        return ""

    def record(self, order: OrderRequest, adapter: BrokerAdapter, decision: GuardDecision) -> None:
        """주문이 **성공한 뒤** 호출한다 — 1일 누적과 중복 키를 남긴다.

        드라이런·차단은 기록하지 않는다(드라이런을 반복해도 한도를 먹지 않게).
        실패한 주문은 기록하지 않아 다시 시도할 수 있지만, 어댑터는 반드시 같은
        `order.client_order_id()`를 써서 증권사 쪽에서도 중복이 나지 않게 해야 한다.
        """
        if not decision.allowed or decision.dry_run:
            return
        # 1일 누적은 "새로 쓰는 돈"(매수)만 센다. 매도는 잔고를 줄일 뿐이다.
        spent = decision.notional_krw if (adapter.is_live and order.is_buy) else 0.0
        self.ledger.add(order.order_key(), spent)

    def snapshot(self) -> dict:
        return {
            "real_trading_enabled": real_trading_enabled(),
            "dry_run": dry_run_enabled(),
            "order_max_krw": round(order_max_krw(), 2),
            "daily_max_krw": round(daily_max_krw(), 2),
            "daily_used_krw": self.ledger.daily_used(),
            "tracked_orders": self.ledger.tracked(),
            "state_path": self.ledger.path,
        }


_GUARDS: dict[str, OrderGuard] = {}
_GUARDS_LOCK = threading.Lock()


def guard_for(db_path: str | None = None) -> OrderGuard:
    """같은 저장소를 쓰는 호출끼리 가드(1일 누적·중복 키)를 공유한다."""
    path = _state_path(db_path)
    key = path or f"memory:{db_path or 'default'}"
    with _GUARDS_LOCK:
        guard = _GUARDS.get(key)
        if guard is None:
            guard = OrderGuard(OrderLedger(path))
            _GUARDS[key] = guard
        return guard


def clear_cache() -> None:
    """테스트·설정 변경용 — 기억해 둔 가드를 버린다(파일은 그대로)."""
    with _GUARDS_LOCK:
        _GUARDS.clear()


class GuardedBroker(BrokerAdapter):
    """`OrderGuard`를 씌운 어댑터 — 모든 주문이 이 클래스를 지난다."""

    def __init__(self, adapter: BrokerAdapter, guard: OrderGuard | None = None):
        self.adapter = adapter
        self.guard = guard or guard_for(getattr(adapter, "db_path", None))

    @property
    def name(self) -> str:
        return self.adapter.name

    @property
    def label(self) -> str:
        return self.adapter.label

    @property
    def is_live(self) -> bool:
        return self.adapter.is_live

    @property
    def implemented(self) -> bool:
        return self.adapter.implemented

    @property
    def configured(self) -> bool:
        return self.adapter.configured

    def missing_env(self) -> list[str]:
        return self.adapter.missing_env()

    def place_buy(self, order: OrderRequest) -> OrderResult:
        return self._place(order, self.adapter.place_buy)

    def place_sell(self, order: OrderRequest) -> OrderResult:
        return self._place(order, self.adapter.place_sell)

    def get_balance(self, db_path: str | None = None,
                    account_id: int | None = None) -> BrokerAccountInfo:
        return self.adapter.get_balance(db_path=db_path, account_id=account_id)

    def health(self) -> BrokerHealth:
        info = self.adapter.health()
        return info

    def _place(self, order: OrderRequest, place_fn) -> OrderResult:
        decision = self.guard.evaluate(order, self.adapter)
        if not decision.allowed:
            logger.warning("주문 차단 %s %s %s [%s]: %s", self.name, order.side, order.ticker,
                           decision.status, decision.reason)
            return OrderResult(ok=False, status=decision.status, adapter=self.name,
                               live=decision.live, reason=decision.reason,
                               guard=decision.as_dict())
        if decision.dry_run:
            note = ("드라이런 — 실주문이었다면 막혔습니다: "
                    f"{REASON_LABELS.get(decision.blocked_by, decision.blocked_by)} "
                    f"({decision.blocked_by})"
                    if decision.blocked_by else "드라이런 — 주문을 보내지 않았습니다.")
            return OrderResult(ok=True, status=STATUS_DRY_RUN, adapter=self.name,
                               live=decision.live, reason=note, guard=decision.as_dict())
        result = place_fn(order)
        if result.ok:
            self.guard.record(order, self.adapter, decision)
        result.guard = decision.as_dict()
        return result


# --- 선택·요약 ----------------------------------------------------------------


def build_adapter(mode: str | None = None, market=None, db_path: str | None = None,
                  account_id: int | None = None) -> BrokerAdapter:
    """모드 → 어댑터(가드 미적용). 모르는 모드는 paper."""
    name = _clean(mode).lower() or broker_mode()
    factory = ADAPTERS.get(name) or ADAPTERS[DEFAULT_MODE]
    if name == "paper" or factory is PaperBroker:
        return PaperBroker(market=market, db_path=db_path, account_id=account_id)
    return factory()


def get_adapter(mode: str | None = None, market=None, db_path: str | None = None,
                account_id: int | None = None) -> GuardedBroker:
    """이번 실행에 쓸 어댑터 — 항상 `OrderGuard`가 씌워진 채로 나온다."""
    adapter = build_adapter(mode=mode, market=market, db_path=db_path, account_id=account_id)
    return GuardedBroker(adapter, guard_for(db_path))


def active_broker(market=None, db_path: str | None = None,
                  account_id: int | None = None) -> GuardedBroker:
    """`BROKER_MODE`가 고른 어댑터(가드 포함). `get_adapter`의 짧은 이름."""
    return get_adapter(None, market=market, db_path=db_path, account_id=account_id)


def adapter_status(name: str) -> dict:
    """어댑터 하나의 상태(키 없이도 안전한 정보만) — `/` 응답과 문서가 같은 값을 본다."""
    cls = ADAPTERS.get(name)
    if cls is None:
        return {"name": name, "label": name, "supported": False}
    missing = [key for key in cls.api_key_envs if not _clean(os.environ.get(key))]
    configured = not missing
    live = bool(cls.is_live)
    implemented = bool(getattr(cls, "implemented", True))
    status = "ok" if configured and implemented else STATUS_NOT_CONFIGURED
    return {
        "name": name,
        "label": MODE_LABELS.get(name, name),
        "supported": True,
        "live": live,
        "configured": configured,
        "implemented": implemented,
        "status": status,
        "missing_env": missing,
    }


def broker_summary() -> dict:
    """`GET /`의 `broker` 블록 — DB·시세를 건드리지 않는다(헬스 체크가 10분마다 부른다)."""
    try:
        mode = broker_mode()
        names = list(ADAPTERS) if "paper" not in ADAPTERS else (
            ["paper"] + [name for name in ADAPTERS if name != "paper"])
        adapters = [adapter_status(name) for name in names]
        chosen = next((row for row in adapters if row["name"] == mode), adapters[0])
        guard = guard_for(None)
        return {
            "mode": mode,
            "requested_mode": requested_mode(),
            "label": MODE_LABELS.get(mode, mode),
            "live": bool(chosen.get("live")),
            "configured": bool(chosen.get("configured")),
            "real_trading_enabled": real_trading_enabled(),
            "dry_run": dry_run_enabled(),
            "supported_adapters": list(SUPPORTED_MODES),
            "adapters": adapters,
            "guard": guard.snapshot(),        }
    except Exception as exc:  # 헬스 체크는 어떤 경우에도 200으로 돌아와야 한다
        logger.warning("브로커 요약 생성 실패(%s): %s", type(exc).__name__, str(exc)[:160])
        return {"mode": DEFAULT_MODE, "requested_mode": requested_mode(), "live": False,
                "real_trading_enabled": real_trading_enabled(), "dry_run": dry_run_enabled(),
                "supported_adapters": list(SUPPORTED_MODES), "adapters": [], "error": "summary_failed"}
