"""증권사 API 키 보관소(암호화) 테스트.

다루는 것
1. 저장은 **암호문으로만** — DB 파일(바이트)에 키 원문이 없다
2. 조회는 **마스킹 힌트만** — 상태 응답·로그 어디에도 원문이 없다
3. 삭제(연동 해제)
4. 키 재료가 없으면 저장을 **거부**한다(평문 폴백 없음)
5. 키 재료가 바뀌면 복호화 실패를 안전하게 알린다(원문 없이)
6. 입력 검증 — 빠진 값·줄바꿈·미출시 증권사(미래에셋)
7. API: confirm_risk 필수, 등록/삭제/상태, 선택적 등록 토큰(X-Broker-Token)
8. `/api/paper/status`·`/api/broker/status` 응답에 키 원문이 없다

실행: python -m unittest tests.test_broker_credentials -v
"""
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import broker_credentials  # noqa: E402
import paper_store  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

APP_KEY = "PSxKIS-APPKEY-9f3a7c21d4e8"
APP_SECRET = "SECRET-8b71f0c2aa55e1d9-KIS"
ACCOUNT_NO = "50123456-01"

CLEAN_KEYS = ("BROKER_CRED_KEY", "BROKER_CRED_TOKEN", "DATABASE_URL",
              "FIREBASE_SERVICE_ACCOUNT_JSON", "GOOGLE_API_KEY", "GEMINI_API_KEY",
              "FINN_API_KEY", "FINN_KEY", "FINNHUB_API_KEY")


def _clean_env(**extra):
    """암호화 재료·DB 관련 환경변수를 지운 상태(patch.dict용)."""
    env = {name: "" for name in CLEAN_KEYS}
    env.update(extra)
    return env


class CredentialsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.env = patch.dict(os.environ, _clean_env(
            BROKER_CRED_KEY="test-passphrase-for-unit-tests-1234",
            HISTORY_DB_PATH=self.db_path), clear=False)
        self.env.start()
        broker_credentials._invalidate()

    def tearDown(self):
        self.env.stop()
        broker_credentials._invalidate()
        self.tmp.cleanup()

    def save(self, adapter="kis", app_key=APP_KEY, app_secret=APP_SECRET,
             account_no=ACCOUNT_NO, db_path=None):
        return broker_credentials.save(
            adapter,
            {"app_key": app_key, "app_secret": app_secret, "account_no": account_no},
            db_path=db_path)

    def test_save_stores_ciphertext_only(self):
        entry = self.save()
        self.assertTrue(entry["registered"])
        self.assertEqual(entry["adapter"], "kis")
        self.assertIn("app_key", entry["hint"])
        self.assertTrue(entry["hint"]["app_key"].startswith("••••"))

        # DB 파일 바이트 어디에도 원문이 없다(암호문만).
        with open(self.db_path, "rb") as handle:
            blob = handle.read()
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret.encode("utf-8"), blob)

    def test_hint_and_status_never_expose_values(self):
        self.save()
        snapshot = broker_credentials.status(use_cache=False)
        self.assertEqual(snapshot["registered_count"], 1)
        row = snapshot["adapters"]["kis"]
        self.assertTrue(row["registered"])
        self.assertEqual(row["fields"], ["account_no", "app_key", "app_secret"])
        self.assertEqual(row["hint"]["app_key"][-4:], APP_KEY[-4:])
        self.assertEqual(row["hint_label"].count("••••"), 3)
        dumped = json.dumps(snapshot, ensure_ascii=False)
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret, dumped)
        self.assertNotIn("cipher", dumped)

    def test_load_values_roundtrip(self):
        self.save()
        values = broker_credentials.load_values("kis")
        self.assertEqual(values["KIS_APP_KEY"], APP_KEY)
        self.assertEqual(values["KIS_APP_SECRET"], APP_SECRET)
        self.assertEqual(values["KIS_ACCOUNT_NO"], ACCOUNT_NO)

    def test_delete_removes_registration(self):
        self.save()
        self.assertEqual(broker_credentials.delete("kis"), 1)
        self.assertEqual(broker_credentials.status(use_cache=False)["registered_count"], 0)
        self.assertEqual(broker_credentials.delete("kis"), 0)
        self.assertEqual(broker_credentials.load_values("kis"), {})

    def test_overwrite_keeps_one_row(self):
        self.save()
        self.save(app_key="NEW-APPKEY-0000aaaa", app_secret="NEW-SECRET-1111bbbb")
        snapshot = broker_credentials.status(use_cache=False)
        self.assertEqual(snapshot["registered_count"], 1)
        self.assertEqual(broker_credentials.load_values("kis")["KIS_APP_KEY"], "NEW-APPKEY-0000aaaa")

    def test_refuses_to_save_without_key_material(self):
        os.environ.pop("BROKER_CRED_KEY", None)
        broker_credentials._invalidate()
        with self.assertRaises(broker_credentials.KeyUnavailable):
            self.save()
        # 평문으로 떨어지지 않는다 — 아무것도 저장되지 않는다.
        self.assertEqual(broker_credentials.status(use_cache=False)["registered_count"], 0)

    def test_decrypt_failure_is_safe(self):
        self.save()
        os.environ["BROKER_CRED_KEY"] = "a-completely-different-passphrase"
        broker_credentials._invalidate()
        with self.assertRaises(broker_credentials.DecryptFailed) as ctx:
            broker_credentials.load_values("kis")
        self.assertNotIn(APP_KEY, str(ctx.exception))
        # 상태 조회는 살아 있고, 등록 사실은 그대로 보인다(다시 등록하라고 안내).
        self.assertTrue(broker_credentials.status(use_cache=False)["adapters"]["kis"]["registered"])

    def test_validation(self):
        with self.assertRaises(broker_credentials.CredentialsError):
            self.save(app_key="")
        with self.assertRaises(broker_credentials.CredentialsError):
            self.save(account_no="5012\r\n3456-01")
        with self.assertRaises(broker_credentials.NotRegisterable):
            self.save(adapter="mirae", app_key="k", app_secret="s", account_no="1")
        with self.assertRaises(broker_credentials.UnknownAdapter):
            self.save(adapter="nope", app_key="k", app_secret="s", account_no="1")

    def test_toss_field_names(self):
        entry = self.save(adapter="toss", app_key="client-id-1234", app_secret="client-secret-5678")
        self.assertEqual(entry["fields"], ["account_no", "app_key", "app_secret"])
        values = broker_credentials.load_values("toss")
        self.assertEqual(values["TOSS_CLIENT_ID"], "client-id-1234")
        self.assertEqual(values["TOSS_CLIENT_SECRET"], "client-secret-5678")

    def test_logs_do_not_contain_secrets(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("broker_credentials")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            self.save()
            broker_credentials.delete("kis")
        finally:
            logger.removeHandler(handler)
        text = stream.getvalue()
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret, text)

    def test_full_status_is_masked_and_has_guides(self):
        self.save()
        payload = broker_credentials.full_status(db_path=None)
        dumped = json.dumps(payload, ensure_ascii=False)
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret, dumped)
        kis = next(row for row in payload["adapters"] if row["name"] == "kis")
        self.assertTrue(kis["registered"])
        self.assertTrue(kis["credential_hint_label"])
        self.assertEqual([guide["adapter"] for guide in payload["guides"]], ["kis", "toss", "mirae"])
        mirae = next(guide for guide in payload["guides"] if guide["adapter"] == "mirae")
        self.assertFalse(mirae["registerable"])
        self.assertFalse(payload["safety"]["real_trading_enabled"])
        self.assertTrue(payload["safety"]["dry_run"])


@unittest.skipIf(TestClient is None, "fastapi TestClient 없음")
class CredentialsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "api.db")
        cls.env = patch.dict(os.environ, _clean_env(
            BROKER_CRED_KEY="api-test-passphrase-9988776655",
            HISTORY_DB_PATH=cls.db_path,
            PAPER_AUTO_ENABLED="0",
            SP500_PREWARM="0",
            KOSPI200_PREWARM="0",
            PREWARM_ENABLED="0"), clear=False)
        cls.env.start()
        import api  # noqa: PLC0415 (환경변수를 세운 뒤 임포트)

        cls.api = api
        cls.client = TestClient(api.app)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        broker_credentials._invalidate()
        cls.tmp.cleanup()

    def setUp(self):
        broker_credentials.delete("kis")
        broker_credentials.delete("toss")

    def test_register_requires_confirm_risk(self):
        res = self.client.post("/api/broker/credentials", json={
            "adapter": "kis", "app_key": APP_KEY, "app_secret": APP_SECRET,
            "account_no": ACCOUNT_NO})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["detail"]["code"], "confirm_risk_required")
        self.assertEqual(broker_credentials.status(use_cache=False)["registered_count"], 0)

    def test_register_then_status_then_delete(self):
        res = self.client.post("/api/broker/credentials", json={
            "adapter": "kis", "app_key": APP_KEY, "app_secret": APP_SECRET,
            "account_no": ACCOUNT_NO, "confirm_risk": True})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret, res.text)

        status = self.client.get("/api/broker/status")
        self.assertEqual(status.status_code, 200)
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret, status.text)
        kis = next(row for row in status.json()["adapters"] if row["name"] == "kis")
        self.assertTrue(kis["registered"])

        # 모의투자 상태에도 브로커 블록이 실리고 원문은 없다.
        paper = self.client.get("/api/paper/status?market=US")
        self.assertEqual(paper.status_code, 200)
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret, paper.text)
        self.assertIn("broker", paper.json())

        deleted = self.client.delete("/api/broker/credentials/kis")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["removed"], 1)
        self.assertEqual(broker_credentials.status(use_cache=False)["registered_count"], 0)

    def test_token_lock_blocks_writes(self):
        with patch.dict(os.environ, {"BROKER_CRED_TOKEN": "s3cret-lock"}):
            res = self.client.post("/api/broker/credentials", json={
                "adapter": "kis", "app_key": APP_KEY, "app_secret": APP_SECRET,
                "account_no": ACCOUNT_NO, "confirm_risk": True})
            self.assertEqual(res.status_code, 401)
            self.assertEqual(res.json()["detail"]["code"], "broker_token_required")
            self.assertEqual(broker_credentials.status(use_cache=False)["registered_count"], 0)

            ok = self.client.post("/api/broker/credentials", json={
                "adapter": "kis", "app_key": APP_KEY, "app_secret": APP_SECRET,
                "account_no": ACCOUNT_NO, "confirm_risk": True},
                headers={"X-Broker-Token": "s3cret-lock"})
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(ok.json()["credentials"]["auth"]["mode"] if "credentials" in ok.json()
                             else broker_credentials.auth_status()["mode"], "token")

    def test_health_has_no_key_material(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("broker", body)
        self.assertIn("broker_credentials", body)
        for secret in (APP_KEY, APP_SECRET, ACCOUNT_NO):
            self.assertNotIn(secret, res.text)
        self.assertFalse(body["broker_credentials"]["adapters"].get("kis", False))


if __name__ == "__main__":
    unittest.main()
