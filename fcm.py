"""AI 모의투자 체결 FCM 푸시 알림 — Firebase Admin 초기화·발송·토큰 정리.

설계 원칙
- **키는 환경변수에서만 읽는다**: `FIREBASE_SERVICE_ACCOUNT_JSON`(JSON 문자열 전체 또는 base64)을
  먼저 보고, 없으면 `FIREBASE_CREDENTIALS_PATH`(파일 경로)를 쓴다. 키 JSON은 절대 Git에 올리지
  않는다(`.gitignore`의 `*firebase-adminsdk*.json`). 응답·로그에는 키 본문을 남기지 않는다.
- **설정이 없어도 서버는 산다**: 키가 없거나 SDK 초기화에 실패하면 `configured: false`로
  `skipped`만 돌려주고 로그 한 줄만 남긴다. 모의투자 매매는 그대로 돌아간다.
- **발송이 매매를 막지 않는다**: 모든 예외는 여기서 흡수한다. 발송은 별도 스레드에서 최대
  `SEND_TIMEOUT_SEC`만 기다리므로 FCM이 느려도 `run_paper_trading`의 실행 락이 묶이지 않는다.
- **죽은 토큰은 스스로 정리한다**: `UnregisteredError`·`InvalidArgumentError`(앱 삭제·재설치·
  토큰 회전)로 실패한 토큰은 DB(`paper_fcm_tokens`)에서 지운다.

알림 문구(요청 사양)
- 단건 매수: `🤖 AI 매수 체결 · 현대글로비스` / `1주 · ₩201,500 (수수료 ₩30)`
- 단건 매도: `🤖 AI 매도 체결 · 오리온` / `순손익 +₩12,300 (+4.2%)`
- 다건: `🤖 AI 모의투자 3건 체결` / `현대글로비스 외 2건 매수 · 총 ₩681,300`
- `data`: `{"screen": "paper", "market": ...}` — 알림을 탭하면 앱이 🤖 AI 모의투자 화면으로 연다.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from typing import Any

import paper_store

logger = logging.getLogger("fcm")

CHANNEL_ID = "paper_trade_alerts"
ANDROID_CHANNEL_NAME = "AI 매매 체결 알림"
ENV_JSON = "FIREBASE_SERVICE_ACCOUNT_JSON"
ENV_PATH = "FIREBASE_CREDENTIALS_PATH"
# 발송 대기 상한(초). 넘으면 '느린 발송'으로 보고 매매 실행은 계속 진행한다.
SEND_TIMEOUT_SEC = 12.0
# FCM 메시지 수명 — 30분이 지난 체결 알림은 늦게 와도 의미가 없다.
MESSAGE_TTL_MINUTES = 30

_LOCK = threading.Lock()
_APP = None
_INIT_ERROR: str | None = None
_CREDENTIAL_SOURCE: str | None = None
_PROJECT_ID: str | None = None


# --- 자격 증명 ----------------------------------------------------------------


def _decode_service_account(raw: str) -> dict | None:
    """환경변수 문자열 → 서비스 계정 dict. JSON 문자열과 base64 둘 다 받는다."""
    text = (raw or "").strip()
    if not text:
        return None
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text).decode("utf-8")
        except Exception:
            return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("project_id"):
        return None
    if not (data.get("private_key") or data.get("client_email")):
        return None
    # 줄바꿈이 `\\n`으로 한 번 더 이스케이프된 경우(대시보드 복붙 사고)를 교정한다.
    key = data.get("private_key")
    if isinstance(key, str) and "\\n" in key:
        data["private_key"] = key.replace("\\n", "\n")
    return data


def credentials_source() -> str | None:
    """쓸 자격 증명의 출처(환경변수 이름). 없으면 None."""
    if (os.environ.get(ENV_JSON) or "").strip():
        return ENV_JSON
    if (os.environ.get(ENV_PATH) or "").strip():
        return ENV_PATH
    return None


def _load_credentials():
    """(credentials.Certificate, 출처) 또는 (None, 이유). 키 본문은 반환·로그에 남기지 않는다."""
    source = credentials_source()
    if source is None:
        return None, "not_configured"
    try:
        from firebase_admin import credentials
    except Exception as exc:  # firebase-admin 미설치
        return None, f"firebase_admin_missing:{type(exc).__name__}"

    if source == ENV_JSON:
        data = _decode_service_account(os.environ.get(ENV_JSON) or "")
        if data is None:
            return None, "invalid_service_account_json"
        return credentials.Certificate(data), source

    path = (os.environ.get(ENV_PATH) or "").strip()
    if not os.path.exists(path):
        return None, "credentials_path_not_found"
    try:
        return credentials.Certificate(path), source
    except Exception as exc:
        return None, f"invalid_credentials_file:{type(exc).__name__}"


def _get_app():
    """Firebase 앱(1회 초기화). 설정이 없거나 실패하면 None."""
    global _APP, _INIT_ERROR, _CREDENTIAL_SOURCE, _PROJECT_ID
    if _APP is not None:
        return _APP
    with _LOCK:
        if _APP is not None:
            return _APP
        cred, source_or_reason = _load_credentials()
        if cred is None:
            _INIT_ERROR = source_or_reason
            _CREDENTIAL_SOURCE = None
            return None
        try:
            import firebase_admin
            _APP = firebase_admin.initialize_app(cred)
            _CREDENTIAL_SOURCE = source_or_reason
            _PROJECT_ID = getattr(cred, "project_id", None)
            _INIT_ERROR = None
            logger.info("FCM 초기화 완료(프로젝트 %s, 출처 %s)", _PROJECT_ID or "?", source_or_reason)
        except Exception as exc:
            _INIT_ERROR = f"init_failed:{type(exc).__name__}"
            logger.warning("FCM 초기화 실패: %s: %s", type(exc).__name__, str(exc)[:200])
            return None
    return _APP


def is_configured() -> bool:
    """지금 푸시를 보낼 수 있는 상태인가(키가 있고 초기화가 됐는가)."""
    return _get_app() is not None


def status(db_path: str | None = None) -> dict:
    """앱·운영 확인용 상태 — 키 본문 없이 설정 여부와 등록 기기 수만 노출한다."""
    configured = is_configured()
    out = {
        "configured": configured,
        "source": _CREDENTIAL_SOURCE,
        "project_id": _PROJECT_ID,
        "channel_id": CHANNEL_ID,
    }
    # 배포 환경에 firebase-admin이 실제로 설치됐는지(키를 넣기 전에 확인할 수 있게) 버전만 노출한다.
    try:
        import firebase_admin as _sdk
        out["sdk"] = getattr(_sdk, "__version__", "installed")
    except Exception:
        out["sdk"] = None
    if not configured:
        out["reason"] = _INIT_ERROR or "not_configured"
    try:
        out["tokens"] = paper_store.count_fcm_tokens(db_path)
    except Exception as exc:
        out["tokens"] = None
        out["tokens_error"] = type(exc).__name__
    return out


def reset_init() -> None:
    """테스트·키 교체용 — 앱 핸들을 버려 다음 호출에서 다시 초기화한다."""
    global _APP, _INIT_ERROR, _CREDENTIAL_SOURCE, _PROJECT_ID
    with _LOCK:
        _APP = None
        _INIT_ERROR = None
        _CREDENTIAL_SOURCE = None
        _PROJECT_ID = None


# --- 알림 문구 ----------------------------------------------------------------


def _display_name(trade: dict) -> str:
    name = str(trade.get("name") or "").strip()
    ticker = str(trade.get("ticker") or "").strip().upper()
    return name or ticker or "종목"


def _won(value: Any) -> str:
    try:
        return f"₩{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "₩0"


def _won_signed(value: Any) -> str:
    try:
        amount = int(round(float(value)))
    except (TypeError, ValueError):
        return "₩0"
    return f"+₩{amount:,}" if amount > 0 else (f"-₩{abs(amount):,}" if amount < 0 else "₩0")


def _shares(value: Any) -> str:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return "0"
    if abs(qty - round(qty)) < 1e-6:
        return f"{int(round(qty)):,}"
    return f"{qty:,.4f}".rstrip("0").rstrip(".")


def _settle_krw(trade: dict) -> float:
    """체결이 실제로 움직인 원화 — 매수는 원금+수수료(나간 돈), 매도는 순수취액(들어온 돈)."""
    amount = float(trade.get("amount_krw") or 0.0)
    fee = float(trade.get("fee_krw") or 0.0)
    return amount + fee if str(trade.get("side") or "").upper() == "BUY" else amount - fee


def _sell_body(trade: dict) -> str:
    realized = trade.get("realized_pnl_krw")
    if realized is None:
        return f"{_shares(trade.get('shares'))}주 · 순수취 {_won(_settle_krw(trade))}"
    net = _settle_krw(trade)
    cost = net - float(realized)
    pct = (float(realized) / cost * 100.0) if cost > 0 else None
    pct_text = f" ({pct:+.1f}%)" if pct is not None else ""
    return f"순손익 {_won_signed(realized)}{pct_text}"


def _account_badge(markets: list[str]) -> str:
    """체결 시장 → 계좌 구분 표식(`[미국]` / `[한국]` / `[미국+한국]`)."""
    labels = {"US": "미국", "KR": "한국"}
    order = [market for market in ("US", "KR") if market in markets]
    if not order:
        return ""
    return "[" + "+".join(labels[market] for market in order) + "]"


def build_fill_message(sells: list[dict] | None, buys: list[dict] | None) -> dict:
    """체결 목록 → FCM 알림 제목·본문·data payload. 체결이 없으면 빈 dict.

    두 계좌(미국/한국)를 나란히 굴리므로, 알림만 봐도 **어느 계좌의 체결인지** 알 수 있게
    제목에 `[미국]`/`[한국]`, 본문 앞에 `미국 계좌 · `를 붙인다.
    """
    trades = list(sells or []) + list(buys or [])
    if not trades:
        return {}
    markets = sorted({str(t.get("market") or "").upper() for t in trades if t.get("market")})
    badge = _account_badge(markets)
    head = f"🤖 {badge} " if badge else "🤖 "
    account_text = (badge.strip("[]") + " 계좌 · ") if badge else ""
    data = {
        "screen": "paper",
        "market": ",".join(markets) or "ALL",
        "count": str(len(trades)),
    }

    if len(trades) == 1:
        trade = trades[0]
        name = _display_name(trade)
        if str(trade.get("side") or "").upper() == "SELL":
            return {"title": f"{head}AI 매도 체결 · {name}",
                    "body": account_text + _sell_body(trade), "data": data}
        body = f"{_shares(trade.get('shares'))}주 · {_won(trade.get('amount_krw'))}"
        fee = float(trade.get("fee_krw") or 0.0)
        if fee > 0:
            body += f" (수수료 {_won(fee)})"
        return {"title": f"{head}AI 매수 체결 · {name}",
                "body": account_text + body, "data": data}

    buy_rows = [t for t in trades if str(t.get("side") or "").upper() == "BUY"]
    sell_rows = [t for t in trades if str(t.get("side") or "").upper() == "SELL"]
    if buy_rows and not sell_rows:
        detail = f"매수 · 총 {_won(sum(_settle_krw(t) for t in buy_rows))}"
    elif sell_rows and not buy_rows:
        detail = f"매도 · 총 {_won(sum(_settle_krw(t) for t in sell_rows))}"
    else:
        detail = (f"매수 {_won(sum(_settle_krw(t) for t in buy_rows))} · "
                  f"매도 {_won(sum(_settle_krw(t) for t in sell_rows))}")
    body = f"{_display_name(trades[0])} 외 {len(trades) - 1}건 {detail}"
    return {"title": f"{head}AI 모의투자 {len(trades)}건 체결",
            "body": account_text + body, "data": data}


# --- 발송 ---------------------------------------------------------------------


class SendOutcome:
    """발송 결과 — 성공 수, 실패 수, 그리고 지워야 할 죽은 토큰."""

    __slots__ = ("success", "failure", "dead_tokens")

    def __init__(self, success: int = 0, failure: int = 0, dead_tokens: list[str] | None = None):
        self.success = int(success)
        self.failure = int(failure)
        self.dead_tokens = list(dead_tokens or [])


def _is_dead_token(exc: BaseException) -> bool:
    """앱 삭제·재설치·토큰 회전으로 더는 배달할 수 없는 토큰인가."""
    if type(exc).__name__ in ("UnregisteredError", "InvalidArgumentError"):
        return True
    text = str(exc).lower()
    return ("not-registered" in text or "not registered" in text
            or "invalid registration" in text or "requested entity was not found" in text)


def _send_multicast(tokens: list[str], title: str, body: str, data: dict) -> SendOutcome:
    """실제 FCM 호출(테스트는 이 함수를 바꿔 끼운다)."""
    from datetime import timedelta

    from firebase_admin import messaging

    message = messaging.MulticastMessage(
        tokens=list(tokens),
        notification=messaging.Notification(title=title, body=body),
        data={str(k): str(v) for k, v in (data or {}).items()},
        android=messaging.AndroidConfig(
            priority="high",
            ttl=timedelta(minutes=MESSAGE_TTL_MINUTES),
            notification=messaging.AndroidNotification(
                channel_id=CHANNEL_ID,
                sound="default",
            ),
        ),
    )
    app = _get_app()
    response = messaging.send_each_for_multicast(message, app=app)
    dead: list[str] = []
    success = 0
    failure = 0
    for token, single in zip(tokens, response.responses):
        exc = getattr(single, "exception", None)
        if exc is None:
            success += 1
            continue
        failure += 1
        if _is_dead_token(exc):
            dead.append(token)
    return SendOutcome(success=success, failure=failure, dead_tokens=dead)


def _send_with_deadline(tokens: list[str], title: str, body: str, data: dict) -> SendOutcome:
    """발송을 별도 스레드에서 최대 `SEND_TIMEOUT_SEC`만 기다린다(매매 실행을 붙잡지 않는다)."""
    holder: dict[str, Any] = {}

    def _work() -> None:
        try:
            holder["outcome"] = _send_multicast(tokens, title, body, data)
        except BaseException as exc:  # noqa: BLE001 - 발송 실패는 매매와 무관하다
            holder["error"] = exc

    worker = threading.Thread(target=_work, name="fcm-send", daemon=True)
    worker.start()
    worker.join(SEND_TIMEOUT_SEC)
    if worker.is_alive():
        logger.warning("FCM 발송이 %.0f초를 넘겨 기다리지 않습니다(토큰 %d개)", SEND_TIMEOUT_SEC, len(tokens))
        return SendOutcome(failure=len(tokens))
    if "error" in holder:
        error = holder["error"]
        logger.warning("FCM 발송 실패: %s: %s", type(error).__name__, str(error)[:200])
        return SendOutcome(failure=len(tokens))
    return holder.get("outcome") or SendOutcome(failure=len(tokens))


def _dispatch(title: str, body: str, data: dict, db_path: str | None,
              user_id: int | None = None) -> dict:
    """지정 유저의 등록 토큰으로 보내고, 죽은 토큰을 DB에서 정리한 결과를 돌려준다.

    `user_id`가 없으면 예전처럼 전체 기기(관리자 알림 — 기존 동작)로 보낸다.
    """
    ready = is_configured()
    if not ready:
        logger.info("FCM 미설정(%s) — 푸시를 건너뜁니다", _INIT_ERROR or "not_configured")
        try:
            registered = paper_store.count_fcm_tokens(db_path, user_id)
        except Exception:
            registered = 0
        return {"configured": False, "reason": _INIT_ERROR or "not_configured",
                "sent": 0, "failed": 0, "removed": 0, "tokens": registered}
    try:
        tokens = [row["token"] for row in paper_store.list_fcm_tokens(db_path, user_id)]
    except Exception as exc:
        logger.warning("FCM 토큰 조회 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        return {"configured": True, "sent": 0, "failed": 0, "removed": 0, "tokens": 0,
                "error": "token_lookup_failed"}
    if not tokens:
        return {"configured": True, "sent": 0, "failed": 0, "removed": 0, "tokens": 0}

    outcome = _send_with_deadline(tokens, title, body, data)
    removed = 0
    if outcome.dead_tokens:
        try:
            removed = paper_store.remove_fcm_tokens(outcome.dead_tokens, db_path)
        except Exception as exc:
            logger.warning("만료 FCM 토큰 정리 실패: %s", type(exc).__name__)
    if outcome.success or outcome.failure:
        logger.info("FCM 발송: 성공 %d · 실패 %d · 정리 %d (\"%s\")",
                    outcome.success, outcome.failure, removed, title)
    return {"configured": True, "sent": outcome.success, "failed": outcome.failure,
            "removed": removed, "tokens": len(tokens)}


def notify_fills(sells: list[dict] | None, buys: list[dict] | None,
                 db_path: str | None = None, user_id: int | None = None) -> dict:
    """체결 직후 호출 — 실제 체결이 있을 때만 그 유저의 기기로 알림을 보낸다. 예외는 밖으로 던지지 않는다."""
    try:
        message = build_fill_message(sells, buys)
        if not message:
            return {"configured": is_configured(), "sent": 0, "failed": 0, "removed": 0, "tokens": 0}
        return _dispatch(message["title"], message["body"], message["data"], db_path, user_id)
    except Exception as exc:  # 발송은 부가 기능 — 매매 결과를 깨지 않는다
        logger.warning("체결 푸시 처리 실패: %s: %s", type(exc).__name__, str(exc)[:200])
        return {"configured": False, "sent": 0, "failed": 0, "removed": 0, "tokens": 0,
                "error": "push_failed"}


def send_test(db_path: str | None = None, user_id: int | None = None) -> dict:
    """`POST /api/paper/fcm/test` — 이 유저의 등록 기기로 테스트 푸시 1건. 체결·계좌에는 손대지 않는다."""
    title = "🔔 테스트 알림 · AI 모의투자"
    body = "푸시 알림이 정상 동작합니다. 매수·매도가 체결되면 바로 알려드릴게요."
    data = {"screen": "paper", "market": "ALL", "test": "1"}
    return _dispatch(title, body, data, db_path, user_id)
