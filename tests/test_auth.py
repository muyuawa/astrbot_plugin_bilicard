import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_bilicard.auth import CredentialVault, QRLogin, cookie_from_poll
from astrbot_plugin_bilicard.client import BiliError


class CookieTests(unittest.TestCase):
    def test_set_cookie_headers_take_precedence_and_exclude_other_fields(self):
        cookie = cookie_from_poll(
            [
                "SESSDATA=secret%2Cvalue; Domain=.bilibili.com; Path=/; HttpOnly",
                "bili_jct=csrf; Path=/",
                "tracking=nope; Path=/",
            ],
            "https://passport.bilibili.com/?SESSDATA=older&DedeUserID=123",
        )
        self.assertIn("SESSDATA=secret%2Cvalue", cookie)
        self.assertIn("DedeUserID=123", cookie)
        self.assertNotIn("older", cookie)
        self.assertNotIn("tracking", cookie)
        self.assertNotIn("Domain", cookie)

    def test_callback_query_supported_without_set_cookie(self):
        cookie = cookie_from_poll(
            [],
            "https://passport.bilibili.com/x/passport-login/web/crossDomain?SESSDATA=token%252Cvalue&bili_jct=test",
        )
        self.assertIn("SESSDATA=token%2Cvalue", cookie)

    def test_missing_session_does_not_claim_login_success(self):
        for url in ["", "https://evil.test/?SESSDATA=bad"]:
            with self.assertRaises(BiliError):
                cookie_from_poll([], url)

    def test_credentials_encrypted_on_disk_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = CredentialVault(directory)
            self.assertEqual(vault.load(), "")
            vault.save("SESSDATA=private-token; bili_jct=csrf")
            self.assertEqual(
                CredentialVault(directory).load(),
                "SESSDATA=private-token; bili_jct=csrf",
            )
            for path in Path(directory).iterdir():
                self.assertNotIn(b"private-token", path.read_bytes())
            vault.clear()
            self.assertEqual(vault.load(), "")


class FakeQR(QRLogin):
    def __init__(self, response, headers=None):
        super().__init__(None)
        self.response = response
        self.headers = headers or []

    async def _request(self, action, params=None):
        return self.response, self.headers


class QRTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_wait_confirm_expiry_and_success(self):
        for code, expected in [
            (86101, "waiting"),
            (86090, "scanned"),
            (86038, "expired"),
        ]:
            qr = FakeQR({"code": 0, "data": {"code": code}})
            self.assertEqual(await qr.poll("test-key"), (expected, ""))
        qr = FakeQR({"code": 0, "data": {"code": 0}}, ["SESSDATA=secret; Path=/"])
        self.assertEqual(await qr.poll("test-key"), ("success", "SESSDATA=secret"))

    async def test_generate_checks_qr_url_origin(self):
        url = (
            "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrsource=main_web"
        )
        qr = FakeQR({"code": 0, "data": {"url": url, "qrcode_key": "a" * 32}})
        self.assertEqual(await qr.generate(), (url, "a" * 32))
        qr = FakeQR(
            {
                "code": 0,
                "data": {"url": "https://evil.test/scan", "qrcode_key": "a" * 32},
            }
        )
        with self.assertRaises(BiliError):
            await qr.generate()
