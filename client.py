"""Bounded, asynchronous read-only Bilibili API access."""

import asyncio
import base64
import hashlib
import json
import re
import time
from collections import OrderedDict
from urllib.parse import urlencode, urljoin, urlsplit

import aiohttp

from .parser import SHORT_HOSTS, VIDEO_HOSTS, video_id_from_url

API = "https://api.bilibili.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
MIXIN = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


class BiliError(Exception):
    """A safe user-facing error; never includes request credentials."""


async def read_limited(stream, limit):
    data = bytearray()
    async for chunk in stream.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > limit:
            raise BiliError("B 站返回的数据过大，请稍后重试。")
    return bytes(data)


def allowed_redirect(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in VIDEO_HOSTS | SHORT_HOSTS
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 80, 443)
        )
    except ValueError:
        return False


def allowed_image(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and (host == "hdslb.com" or host.endswith(".hdslb.com"))
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def check_response(response: dict) -> dict:
    code = response.get("code")
    if code == 0:
        return response.get("data") or {}
    if code in (-404, 62002, 62004, 62012):
        raise BiliError("视频不存在、已删除、审核中或暂不可见。")
    if code in (-101, -403, -352, -412, -509):
        raise BiliError("B 站访问受限，请让管理员私聊发送 /bili 登录 扫码后重试。")
    raise BiliError(f"B 站接口暂不可用（错误码 {code}），请稍后重试。")


class BiliClient:
    def __init__(self, cookie="", timeout=12):
        self.cookie = cookie
        self.timeout = max(5, min(30, int(timeout)))
        self.session = None
        self.gate = asyncio.Semaphore(6)
        self.cache = OrderedDict()

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self.session

    def set_cookie(self, cookie):
        self.cookie = cookie
        self.cache.clear()

    def _cached(self, key):
        item = self.cache.get(key)
        if item and item[0] > time.monotonic():
            self.cache.move_to_end(key)
            return item[1]
        self.cache.pop(key, None)
        return None

    def _remember(self, key, value, ttl=120):
        self.cache[key] = (time.monotonic() + ttl, value)
        self.cache.move_to_end(key)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return value

    async def _json(self, path, params=None):
        session = await self.get_session()
        headers = {"Cookie": self.cookie} if self.cookie else {}
        try:
            async with (
                self.gate,
                session.get(
                    API + path, params=params, headers=headers, allow_redirects=False
                ) as response,
            ):
                if response.status != 200:
                    raise BiliError(
                        f"B 站请求失败（HTTP {response.status}），请稍后重试。"
                    )
                raw = await read_limited(response.content, 4 * 1024 * 1024)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BiliError("暂时无法连接 B 站，请检查网络后重试。") from None

    async def _signed(self, path, params):
        key = self._cached("wbi_key")
        if not key:
            response = await self._json("/x/web-interface/nav")
            # Anonymous nav returns -101 while still providing the public WBI keys.
            nav = response.get("data") or {}
            images = nav.get("wbi_img") or {}
            raw = "".join(
                urlsplit(images.get(k, "")).path.rsplit("/", 1)[-1].split(".")[0]
                for k in ("img_url", "sub_url")
            )
            if len(raw) < 64:
                raise BiliError("签名信息暂时不可用，请稍后重试。")
            key = self._remember("wbi_key", "".join(raw[i] for i in MIXIN)[:32], 3600)
        values = {**params, "wts": int(time.time())}
        values = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(values.items())}
        query = urlencode(values)
        values["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
        return await self._json(path, values)

    async def resolve(self, target):
        if re.fullmatch(r"BV[A-Za-z0-9]{10}|av\d+", target):
            return target
        current = target
        session = await self.get_session()
        try:
            for _ in range(5):
                if not allowed_redirect(current):
                    raise BiliError("短链接跳转到了非 B 站地址，已停止解析。")
                if value := video_id_from_url(current):
                    return value
                async with (
                    self.gate,
                    session.get(current, allow_redirects=False) as response,
                ):
                    if response.status in (
                        301,
                        302,
                        303,
                        307,
                        308,
                    ) and response.headers.get("Location"):
                        current = urljoin(current, response.headers["Location"])
                    else:
                        raise BiliError("短链接无效或没有指向 B 站视频。")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BiliError("短链接暂时无法打开，请发送 BV 号重试。") from None
        raise BiliError("短链接跳转次数过多，请直接发送 BV 号。")

    async def video(self, target):
        if cached := self._cached(("video", target)):
            return cached
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}|av\d+", target):
            raise BiliError("视频编号无效，请发送完整 BV 号或视频链接。")
        params = {"aid": target[2:]} if target.startswith("av") else {"bvid": target}
        response = await self._json("/x/web-interface/view", params)
        if response.get("code") in (-403, -352, -412):
            response = await self._signed("/x/web-interface/wbi/view", params)
        data = check_response(response)
        if not data.get("bvid"):
            raise BiliError("视频数据不完整，请稍后重试。")
        return self._remember(("video", target), data)

    async def comments(self, aid):
        if cached := self._cached(("comments", aid)):
            return cached
        try:
            response = await self._json(
                "/x/v2/reply", {"type": 1, "oid": aid, "sort": 2, "pn": 1, "ps": 20}
            )
            if response.get("code") in (-403, -352, -412):
                response = await self._signed(
                    "/x/v2/reply/wbi/main",
                    {
                        "type": 1,
                        "oid": aid,
                        "mode": 3,
                        "pagination_str": '{"offset":""}',
                    },
                )
            data = check_response(response)
            replies = list(data.get("top_replies") or [])
            upper = (data.get("upper") or {}).get("top")
            if upper:
                replies.append(upper)
            replies.extend(data.get("hots") or data.get("replies") or [])
            unique, seen = [], set()
            for item in replies:
                key = item.get("rpid")
                if key not in seen:
                    unique.append(item)
                    seen.add(key)
            return self._remember(("comments", aid), (unique, "ok"))
        except BiliError:
            return self._remember(("comments", aid), ([], "unavailable"), 30)

    async def playback(self, video, quality="720P", direct_only=False):
        from .downloads import QUALITIES, choose_stream

        pages = video.get("pages") or []
        cid = pages[0].get("cid") if pages else video.get("cid")
        if not cid:
            raise BiliError("视频缺少播放编号，暂时无法下载。")
        requested = QUALITIES.get(str(quality).upper(), 64)
        params = {
            "bvid": video["bvid"],
            "cid": cid,
            "qn": requested,
            "fnval": 1,
            "fnver": 0,
            "fourk": 1,
        }
        response = await self._json("/x/player/playurl", params)
        if response.get("code") in (-403, -352, -412):
            response = await self._signed("/x/player/wbi/playurl", params)
        data = check_response(response)
        combined = choose_stream(data, quality) if data.get("durl") else None
        if combined and (direct_only or combined["quality_id"] >= requested):
            return combined
        try:
            params["fnval"] = 4048
            response = await self._signed("/x/player/wbi/playurl", params)
            separate = choose_stream(check_response(response), quality)
            if combined is None or separate["quality_id"] > combined["quality_id"]:
                return separate
        except BiliError:
            if combined is None:
                raise
        if combined:
            return combined
        raise BiliError("当前账号暂时无法获取视频播放地址，请扫码登录后重试。")

    async def audio_playback(self, video):
        """Prefer a small DASH audio track; never fetch video bytes here."""
        from .downloads import choose_stream, urls

        pages = video.get("pages") or []
        cid = pages[0].get("cid") if pages else video.get("cid")
        if not cid:
            raise BiliError("视频缺少播放编号，暂时无法提取音频。")
        params = {
            "bvid": video["bvid"],
            "cid": cid,
            "qn": 16,
            "fnval": 4048,
            "fnver": 0,
        }
        data = check_response(await self._signed("/x/player/wbi/playurl", params))
        dash = data.get("dash") or {}
        audios = [item for item in dash.get("audio") or [] if urls(item)]
        if audios:
            audio = min(
                audios,
                key=lambda item: (
                    not str(item.get("codecs", "")).startswith("mp4a"),
                    int(item.get("bandwidth") or 0),
                ),
            )
            return {
                "duration": float(data.get("timelength") or 0) / 1000
                or float(dash.get("duration") or 0),
                "audio": urls(audio),
                "videos": [],
                "backups": [],
            }
        if data.get("durl"):
            return choose_stream(data, "360P")
        raise BiliError("没有获取到可用音轨，无法生成音频总结。")

    async def image(self, url):
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http://"):
            url = "https://" + url[7:]
        if not allowed_image(url):
            return ""
        session = await self.get_session()
        try:
            async with self.gate, session.get(url, allow_redirects=False) as response:
                mime = response.headers.get("Content-Type", "").split(";")[0].lower()
                if response.status != 200 or mime not in {
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "image/avif",
                    "image/gif",
                }:
                    return ""
                raw = await read_limited(response.content, 5 * 1024 * 1024)
                return f"data:{mime};base64," + base64.b64encode(raw).decode()
        except (aiohttp.ClientError, asyncio.TimeoutError, BiliError):
            return ""

    async def embed_images(self, card):
        import copy

        card = copy.deepcopy(card)
        fields = [(card, "cover"), (card["owner"], "avatar")] + [
            (item, "avatar") for item in card["comments"]
        ]
        images = await asyncio.gather(
            *(self.image(obj.get(key) or "") for obj, key in fields)
        )
        for (obj, key), value in zip(fields, images):
            obj[key] = value
        return card

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
