"""Official web QR login and locally encrypted shared credentials."""

import asyncio
import json
import os
import re
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from cryptography.fernet import Fernet, InvalidToken

from .client import BiliError, check_response, read_limited

COOKIE_NAMES = {
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
}
PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode/"


def cookie_from_poll(headers: list[str], callback: str) -> str:
    cookies = {}
    try:
        parsed = urlsplit(callback)
        if parsed.scheme == "https" and parsed.hostname in {
            "passport.bilibili.com",
            "www.bilibili.com",
            "account.bilibili.com",
        }:
            cookies.update(
                (key, value)
                for key, value in parse_qsl(parsed.query)
                if key in COOKIE_NAMES
            )
    except ValueError:
        pass
    for header in headers:
        parsed_cookie = SimpleCookie()
        try:
            parsed_cookie.load(header)
        except Exception:
            continue
        cookies.update(
            (key, value.value)
            for key, value in parsed_cookie.items()
            if key in COOKIE_NAMES
        )
    cookies = {
        key: value
        for key, value in cookies.items()
        if value and not re.search(r"[;\r\n]", value)
    }
    if not cookies.get("SESSDATA"):
        raise BiliError("扫码已确认，但没有收到有效登录凭据，请重新扫码。")
    return "; ".join(f"{key}={value}" for key, value in sorted(cookies.items()))


class CredentialVault:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.key = self.root / "account.key"
        self.credentials = self.root / "account.enc"

    def load(self):
        if not self.credentials.exists():
            return ""
        try:
            return (
                Fernet(self.key.read_bytes())
                .decrypt(self.credentials.read_bytes())
                .decode()
            )
        except (OSError, ValueError, InvalidToken):
            raise BiliError("本地登录凭据无法读取，请重新扫码登录。") from None

    def save(self, cookie):
        if not self.key.exists():
            with self.key.open("xb") as output:
                os.chmod(self.key, 0o600)
                output.write(Fernet.generate_key())
        token = Fernet(self.key.read_bytes()).encrypt(cookie.encode())
        temporary = self.root / "account.enc.tmp"
        with temporary.open("wb") as output:
            os.chmod(temporary, 0o600)
            output.write(token)
        os.replace(temporary, self.credentials)

    def clear(self):
        self.credentials.unlink(missing_ok=True)
        self.key.unlink(missing_ok=True)


class QRLogin:
    def __init__(self, client):
        self.client = client

    async def _request(self, action, params=None):
        session = await self.client.get_session()
        try:
            async with (
                self.client.gate,
                session.get(
                    PASSPORT + action, params=params, allow_redirects=False
                ) as response,
            ):
                if response.status != 200:
                    raise BiliError("扫码登录服务暂不可用，请稍后重试。")
                raw = await read_limited(response.content, 65536)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result, response.headers.getall("Set-Cookie", [])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BiliError("无法连接扫码登录服务，请检查网络后重试。") from None

    async def generate(self):
        response, _ = await self._request("generate")
        data = check_response(response)
        parsed = urlsplit(data.get("url", ""))
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"passport.bilibili.com", "account.bilibili.com"}
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", data.get("qrcode_key", ""))
        ):
            raise BiliError("登录二维码数据异常，请稍后重新获取。")
        return data["url"], data["qrcode_key"]

    async def poll(self, key):
        response, headers = await self._request("poll", {"qrcode_key": key})
        data = check_response(response)
        code = data.get("code")
        states = {86101: "waiting", 86090: "scanned", 86038: "expired"}
        if code in states:
            return states[code], ""
        if code == 0:
            return "success", cookie_from_poll(headers, data.get("url", ""))
        raise BiliError("扫码登录未完成，请重新获取二维码。")

    @staticmethod
    def make_image(url, path):
        import qrcode

        image = qrcode.make(url, box_size=9, border=4)
        image.save(str(path))
        return str(path)
