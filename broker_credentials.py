"""증권사 API 키 보관소 — 화면(앱·웹)에서 받은 비밀값을 **암호화해서만** DB에 둔다.

왜 이 모듈이 있나
-----------------
`broker.py`는 어댑터마다 필요한 환경변수 **이름**만 알고(`api_key_envs`), 실제 값은
지금까지 Render 환경변수에만 두었다. 그런데 앱에서 직접 키를 넣고 싶다는 요구가 생겨,
값을 서버 DB에 보관하되 아래 원칙을 지킨다.

1. **평문 저장 금지** — 값은 `cryptography.fernet`(AES-128-CBC + HMAC-SHA256)으로 암호화한
   토큰 하나로만 저장한다. DB를 통째로 덤프해도 키 원문은 나오지 않는다.
2. **키 재료는 DB 밖에** — 암호화 키는 `BROKER_CRED_KEY`(권장)에서 PBKDF2-HMAC-SHA256으로
   유도한다. 설정이 없으면 서버에 이미 있는 비밀(`DATABASE_URL` → `FIREBASE_SERVICE_ACCOUNT_JSON`
   → `GOOGLE_API_KEY`/`GEMINI_API_KEY` → `FINN_API_KEY`)에서 유도한다. 그것도 없으면
   **저장을 거부한다**(평문으로 떨어지지 않는다).
3. **원문은 응답·로그에 절대 없다** — 화면에는 끝 4자리만 마스킹한 힌트(`••••3f9a`)와
   등록 시각·필드 이름만 준다. `GET /api/broker/status`·`GET /`·`/api/paper/status` 어디에도
   원문은 없다. 로그에도 어댑터 이름·필드 이름만 남긴다.
4. **키를 넣어도 주문은 안 나간다** — 등록은 "보관"일 뿐이고, 실제 주문은 `OrderGuard`의
   킬 스위치(`REAL_TRADING_ENABLED`, 기본 false)·드라이런(`REAL_DRY_RUN`, 기본 true)과
   어댑터 구현 여부(`implemented`)를 모두 통과해야 나간다.

테이블은 모의투자·성적표와 같은 저장소를 쓴다(`paper_store._connection` → `DATABASE_URL`이
있으면 PostgreSQL, 없으면 SQLite). 컬럼:

    adapter     TEXT PRIMARY KEY   -- "kis" | "toss" (paper·mirae는 등록 대상 아님)
    cipher      TEXT NOT NULL      -- Fernet 토큰(암호문)
    fields      TEXT NOT NULL      -- 저장한 필드 이름(쉼표) — 값 아님
    hint        TEXT NOT NULL      -- 필드별 마스킹 힌트(JSON) — 화면 표시용
    created_at  TEXT
    updated_at  TEXT
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time

import paper_store

logger = logging.getLogger("broker_credentials")

TABLE = "broker_credentials"

# 암호화 키 유도 — 고정 salt + 충분한 반복. 키 재료가 바뀌면 기존 암호문은 못 읽는다(그때는 재등록).
SALT = b"stock-ai-scanner/broker-credentials/v1"
KDF_ITERATIONS = 200_000
KEY_SOURCES = ("DATABASE_URL", "FIREBASE_SERVICE_ACCOUNT_JSON", "GOOGLE_API_KEY",
               "GEMINI_API_KEY", "FINN_API_KEY")

# 화면에서 받는 필드 — 증권사 환경변수 이름을 이 셋으로 접는다.
FIELD_APP_KEY = "app_key"
FIELD_APP_SECRET = "app_secret"
FIELD_ACCOUNT_NO = "account_no"
FIELDS = (FIELD_APP_KEY, FIELD_APP_SECRET, FIELD_ACCOUNT_NO)
FIELD_LABELS = {FIELD_APP_KEY: "App Key", FIELD_APP_SECRET: "App Secret",
                FIELD_ACCOUNT_NO: "계좌번호"}
MAX_LEN = {FIELD_APP_KEY: 512, FIELD_APP_SECRET: 512, FIELD_ACCOUNT_NO: 64}

# 등록을 받는 어댑터 — 미래에셋은 개인용 Open API가 아직 없어(2026-10 개발 착수 보도, 시기 미정)
# 등록 자체를 막고 안내만 한다.
REGISTERABLE = ("kis", "toss")
NOT_REGISTERABLE = {
    "mirae": "미래에셋증권은 개인용 Open API가 아직 출시되지 않았습니다"
             "(2026-10 개발 착수 보도, 출시 시기 미정). 출시 뒤 등록할 수 있어요.",
    "paper": "모의투자 계좌는 API 키가 필요 없습니다.",
}

STATUS_TTL = 20.0     # 상태 스냅샷 재사용(화면·헬스체크가 같은 값을 자주 읽는다)
_AUTH_HEADER = "X-Broker-Token"


class CredentialsError(Exception):
    """화면에 그대로 보여 줄 수 있는 오류(원문·키 재료는 절대 담지 않는다)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(code)
        self.code = code
        self.message = message or code


class KeyUnavailable(CredentialsError):
    def __init__(self, message: str = ""):
        super().__init__("credentials_key_unavailable", message or (
            "서버에 암호화 키 재료가 없어 키를 안전하게 저장할 수 없습니다. "
            "Render 환경변수에 BROKER_CRED_KEY를 넣어 주세요(평문으로는 저장하지 않습니다)."))


class UnknownAdapter(CredentialsError):
    def __init__(self, adapter: str):
        super().__init__("unknown_adapter", f"지원하지 않는 증권사입니다: {adapter}")


class NotRegisterable(CredentialsError):
    def __init__(self, adapter: str, message: str):
        super().__init__("broker_not_registerable", message)


class DecryptFailed(CredentialsError):
    def __init__(self, adapter: str):
        super().__init__("credentials_decrypt_failed", (
            f"{adapter} 키를 해독할 수 없습니다 — 서버 암호화 키가 바뀌었을 수 있어요. 다시 등록해 주세요."))


# --- 암호화 키 -----------------------------------------------------------------


_KEY_CACHE: dict[str, tuple[bytes, str]] = {}
_KEY_LOCK = threading.Lock()


def _clean(value) -> str:
    return str(value or "").strip()


def _key_material() -> tuple[str, str]:
    """(키 재료, 출처). DB에 넣지 않는 값만 쓴다 — 없으면 KeyUnavailable."""
    explicit = _clean(os.environ.get("BROKER_CRED_KEY"))
    if explicit:
        return explicit, "BROKER_CRED_KEY"
    for name in KEY_SOURCES:
        value = _clean(os.environ.get(name))
        if len(value) >= 16:
            return "stock-ai-scanner|" + value, "derived:" + name
    raise KeyUnavailable()


def _fernet():
    """Fernet 인스턴스와 키 출처. cryptography는 쓸 때만 임포트한다(부팅 메모리 절약)."""
    material, source = _key_material()
    with _KEY_LOCK:
        cached = _KEY_CACHE.get(material)
        if cached is None:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

            derived = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT,
                                 iterations=KDF_ITERATIONS).derive(material.encode("utf-8"))
            cached = (base64.urlsafe_b64encode(derived), source)
            if len(_KEY_CACHE) > 4:      # 재료가 바뀌어도 무한히 쌓이지 않게
                _KEY_CACHE.clear()
            _KEY_CACHE[material] = cached
        key = cached[0]
    from cryptography.fernet import Fernet

    return Fernet(key), source


def encryption_status() -> dict:
    """암호화 준비 상태 — 재료 이름만 노출한다(값 아님)."""
    try:
        _, source = _fernet()
    except KeyUnavailable:
        return {"algorithm": "fernet", "ready": False, "key_source": None,
                "message": "암호화 키 재료가 없어 키 저장이 잠겨 있습니다(BROKER_CRED_KEY 권장)."}
    return {"algorithm": "fernet", "ready": True, "key_source": source, "message": ""}


# --- 필드·마스킹 ----------------------------------------------------------------


def field_for_env(env_name: str) -> str:
    """`KIS_APP_KEY` → `app_key` 처럼 증권사 환경변수 이름을 공통 필드로 접는다."""
    name = _clean(env_name).upper()
    if name.endswith("ACCOUNT_NO"):
        return FIELD_ACCOUNT_NO
    if name.endswith("APP_SECRET") or name.endswith("CLIENT_SECRET"):
        return FIELD_APP_SECRET
    return FIELD_APP_KEY     # *_APP_KEY / *_CLIENT_ID / *_API_KEY


def fields_for(adapter: str) -> tuple[str, ...]:
    """어댑터가 요구하는 환경변수 이름(정의는 `broker.py` 한 곳에 있다)."""
    import broker  # 순환 임포트 방지용 지연 임포트

    cls = broker.ADAPTERS.get(adapter)
    if cls is None:
        raise UnknownAdapter(adapter)
    return tuple(cls.api_key_envs)


def adapter_field_labels(adapter: str) -> list[dict]:
    """화면이 그릴 입력 칸(이름·라벨·필드) — 증권사마다 필요한 값이 다르다."""
    out = []
    for env in fields_for(adapter):
        field = field_for_env(env)
        out.append({"field": field, "env": env, "label": FIELD_LABELS.get(field, env),
                    "secret": field != FIELD_ACCOUNT_NO})
    return out


def mask(value: str) -> str:
    """끝 4자리만 남긴 마스킹. 짧은 값은 아무것도 보여 주지 않는다."""
    text = _clean(value)
    if len(text) >= 8:
        return "••••" + text[-4:]
    if len(text) >= 4:
        return "••••"
    return "••••"


def _hint_label(hint: dict) -> str:
    """`App Key ••••3f9a · 계좌번호 ••••-01` — 카드에 한 줄로 쓰는 표시."""
    parts = []
    for field in FIELDS:
        value = _clean((hint or {}).get(field))
        if value:
            parts.append(f"{FIELD_LABELS[field]} {value}")
    return " · ".join(parts)


# --- 스키마 -------------------------------------------------------------------


_SQLITE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    adapter    TEXT PRIMARY KEY,
    cipher     TEXT NOT NULL,
    fields     TEXT NOT NULL DEFAULT '',
    hint       TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT,
    updated_at TEXT
)
"""

_PG_SCHEMA = _SQLITE_SCHEMA

_READY: set[str] = set()
_READY_LOCK = threading.Lock()


def _ensure_schema(conn, backend: str, key: str) -> None:
    if key in _READY:
        return
    with _READY_LOCK:
        if key in _READY:
            return
        conn.execute(_SQLITE_SCHEMA if backend == "sqlite" else _PG_SCHEMA)
        conn.commit()      # DDL만 먼저 확정한다(읽기 연결이어도 테이블은 남는다)
        _READY.add(key)


def _schema_key(db_path: str | None, backend: str) -> str:
    if backend == "postgres":
        return "postgres"
    return "sqlite:" + str(db_path or paper_store.history_store.get_db_path())


# --- 저장·삭제 ------------------------------------------------------------------


def _normalize(adapter: str, values: dict) -> dict:
    """어댑터 환경변수 → 값. 빠진 값·제어문자·과길이를 막는다(원문은 메시지에 넣지 않는다)."""
    adapter = _clean(adapter).lower()
    if adapter in NOT_REGISTERABLE:
        raise NotRegisterable(adapter, NOT_REGISTERABLE[adapter])
    if adapter not in REGISTERABLE:
        raise UnknownAdapter(adapter)
    envs = fields_for(adapter)
    out: dict[str, str] = {}
    for env in envs:
        field = field_for_env(env)
        value = _clean((values or {}).get(field))
        if not value:
            raise CredentialsError("missing_field", f"{FIELD_LABELS.get(field, field)} 값이 비어 있습니다.")
        if any(ch in value for ch in "\r\n\t") or any(ord(ch) < 32 for ch in value):
            raise CredentialsError("invalid_field", f"{FIELD_LABELS.get(field, field)}에 줄바꿈·제어문자가 있습니다.")
        if len(value) > MAX_LEN.get(field, 512):
            raise CredentialsError("field_too_long", f"{FIELD_LABELS.get(field, field)} 값이 너무 깁니다.")
        out[env] = value
    return out


def save(adapter: str, values: dict, db_path: str | None = None) -> dict:
    """키를 암호화해 저장(있으면 덮어쓴다)하고, 마스킹된 상태 한 건을 돌려준다.

    원문은 반환값에도 로그에도 남기지 않는다.
    """
    normalized = _normalize(adapter, values)
    adapter = _clean(adapter).lower()
    fernet, _ = _fernet()          # 키 재료가 없으면 여기서 KeyUnavailable(평문 저장 없음)
    plaintext = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    cipher = fernet.encrypt(plaintext).decode("ascii")
    hint = {field_for_env(env): mask(value) for env, value in normalized.items()}
    fields = ",".join(sorted({field_for_env(env) for env in normalized}))
    stamp = paper_store._now()
    with paper_store._connection(db_path, write=True) as (conn, backend):
        _ensure_schema(conn, backend, _schema_key(db_path, backend))
        conn.execute(
            paper_store._sql(
                f"INSERT INTO {TABLE} (adapter, cipher, fields, hint, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (adapter) DO UPDATE SET cipher = excluded.cipher,"
                " fields = excluded.fields, hint = excluded.hint, updated_at = excluded.updated_at",
                backend),
            (adapter, cipher, fields, json.dumps(hint, ensure_ascii=False), stamp, stamp),
        )
    _invalidate()
    logger.info("증권사 키 등록(adapter=%s, fields=%s) — 값은 기록하지 않습니다", adapter, fields)
    return {"adapter": adapter, "registered": True, "fields": fields.split(",") if fields else [],
            "hint": hint, "hint_label": _hint_label(hint), "updated_at": stamp}


def delete(adapter: str, db_path: str | None = None) -> int:
    """등록된 키를 지운다(연동 해제). 지운 행 수를 돌려준다."""
    adapter = _clean(adapter).lower()
    with paper_store._connection(db_path, write=True) as (conn, backend):
        _ensure_schema(conn, backend, _schema_key(db_path, backend))
        cur = conn.execute(paper_store._sql(f"DELETE FROM {TABLE} WHERE adapter = ?", backend),
                           (adapter,))
        removed = int(getattr(cur, "rowcount", 0) or 0)
    _invalidate()
    if removed:
        logger.info("증권사 키 삭제(adapter=%s)", adapter)
    return removed


def load_values(adapter: str, db_path: str | None = None) -> dict:
    """복호화한 값(환경변수 이름 → 값) — **서버 내부 전용**. 화면·API로 내보내지 않는다.

    등록이 없으면 빈 dict. 등록은 있는데 못 읽으면 `DecryptFailed`(원문은 담지 않는다).
    """
    adapter = _clean(adapter).lower()
    row = _read_row(adapter, db_path)
    if not row:
        return {}
    fernet, _ = _fernet()
    try:
        raw = fernet.decrypt(str(row.get("cipher") or "").encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise DecryptFailed(adapter)
    if not isinstance(data, dict):
        raise DecryptFailed(adapter)
    return {str(k): str(v) for k, v in data.items()}


def _read_row(adapter: str, db_path: str | None = None) -> dict | None:
    with paper_store._connection(db_path) as (conn, backend):
        _ensure_schema(conn, backend, _schema_key(db_path, backend))
        row = paper_store._row(conn.execute(
            paper_store._sql(f"SELECT adapter, cipher, fields, hint, created_at, updated_at"
                             f" FROM {TABLE} WHERE adapter = ?", backend), (adapter,)))
    return row or None


# --- 상태(마스킹)·요약 -----------------------------------------------------------


_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _invalidate() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_key(db_path: str | None) -> str:
    return "postgres" if paper_store.storage_backend(db_path) == "postgres" else str(
        db_path or paper_store.history_store.get_db_path())


def _rows(db_path: str | None = None) -> list[dict]:
    with paper_store._connection(db_path) as (conn, backend):
        _ensure_schema(conn, backend, _schema_key(db_path, backend))
        rows = paper_store._rows(conn.execute(paper_store._sql(
            f"SELECT adapter, fields, hint, created_at, updated_at FROM {TABLE}", backend)))
    return rows


def status(db_path: str | None = None, use_cache: bool = True) -> dict:
    """등록 현황 — **원문 없이** 마스킹 힌트·필드 이름·시각만."""
    key = _cache_key(db_path)
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached and time.time() - cached[0] < STATUS_TTL:
            return cached[1]

    encryption = encryption_status()
    adapters: dict[str, dict] = {}
    error = ""
    try:
        rows = _rows(db_path)
    except Exception as exc:      # DB 없음·권한 — 상태 조회가 화면을 막지 않게
        rows = []
        error = "storage_unavailable"
        logger.warning("증권사 키 상태 조회 실패(%s)", type(exc).__name__)

    by_adapter = {str(row.get("adapter") or ""): row for row in rows}
    try:
        import broker
        names = [name for name in broker.ADAPTERS if name != "paper"]
    except Exception:
        names = list(REGISTERABLE) + list(NOT_REGISTERABLE)

    for name in names:
        row = by_adapter.get(name) or {}
        hint: dict = {}
        try:
            hint = json.loads(str(row.get("hint") or "{}"))
        except Exception:
            hint = {}
        registered = bool(row)
        adapters[name] = {
            "adapter": name,
            "registered": registered,
            "fields": [f for f in str(row.get("fields") or "").split(",") if f],
            "hint": hint if registered else {},
            "hint_label": _hint_label(hint) if registered else "",
            "updated_at": row.get("updated_at") if registered else None,
            "registerable": name in REGISTERABLE,
            "note": NOT_REGISTERABLE.get(name, ""),
        }

    payload = {
        "registered_count": sum(1 for row in adapters.values() if row["registered"]),
        "adapters": adapters,
        "encryption": encryption,
        "storage_backend": paper_store.storage_backend(db_path),
        "auth": auth_status(),
        "error": error,
    }
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), payload)
    return payload


def auth_status() -> dict:
    """등록 요청 잠금 상태 — 토큰이 설정돼 있으면 헤더가 필요하다(값은 노출하지 않는다)."""
    token = _clean(os.environ.get("BROKER_CRED_TOKEN"))
    return {
        "mode": "token" if token else "open",
        "header": _AUTH_HEADER,
        "message": ("" if token else
                    "서버에 등록 잠금 토큰(BROKER_CRED_TOKEN)이 없어 누구나 등록·삭제 요청을 보낼 수 있습니다. "
                    "Render 환경변수에 넣으면 X-Broker-Token 헤더가 있어야만 바뀝니다."),
    }


def check_write_token(supplied: str | None) -> None:
    """`BROKER_CRED_TOKEN`이 설정돼 있으면 헤더 값을 대조한다(타이밍 안전 비교)."""
    token = _clean(os.environ.get("BROKER_CRED_TOKEN"))
    if not token:
        return
    import hmac
    if not hmac.compare_digest(_clean(supplied), token):
        raise CredentialsError("broker_token_required",
                              "서버에 등록 토큰이 설정돼 있습니다(Render의 BROKER_CRED_TOKEN). 등록 요청을 보낼 수 없어요.")


# --- 증권사별 등록 안내 -----------------------------------------------------------

GUIDES: dict[str, dict] = {
    "kis": {
        "title": "한국투자증권 · KIS Developers",
        "status": "available",
        "url": "https://apiportal.koreainvestment.com/intro",
        "summary": "모의투자용 앱키를 먼저 발급해 실돈 없이 검증할 수 있어요(REST·WebSocket, 국내·해외).",
        "steps": [
            "한국투자증권 계좌를 열고 KIS Developers(apiportal.koreainvestment.com)에 가입·ID 연결",
            "Open API 서비스 신청 → 앱키(App Key)·앱시크릿(App Secret) 발급",
            "모의투자용과 실전용 앱키는 따로 발급됩니다 — 처음엔 모의투자용으로",
            "계좌번호는 앞 8자리 + 뒤 2자리(예: 12345678-01) 형태로 입력",
        ],
        "notes": [
            "실전 키로는 모의투자 서버에 접속할 수 없고, 그 반대도 안 됩니다.",
            "모의투자는 전체 API 중 일부만 열려 있어요(주문·잔고·시세 위주).",
            "발급한 키는 이 화면에서만 서버로 보내고, 앱·웹에는 저장하지 않습니다.",
        ],
        "env": ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"],
    },
    "toss": {
        "title": "토스증권 · Open API",
        "status": "available",
        "url": "https://corp.tossinvest.com/ko/open-api",
        "summary": "2026-08-13 정식 출시 — 국내·해외 주식을 한 API로 다루고, 키 발급 절차가 짧아요.",
        "steps": [
            "토스증권 계좌 보유 상태에서 PC 웹(WTS) 설정 → Open API에서 약관 동의 후 신청",
            "승인되면 같은 메뉴에서 Client ID·Client Secret 발급(Secret은 발급 직후 한 번만 보임)",
            "허용 IP 관리에 API를 호출할 서버의 공인 IP를 등록 — 목록에 없으면 403으로 막힙니다",
            "계좌번호는 조회·주문에 쓰는 계좌의 번호를 입력",
        ],
        "notes": [
            "Render 무료 서버는 나가는 IP가 고정이 아니어서, 실사용 때는 고정 IP(프록시·유료 플랜)가 필요할 수 있어요.",
            "OAuth 액세스 토큰은 24시간마다 새로 받아야 합니다(연동 코드가 처리).",
            "조회 권한부터 시작하고, 주문 권한은 마지막에 켜세요.",
        ],
        "env": ["TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO"],
    },
    "mirae": {
        "title": "미래에셋증권 · Open API",
        "status": "waiting",
        "url": "",
        "summary": "개인용 Open API가 아직 출시되지 않았습니다 — 지금은 등록할 수 없어요.",
        "steps": [
            "2026-10 보도로 개발 착수는 확인됐지만 출시 시기는 정해지지 않았습니다(업계 관측 연말~2027년 초).",
            "출시되면 이 화면에서 다른 증권사와 같은 방식으로 등록할 수 있게 열어 둡니다.",
        ],
        "notes": [
            "그 전까지는 한국투자증권·토스증권 키로 연동을 시험해 보세요.",
            "미래에셋 HTS를 자동 조작하는 방식은 지원하지 않습니다(불안정·약관 문제).",
        ],
        "env": ["MIRAE_API_KEY", "MIRAE_ACCOUNT_NO"],
    },
}


def summary_for_health() -> dict:
    """`GET /`용 짧은 요약 — 등록 건수·암호화 준비·잠금 방식만(원문·힌트 없음).

    헬스 체크는 어떤 경우에도 200으로 돌아와야 하므로 실패를 값으로 바꾼다.
    """
    try:
        snapshot = status(use_cache=True)
        return {
            "registered_count": snapshot.get("registered_count", 0),
            "adapters": {name: bool(row.get("registered"))
                         for name, row in (snapshot.get("adapters") or {}).items()},
            "encryption": (snapshot.get("encryption") or {}).get("algorithm"),
            "encryption_ready": bool((snapshot.get("encryption") or {}).get("ready")),
            "key_source": (snapshot.get("encryption") or {}).get("key_source"),
            "auth": (snapshot.get("auth") or {}).get("mode"),
            "error": snapshot.get("error") or "",
        }
    except Exception as exc:
        return {"registered_count": 0, "adapters": {}, "encryption_ready": False,
                "error": type(exc).__name__}


def guide_list() -> list[dict]:
    """화면이 그리는 순서(한투 → 토스 → 미래에셋)."""
    out = []
    for name in ("kis", "toss", "mirae"):
        guide = dict(GUIDES[name])
        guide["adapter"] = name
        guide["fields"] = adapter_field_labels(name) if name in REGISTERABLE else []
        guide["registerable"] = name in REGISTERABLE
        out.append(guide)
    return out


def safety_status() -> dict:
    """실주문 안전장치 — 화면 경고창이 서버 값 그대로 보여 준다."""
    import broker

    guard = broker.guard_for(None).snapshot()
    return {
        "real_trading_enabled": bool(guard.get("real_trading_enabled")),
        "dry_run": bool(guard.get("dry_run")),
        "order_max_krw": guard.get("order_max_krw"),
        "daily_max_krw": guard.get("daily_max_krw"),
        "daily_used_krw": guard.get("daily_used_krw"),
        "tracked_orders": guard.get("tracked_orders"),
        "wired": {name: bool(getattr(cls, "implemented", True))
                  for name, cls in broker.ADAPTERS.items() if name != "paper"},
        "kill_switch_env": "REAL_TRADING_ENABLED",
        "dry_run_env": "REAL_DRY_RUN",
        "order_env": "REAL_ORDER_MAX_KRW",
        "daily_env": "REAL_DAILY_MAX_KRW",
    }


def full_status(db_path: str | None = None) -> dict:
    """브로커 요약 + 키 등록 현황 + 안내 + 안전장치 — 앱·웹이 한 번에 읽는 상태.

    어댑터별로 `configured`(환경변수), `registered`(DB 보관), `implemented`(주문 코드 연결)를
    구분해 준다. **어느 값에도 키 원문은 없다.**
    """
    import broker

    try:
        summary = broker.broker_summary()
    except Exception as exc:     # 헬스체크가 죽지 않게
        logger.warning("브로커 요약 실패(%s)", type(exc).__name__)
        summary = {"mode": "paper", "live": False, "real_trading_enabled": False,
                   "dry_run": True, "adapters": [], "guard": {}}
    credentials = status(db_path)
    adapters = []
    for row in summary.get("adapters") or []:
        name = row.get("name") or ""
        cred = (credentials.get("adapters") or {}).get(name) or {}
        merged = dict(row)
        merged["registered"] = bool(cred.get("registered"))
        merged["credential_hint"] = cred.get("hint") or {}
        merged["credential_hint_label"] = cred.get("hint_label") or ""
        merged["credential_updated_at"] = cred.get("updated_at")
        merged["registerable"] = bool(cred.get("registerable"))
        merged["credential_note"] = cred.get("note") or ""
        merged["keys_present"] = bool(row.get("configured") or cred.get("registered"))
        adapters.append(merged)
    selected = next((row for row in adapters if row.get("name") == summary.get("mode")), None)
    return {
        **summary,
        "adapters": adapters,
        "selected": selected,
        "limits": {
            "order_max_krw": (summary.get("guard") or {}).get("order_max_krw"),
            "daily_max_krw": (summary.get("guard") or {}).get("daily_max_krw"),
            "daily_used_krw": (summary.get("guard") or {}).get("daily_used_krw"),
        },
        "credentials": credentials,
        "guides": guide_list(),
        "safety": safety_status(),
        "summary": _summary_text(summary, adapters, credentials),
    }


def _summary_text(summary: dict, adapters: list[dict], credentials: dict) -> str:
    """카드에 한 줄로 쓰는 상태 문장(사실만)."""
    registered = [row for row in adapters if row.get("registered")]
    if not registered:
        head = "키 미등록 — 모의투자(가상 계좌)로만 돌고 있어요"
    else:
        names = ", ".join(row.get("label") or row.get("name") or "" for row in registered)
        head = f"{names} 키 등록됨"
    wired = any((row.get("implemented") for row in adapters if row.get("registered")))
    if not wired:
        head += " · 주문 실행 코드 미연결"
    if not summary.get("real_trading_enabled"):
        head += " · 실주문 킬 스위치 꺼짐"
    if summary.get("dry_run"):
        head += " · 드라이런"
    if not (credentials.get("encryption") or {}).get("ready"):
        head += " · 암호화 키 미설정"
    return head
