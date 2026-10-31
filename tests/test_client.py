import unittest
from unittest.mock import patch

from astrbot_plugin_bilicard.client import (
    BiliClient,
    BiliError,
    allowed_image,
    allowed_redirect,
    read_limited,
)


class URLSafetyTests(unittest.TestCase):
    def test_redirect_targets_are_limited_to_real_bili_hosts(self):
        for url in [
            "https://www.bilibili.com/video/BV1GJ411x7h7",
            "https://b23.tv/abcd",
        ]:
            self.assertTrue(allowed_redirect(url))
        for url in [
            "http://127.0.0.1/",
            "https://bilibili.com.evil.test/",
            "https://evil.test/",
            "file:///etc/passwd",
            "https://user@www.bilibili.com/",
            "https://b23.tv:8080/x",
        ]:
            self.assertFalse(allowed_redirect(url))

    def test_images_are_https_bilibili_cdn_only(self):
        self.assertTrue(allowed_image("https://i0.hdslb.com/bfs/archive/a.jpg"))
        for url in [
            "https://hdslb.com.evil.test/x",
            "http://127.0.0.1/x",
            "data:image/svg+xml,x",
            "https://i0.hdslb.com:8443/a",
        ]:
            self.assertFalse(allowed_image(url))


class FixtureClient(BiliClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    async def _json(self, path, params=None):
        self.calls.append((path, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_playback_selects_audio_without_requiring_video(self):
        client = FixtureClient(
            [
                {
                    "code": 0,
                    "data": {
                        "timelength": 20000,
                        "dash": {
                            "audio": [
                                {
                                    "baseUrl": "https://v.bilivideo.com/audio",
                                    "codecs": "mp4a.40.2",
                                    "bandwidth": 64000,
                                },
                                {
                                    "baseUrl": "https://127.0.0.1/private",
                                    "codecs": "mp4a.40.2",
                                    "bandwidth": 1,
                                },
                            ]
                        },
                    },
                }
            ]
        )

        async def signed(path, params):
            return await client._json(path, params)

        client._signed = signed
        stream = await client.audio_playback({"bvid": "BV1GJ411x7h7", "cid": 2})
        self.assertEqual(stream["audio"], ["https://v.bilivideo.com/audio"])
        self.assertEqual(stream["videos"], [])
        self.assertEqual(stream["duration"], 20)
        self.assertEqual(client.calls[0][1]["fnval"], 4048)

    async def test_chunked_media_is_read_completely_with_size_limit(self):
        class Stream:
            async def iter_chunked(self, size):
                for chunk in [b"first", b"second", b"last"]:
                    yield chunk

        self.assertEqual(await read_limited(Stream(), 30), b"firstsecondlast")
        with self.assertRaises(BiliError):
            await read_limited(Stream(), 5)

    async def test_video_response_cached_and_av_resolves_to_bv(self):
        client = FixtureClient(
            [{"code": 0, "data": {"bvid": "BV1GJ411x7h7", "aid": 170001}}]
        )
        first = await client.video("av170001")
        second = await client.video("av170001")
        self.assertEqual(first["bvid"], "BV1GJ411x7h7")
        self.assertEqual(first, second)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1], {"aid": "170001"})

    async def test_missing_video_has_readable_error(self):
        client = FixtureClient([{"code": -404, "message": "啥都木有"}])
        with self.assertRaisesRegex(BiliError, "不存在|删除"):
            await client.video("BV1GJ411x7h7")

    async def test_optional_comments_do_not_break_video(self):
        client = FixtureClient([BiliError("评论读取失败")])
        comments, status = await client.comments(170001)
        self.assertEqual(comments, [])
        self.assertEqual(status, "unavailable")

    async def test_top_comments_deduplicated_and_in_api_hot_order(self):
        replies = [{"rpid": 1, "like": 20}, {"rpid": 2, "like": 10}]
        client = FixtureClient(
            [{"code": 0, "data": {"top_replies": [replies[0]], "replies": replies}}]
        )
        comments, status = await client.comments(170001)
        self.assertEqual([x["rpid"] for x in comments], [1, 2])
        self.assertEqual(status, "ok")

    async def test_resolve_rejects_external_url_before_network(self):
        client = BiliClient()
        with self.assertRaises(BiliError):
            await client.resolve("https://evil.test/x")
        await client.close()

    async def test_cache_expiry_fetches_new_data(self):
        client = FixtureClient(
            [
                {"code": 0, "data": {"bvid": "BV1GJ411x7h7", "title": "旧"}},
                {"code": 0, "data": {"bvid": "BV1GJ411x7h7", "title": "新"}},
            ]
        )
        with patch("astrbot_plugin_bilicard.client.time.monotonic", return_value=100):
            await client.video("BV1GJ411x7h7")
        with patch("astrbot_plugin_bilicard.client.time.monotonic", return_value=500):
            self.assertEqual((await client.video("BV1GJ411x7h7"))["title"], "新")
