"""모의투자 닉네임 + 4자리 PIN 인증 — 사용자 행, PIN 해시(PBKDF2), 서명 세션 토큰.

설계 원칙
---------
- **PIN은 절대 평문으로 저장하지 않는다**: PBKDF2-HMAC-SHA256(랜덤 salt, 기본 12만 회)으로
  해시해 `paper_users.pin_hash`에만 둔다. 비교는 `hmac.compare_digest`(타이밍 공격 방지).
- **세션은 DB에 저장하지 않는다**: HMAC-SHA256 서명 토큰(만료 30일)을 발급하고, 서버는 서명만
  검증한다. 서명 키는 DB의 `paper_settings`에 한 번 만들어 보관하므로 재배포·재시작에도
  발급한 토큰이 계속 유효하다(환경변수 `PAPER_TOKEN_SECRET`이 있으면 그것을 우선한다).
- **기본 관리자(user 1)**: 기존 현우님 계좌(id 1=US, 2=KR)의 소유자다. 닉네임은 `현우`
  (`PAPER_ADMIN_NICKNAME`으로 바꿀 수 있다). `ADMIN_PIN`이 있으면 그 PIN으로 바로 잠그고,
  없으면 첫 로그인에서 입력한 4자리 PIN이 그 계정의 PIN이 된다(기존 계좌를 그대로 이어받는다).
- **지인 계정**: 처음 보는 닉네임 + 4자리 PIN이면 유저를 만들고 전용 미국·한국 계좌(각 100만 원)를
  자동 생성한다. 이후 같은 닉네임은 PIN이 맞아야 토큰이 나온다.
- **브루트포스 방어**: 같은 닉네임에 PIN을 10분 안에 10번 틀리면 그 닉네임의 로그인을 10분 잠근다.
  (성공하면 실패 기록을 지운다. 프로세스 메모리 기준 — 재시작하면 초기화된다.)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any

import paper_store

logger = logging.getLogger("paper_auth")

TABLE_USERS = paper_store.TABLE_USERS
TABLE_SETTINGS = paper_store.TABLE_SETTINGS

PIN_LENGTH = 4
PIN_ITERATIONS = 120_000
TOKEN_TTL_SEC = 30 * 24 * 3600          # 30일
TOKEN_VERSION = "v1"
HEADER_NAME = "X-Paper-User-Token"
DEFAULT_ADMIN_NICKNAME = os.environ.get("PAPER_ADMIN_NICKNAME", "현우").strip() or "현우"

MAX_FAILS = 10
FAIL_WINDOW_SEC = 600
LOCK_SEC = 600

_NICKNAME_RE = re.compile(r"^[\w가-힣·.\-]{1,20}$")

_USERS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_USERS} (
    id            INTEGER PRIMARY KEY,
    nickname      TEXT    NOT NULL,
    nickname_key  TEXT    NOT NULL,
    pin_hash      TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS {TABLE_SETTINGS} (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

_USERS_READY: set[str] = set()
_USERS_LOCK = threading.Lock()
_ATTEMPTS: dict[str, list[float]] = {}
_ATTEMPT_LOCK = threading.Lock()
_SECRET_CACHE: dict[str, str] = {}
_ADMIN_CACHE: dict[str, dict] = {}


class AuthError(Exception):
    """인증 실패 — `code`를 HTTP 상태로 옮긴다(비밀번호·PIN 원문은 메시지에 넣지 않는다)."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or {
            "nickname_invalid": "닉네임은 1~20자의 한글·영문·숫자로 입력해 주세요.",
            "pin_invalid": "PIN은 4자리 숫자입니다.",
            "pin_mismatch": "PIN이 맞지 않습니다.",
            "too_many_attempts": "PIN을 여러 번 틀렸습니다. 10분 뒤 다시 시도해 주세요.",
            "user_not_found": "닉네임을 찾지 못했습니다.",
            "auth_storage_unavailable": "사용자 저장소에 연결하지 못했습니다.",
        }.get(code, "로그인하지 못했습니다.")
        super().__init__(self.message)


# --- 검증·해시 ---------------------------------------------------------------


def normalize_nickname(value: Any) -> str:
    """닉네임 정규화(앞뒤·연속 공백 정리). 형식이 틀리면 `AuthError("nickname_invalid")`."""
    nick = " ".join(str(value or "").split())
    if not nick or len(nick) > 20 or not _NICKNAME_RE.match(nick):
        raise AuthError("nickname_invalid")
    return nick


def nickname_key(value: Any) -> str:
    """대소문자·유니코드 정규화 키 — `Hyeonu`와 `hyeonu`를 같은 닉네임으로 본다."""
    import unicodedata

    return unicodedata.normalize("NFKC", " ".join(str(value or "").split())).casefold()


def normalize_pin(value: Any) -> str:
    """4자리 숫자 PIN만 통과. 아니면 `AuthError("pin_invalid")`."""
    pin = str(value or "").strip()
    if len(pin) != PIN_LENGTH or not pin.isdigit():
        raise AuthError("pin_invalid")
    return pin


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_pin(pin: str, salt: bytes | None = None, iterations: int = PIN_ITERATIONS) -> str:
    """`pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>` — salt는 매번 새로 만든다."""
    pin = normalize_pin(pin)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_pin(pin: str, stored: str | None) -> bool:
    """저장된 해시와 PIN을 상수 시간으로 비교한다. 해시가 없거나 깨졌으면 False."""
    if not stored:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = str(stored).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", str(pin or "").encode("utf-8"), salt,
                                    int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# --- 스키마 -------------------------------------------------------------------


def _ensure_schema(conn, backend: str, key: str) -> None:
    if key in _USERS_READY:
        return
    with _USERS_LOCK:
        if key in _USERS_READY:
            return
        for statement in paper_store._split_statements(_USERS_SCHEMA):
            conn.execute(statement)
        try:
            if backend == "postgres":
                conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS paper_users_nickname_key_idx"
                             f" ON {TABLE_USERS} (nickname_key)")
            else:
                conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS paper_users_nickname_key_idx"
                             f" ON {TABLE_USERS} (nickname_key)")
        except Exception as exc:  # 이미 같은 유니크가 있으면 그대로 쓴다
            logger.warning("사용자 닉네임 유니크 인덱스 확인 실패: %s: %s",
                           type(exc).__name__, str(exc)[:120])
        conn.commit()
        _USERS_READY.add(key)


@contextmanager
def _connection(db_path: str | None = None, write: bool = False):
    """paper_store와 같은 저장소 규칙(Postgres/SQLite)을 쓰는 연결 + 사용자 스키마 보장."""
    with paper_store._connection(db_path, write=write) as (conn, backend):
        _ensure_schema(conn, backend, paper_store.database_label(db_path))
        yield conn, backend


def _user_row_to_dict(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": int(row.get("id") or 0),
        "nickname": str(row.get("nickname") or ""),
        "is_admin": bool(row.get("is_admin")),
        "has_pin": bool(str(row.get("pin_hash") or "").strip()),
        "created_at": row.get("created_at"),
        "last_login_at": row.get("last_login_at"),
        "_pin_hash": row.get("pin_hash"),
    }


def public_user(user: dict | None) -> dict:
    """응답에 넣는 사용자 정보 — PIN 해시는 절대 포함하지 않는다."""
    if not user:
        return {}
    return {"id": int(user.get("id") or 0), "nickname": user.get("nickname") or "",
            "is_admin": bool(user.get("is_admin"))}


# --- 사용자 CRUD --------------------------------------------------------------


def get_user(user_id: int, db_path: str | None = None) -> dict | None:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    with _connection(db_path) as (conn, backend):
        row = paper_store._row(conn.execute(paper_store._sql(
            f"SELECT * FROM {TABLE_USERS} WHERE id = ?", backend), (uid,)))
    return _user_row_to_dict(row)


def get_user_by_nickname(nickname: str, db_path: str | None = None) -> dict | None:
    key = nickname_key(nickname)
    if not key:
        return None
    with _connection(db_path) as (conn, backend):
        row = paper_store._row(conn.execute(paper_store._sql(
            f"SELECT * FROM {TABLE_USERS} WHERE nickname_key = ?", backend), (key,)))
    return _user_row_to_dict(row)


def list_users(db_path: str | None = None) -> list[dict]:
    """가입한 모든 유저(관리자 먼저, 그다음 가입 순) — 닉네임·관리자 여부만."""
    with _connection(db_path) as (conn, backend):
        rows = paper_store._rows(conn.execute(paper_store._sql(
            f"SELECT * FROM {TABLE_USERS} ORDER BY is_admin DESC, id", backend)))
    return [_user_row_to_dict(row) for row in rows]


def _insert_user(conn, backend: str, nickname: str, pin_hash: str | None,
                 is_admin: bool) -> dict:
    row = paper_store._row(conn.execute(paper_store._sql(
        f"SELECT COALESCE(MAX(id), 0) AS m FROM {TABLE_USERS}", backend)))
    uid = int((row or {}).get("m") or 0) + 1
    stamp = paper_store._now()
    conn.execute(paper_store._sql(
        f"INSERT INTO {TABLE_USERS} (id, nickname, nickname_key, pin_hash, is_admin,"
        " created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?)", backend),
        (uid, nickname, nickname_key(nickname), pin_hash, 1 if is_admin else 0, stamp, None))
    return {"id": uid, "nickname": nickname, "is_admin": bool(is_admin),
            "has_pin": bool(pin_hash), "created_at": stamp, "last_login_at": None,
            "_pin_hash": pin_hash}


def create_user(nickname: str, pin: str, db_path: str | None = None,
                is_admin: bool = False) -> dict:
    """새 유저 + 전용 미국·한국 계좌(각 100만 원)를 만든다."""
    nick = normalize_nickname(nickname)
    pin_hash = hash_pin(normalize_pin(pin))
    with _connection(db_path, write=True) as (conn, backend):
        existing = paper_store._row(conn.execute(paper_store._sql(
            f"SELECT id FROM {TABLE_USERS} WHERE nickname_key = ?", backend),
            (nickname_key(nick),)))
        if existing:
            raise AuthError("nickname_taken", "이미 사용 중인 닉네임입니다.")
        user = _insert_user(conn, backend, nick, pin_hash, is_admin)
    paper_store.ensure_accounts(db_path, user["id"])  # 전용 계좌 두 개
    logger.info("모의투자 유저 생성: #%d %s (관리자=%s)", user["id"], nick, is_admin)
    return user


def set_user_pin(user_id: int, pin: str, db_path: str | None = None) -> dict | None:
    pin_hash = hash_pin(normalize_pin(pin))
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(paper_store._sql(
            f"UPDATE {TABLE_USERS} SET pin_hash = ? WHERE id = ?", backend),
            (pin_hash, int(user_id)))
    return get_user(user_id, db_path)


def _touch_login(user_id: int, db_path: str | None = None) -> None:
    with _connection(db_path, write=True) as (conn, backend):
        conn.execute(paper_store._sql(
            f"UPDATE {TABLE_USERS} SET last_login_at = ? WHERE id = ?", backend),
            (paper_store._now(), int(user_id)))


def ensure_admin(db_path: str | None = None) -> dict:
    """기본 관리자(user 1, 닉네임 `현우`)를 보장한다 — 기존 계좌 1·2의 소유자.

    한 프로세스에서 한 번 확인하면 메모리에 둔다(토큰 없는 구 클라이언트가 요청할 때마다
    원격 DB 연결을 열지 않게 하기 위해서다). 관리자 행은 삭제·변경 기능이 없다.
    """
    key = paper_store.database_label(db_path)
    cached = _ADMIN_CACHE.get(key)
    if cached:
        return dict(cached)
    nick = DEFAULT_ADMIN_NICKNAME
    user = get_user(1, db_path)
    if not user or not user.get("nickname"):
        user = get_user_by_nickname(nick, db_path)
    if not user:
        admin_pin = (os.environ.get("ADMIN_PIN") or "").strip()
        pin_hash = None
        if len(admin_pin) == PIN_LENGTH and admin_pin.isdigit():
            pin_hash = hash_pin(admin_pin)
        with _connection(db_path, write=True) as (conn, backend):
            if not paper_store._row(conn.execute(paper_store._sql(
                    f"SELECT id FROM {TABLE_USERS} WHERE id = 1", backend))):
                _insert_user(conn, backend, nick, pin_hash, is_admin=True)
            row = paper_store._row(conn.execute(paper_store._sql(
                f"SELECT * FROM {TABLE_USERS} WHERE id = 1", backend)))
            user = _user_row_to_dict(row) or {}
        paper_store.ensure_accounts(db_path, 1)   # 기존 계좌 1·2가 그대로 관리자 소유
        logger.info("모의투자 관리자 계정 준비: #1 %s", user.get("nickname") or nick)
    _ADMIN_CACHE[key] = dict(user)
    return user


# --- 브루트포스 방어 ----------------------------------------------------------


def _attempt_state(key: str, now: float | None = None) -> tuple[int, float | None]:
    current = time.time() if now is None else now
    with _ATTEMPT_LOCK:
        stamps = [t for t in _ATTEMPTS.get(key, []) if current - t < FAIL_WINDOW_SEC]
        _ATTEMPTS[key] = stamps
        locked_until = None
        if len(stamps) >= MAX_FAILS:
            locked_until = stamps[-MAX_FAILS] + LOCK_SEC
            if locked_until <= current:
                _ATTEMPTS[key] = []
                stamps = []
                locked_until = None
        return len(stamps), locked_until


def _record_failure(key: str) -> None:
    with _ATTEMPT_LOCK:
        _ATTEMPTS.setdefault(key, []).append(time.time())


def reset_attempts() -> None:
    """테스트·운영자용 — 로그인 실패 기록을 지운다."""
    with _ATTEMPT_LOCK:
        _ATTEMPTS.clear()


# --- 로그인 -------------------------------------------------------------------


def authenticate(nickname: str, pin: str, db_path: str | None = None) -> tuple[dict, bool]:
    """`(user, created)` — 새 닉네임이면 계정·계좌를 만들고 created=True.

    - 기존 닉네임: PIN 검증 실패 시 `AuthError("pin_mismatch")`.
    - 관리자(user 1)에 PIN이 아직 없으면 이번 PIN을 그 계정의 PIN으로 설정한다(첫 설정).
    """
    nick = normalize_nickname(nickname)
    code = normalize_pin(pin)
    ensure_admin(db_path)
    key = nickname_key(nick)
    fails, locked_until = _attempt_state(key)
    if locked_until and locked_until > time.time():
        raise AuthError("too_many_attempts")

    user = get_user_by_nickname(nick, db_path)
    if user is None:
        created = create_user(nick, code, db_path=db_path)
        _touch_login(created["id"], db_path)
        return get_user(created["id"], db_path) or created, True

    if not user.get("_pin_hash"):
        # PIN 없는 관리자(또는 예전에 만들어진 행) — 이번 입력을 그 계정의 PIN으로 잠근다.
        set_user_pin(user["id"], code, db_path)
        _touch_login(user["id"], db_path)
        return get_user(user["id"], db_path) or user, False

    if not verify_pin(code, user.get("_pin_hash")):
        _record_failure(key)
        raise AuthError("pin_mismatch")

    with _ATTEMPT_LOCK:
        _ATTEMPTS.pop(key, None)
    _touch_login(user["id"], db_path)
    return get_user(user["id"], db_path) or user, False


# --- 세션 토큰 ----------------------------------------------------------------


def _secret(db_path: str | None = None) -> str:
    """토큰 서명 키 — 환경변수 우선, 없으면 DB(`paper_settings`)에 한 번 만들어 보관."""
    env = (os.environ.get("PAPER_TOKEN_SECRET") or "").strip()
    if env:
        return env
    key = paper_store.database_label(db_path)
    cached = _SECRET_CACHE.get(key)
    if cached:
        return cached
    with _connection(db_path, write=True) as (conn, backend):
        row = paper_store._row(conn.execute(paper_store._sql(
            f"SELECT value FROM {TABLE_SETTINGS} WHERE key = ?", backend), ("paper_token_secret",)))
        value = str((row or {}).get("value") or "").strip()
        if not value:
            value = secrets.token_hex(32)
            conn.execute(paper_store._sql(
                f"INSERT INTO {TABLE_SETTINGS} (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO NOTHING", backend),
                ("paper_token_secret", value, paper_store._now()))
            row = paper_store._row(conn.execute(paper_store._sql(
                f"SELECT value FROM {TABLE_SETTINGS} WHERE key = ?", backend),
                ("paper_token_secret",)))
            value = str((row or {}).get("value") or value)
    _SECRET_CACHE[key] = value
    return value


def sign_token(user: dict, db_path: str | None = None,
               ttl_sec: int = TOKEN_TTL_SEC) -> tuple[str, int]:
    """`(token, exp)` — `v1.<payload>.<sig>` (payload는 base64url JSON)."""
    now = int(time.time())
    exp = now + max(60, int(ttl_sec))
    payload = {"uid": int(user["id"]), "nick": user.get("nickname") or "",
               "adm": bool(user.get("is_admin")), "iat": now, "exp": exp}
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{TOKEN_VERSION}.{body}".encode("ascii")
    signature = _b64(hmac.new(_secret(db_path).encode("utf-8"), signing_input,
                              hashlib.sha256).digest())
    return f"{TOKEN_VERSION}.{body}.{signature}", exp


def verify_token(token: str, db_path: str | None = None) -> dict | None:
    """서명·만료를 검증해 `{user_id, nickname, is_admin, exp}`를 돌려준다. 틀리면 None."""
    raw = str(token or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return None
    expected = _b64(hmac.new(_secret(db_path).encode("utf-8"),
                             f"{parts[0]}.{parts[1]}".encode("ascii"),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(expected, parts[2]):
        return None
    try:
        payload = json.loads(_unb64(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        if int(payload.get("exp") or 0) <= int(time.time()):
            return None
        return {"user_id": int(payload["uid"]), "nickname": str(payload.get("nick") or ""),
                "is_admin": bool(payload.get("adm")), "exp": int(payload.get("exp") or 0)}
    except (KeyError, TypeError, ValueError):
        return None
