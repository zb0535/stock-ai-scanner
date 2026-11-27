"""무인증 요청 차단 — 모의투자·브로커 API는 유효한 세션 토큰 없이는 401 (보안 회귀 방지).

왜 필요한가
-----------
2026-11-27 이전에는 `X-Paper-User-Token` 헤더가 없으면 서버가 **기본 관리자(user 1 = 현우님 계좌)**로
폴백했다. 그 구멍으로 주소만 아는 누구나 현우님 모의투자 계좌의 상태를 읽고, 매매 실행·계좌
리셋·설정 변경·증권사 키 등록까지 할 수 있었다. 이 테스트는 그 폴백이 되살아나지 않게
**모든 보호 엔드포인트**를 토큰 없이/위조된 토큰으로/만료된 토큰으로 때려 401을 확인하고,
권한 경계(지인 토큰 → /api/broker/* 403)와 정상 로그인 토큰의 200 통과를 함께 고정한다.

확인 항목
1. 보호 엔드포인트 전부: 토큰 없음 → 401 (`paper_token_required`)
2. 위조 서명·형식 오류·만료 토큰 → 401 (`paper_token_invalid`)
3. 지인(비관리자) 토큰은 자기 계좌만 200, /api/broker/*는 403
4. 관리자 토큰은 status·leaderboard·fcm/status·broker/status 200
5. `POST /api/paper/auth`(로그인)와 `GET /`(헬스체크)는 토큰 없이도 열려 있다
6. 401 응답에는 계좌·닉네임·키 같은 값이 실리지 않는다

실행: python -m pytest tests/test_auth_required.py -q
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paper_auth  # noqa: E402
import paper_trading  # noqa: E402

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - httpx 미설치
    TestClient = None

HEADER = "X-Paper-User-Token"

# 토큰 없이 부르면 반드시 401이어야 하는 표면(메서드, 경로, 본문).
PROTECTED = [
    ("GET", "/api/paper/status", None),
    ("GET", "/api/paper/status?market=KR", None),
    ("POST", "/api/paper/configure", {"market": "KR", "initial_cash_krw": 1_000_000}),
    ("POST", "/api/paper/run", {"market": "US", "force": True}),
    ("GET", "/api/paper/leaderboard", None),
    ("GET", "/api/paper/fcm/status", None),
    ("POST", "/api/paper/fcm/register", {"token": "device-x"}),
    ("DELETE", "/api/paper/fcm/register", None),
    ("DELETE", "/api/paper/fcm/register?token=device-x", None),
    ("POST", "/api/paper/fcm/test", {}),
    ("GET", "/api/broker/status", None),
    ("POST", "/api/broker/credentials",
     {"adapter": "kis", "app_key": "k", "app_secret": "s", "account_no": "1",
      "confirm_risk": True}),
    ("DELETE", "/api/broker/credentials/kis", None),
]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def forge_token(payload: dict, *, secret: str, sign: bool = True) -> str:
    """만료·위조 토큰 만들기 — 서명은 맞지만 exp가 지난 토큰, 서명만 틀린 토큰 둘 다."""
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if not sign:
        return f"v1.{body}.{'A' * 43}"
    signature = _b64(hmac.new(secret.encode("utf-8"), f"v1.{body}".encode("ascii"),
                              hashlib.sha256).digest())
    return f"v1.{body}.{signature}"


@unittest.skipIf(TestClient is None, "httpx 미설치")
class AuthRequiredTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "paper.db")
        patcher = patch.dict(os.environ, {
            "HISTORY_DB_PATH": self.db_path,
            "PAPER_AUTO": "0",
            "PAPER_STATUS_PREWARM": "0",
            "PAPER_TOKEN_SECRET": "auth-required-test-secret",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        paper_trading.clear_caches()
        paper_auth.reset_attempts()
        paper_auth._SECRET_CACHE.clear()
        self.addCleanup(paper_trading.clear_caches)
        self.addCleanup(paper_auth.reset_attempts)
        import api
        self.api = api
        # 기본 헤더를 싣지 않는다 — 각 테스트가 직접 토큰을 정한다.
        self.client = TestClient(api.app)

    # --- 토큰 만들기 ---------------------------------------------------------
    def admin_token(self) -> str:
        user = paper_auth.ensure_admin(self.db_path)
        token, _ = paper_auth.sign_token(user, self.db_path)
        return token

    def friend_token(self, nickname: str = "민수", pin: str = "1234") -> str:
        user, _ = paper_auth.authenticate(nickname, pin, self.db_path)
        token, _ = paper_auth.sign_token(user, self.db_path)
        return token

    def call(self, method: str, path: str, body, headers: dict | None = None):
        kwargs = {"headers": headers or {}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, path, **kwargs)

    # --- 1. 토큰 없음 → 401 ---------------------------------------------------
    def test_every_protected_endpoint_is_401_without_token(self):
        for method, path, body in PROTECTED:
            with self.subTest(method=method, path=path):
                response = self.call(method, path, body)
                self.assertEqual(response.status_code, 401, f"{method} {path} → {response.text}")
                detail = response.json().get("detail") or {}
                self.assertEqual(detail.get("code"), "paper_token_required")
                # 기본 관리자 폴백이 되살아나면 계좌 데이터가 실린다 — 그 값이 없어야 한다.
                self.assertNotIn("account", response.text)
                self.assertNotIn("현우", response.text)

    def test_blank_token_header_is_401(self):
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                response = self.client.get("/api/paper/status", headers={HEADER: value})
                self.assertEqual(response.status_code, 401)

    # --- 2. 위조·만료 토큰 → 401 ---------------------------------------------
    def test_forged_and_malformed_tokens_are_401(self):
        now = int(time.time())
        tokens = {
            "잘못된 형식": "not-a-token",
            "서명 불일치": forge_token({"uid": 1, "nick": "현우", "adm": True,
                                       "iat": now, "exp": now + 3600},
                                      secret="다른-비밀"),
            "알고리즘 접두어": "v2.abc.def",
            "만료": forge_token({"uid": 1, "nick": "현우", "adm": True,
                                "iat": now - 7200, "exp": now - 3600},
                               secret="auth-required-test-secret"),
            "uid 없음": forge_token({"nick": "현우", "adm": True,
                                   "iat": now, "exp": now + 3600},
                                  secret="auth-required-test-secret"),
        }
        for label, token in tokens.items():
            for method, path, body in PROTECTED:
                with self.subTest(token=label, method=method, path=path):
                    response = self.call(method, path, body, headers={HEADER: token})
                    self.assertEqual(response.status_code, 401,
                                     f"{label}: {method} {path} → {response.text}")
                    self.assertEqual((response.json().get("detail") or {}).get("code"),
                                     "paper_token_invalid")

    # --- 3. 지인 토큰: 자기 계좌만, 브로커는 403 --------------------------------
    def test_friend_token_cannot_touch_broker_endpoints(self):
        headers = {HEADER: self.friend_token()}
        self.assertEqual(self.client.get("/api/broker/status", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/broker/credentials", headers=headers, json={
            "adapter": "kis", "app_key": "k", "app_secret": "s", "account_no": "1",
            "confirm_risk": True}).status_code, 403)
        self.assertEqual(self.client.delete("/api/broker/credentials/kis",
                                            headers=headers).status_code, 403)

    def test_friend_token_sees_own_account_only(self):
        headers = {HEADER: self.friend_token()}
        body = self.client.get("/api/paper/status", headers=headers).json()
        self.assertEqual(body["user"]["nickname"], "민수")
        self.assertFalse(body["user"]["is_admin"])
        self.assertIsNone(body["broker"])
        # 기본 관리자 계좌(미국 1·한국 2)가 아니라 자기 전용 계좌를 본다.
        self.assertNotIn(body["account"]["id"], (1, 2))

    # --- 4. 정상 토큰은 200 ---------------------------------------------------
    def test_admin_token_passes_everywhere(self):
        headers = {HEADER: self.admin_token()}
        for method, path, body in [("GET", "/api/paper/status", None),
                                   ("GET", "/api/paper/status?market=KR", None),
                                   ("GET", "/api/paper/leaderboard", None),
                                   ("GET", "/api/paper/fcm/status", None),
                                   ("GET", "/api/broker/status", None)]:
            with self.subTest(method=method, path=path):
                response = self.call(method, path, body, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get("/api/paper/status", headers=headers).json()
        self.assertTrue(status["user"]["is_admin"])
        self.assertIsNotNone(status["broker"])
        board = self.client.get("/api/paper/leaderboard", headers=headers).json()
        self.assertEqual(board["me"], paper_auth.ensure_admin(self.db_path)["id"])

    def test_friend_token_passes_paper_endpoints(self):
        headers = {HEADER: self.friend_token()}
        for method, path, body in [("GET", "/api/paper/status", None),
                                   ("GET", "/api/paper/leaderboard", None),
                                   ("GET", "/api/paper/fcm/status", None)]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path, body, headers=headers).status_code, 200)

    # --- 5. 로그인·헬스체크는 열려 있다 ------------------------------------------
    def test_login_and_health_are_open(self):
        response = self.client.post("/api/paper/auth", json={"nickname": "새친구", "pin": "1234"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["token"])
        health = self.client.get("/")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

    # --- 6. 헬스체크에 내부 운영 정보·키 재료가 없다 ------------------------------
    def test_health_hides_credentials_and_internal_paths(self):
        text = self.client.get("/").text
        body = json.loads(text)
        self.assertEqual(set(body["broker"]) & {"guard", "adapters", "state_path"}, set())
        self.assertEqual(set(body["broker_credentials"]) & {"adapters", "encryption",
                                                            "key_source", "auth"}, set())
        self.assertEqual(set(body["fcm"]) & {"project_id", "channel_id", "source", "reason"}, set())
        self.assertNotIn("auth-required-test-secret", text)


if __name__ == "__main__":
    unittest.main()
