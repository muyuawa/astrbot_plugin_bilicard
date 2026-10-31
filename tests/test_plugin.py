"""Small event-boundary checks; SDK types are isolated because AstrBot is not installed here."""

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class At:
    def __init__(self, qq):
        self.qq = qq


class Json:
    def __init__(self, data):
        self.data = data


class Star:
    def __init__(self, context):
        self.context = context


class MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []


class Video:
    def __init__(self, file="", path=""):
        self.file = file
        self.path = path or file

    @classmethod
    def fromFileSystem(cls, path):
        path = Path(path).resolve()
        return cls(file=path.as_uri(), path=str(path))

    @classmethod
    def fromURL(cls, url):
        return cls(file=url)


class TextResult(tuple):
    def __new__(cls, text):
        result = super().__new__(cls, ("text", text))
        result.use_t2i_ = None
        return result

    def use_t2i(self, value):
        self.use_t2i_ = value
        return self


def load_plugin():
    modules = {
        name: types.ModuleType(name)
        for name in [
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.message_components",
            "astrbot.api.star",
            "astrbot.core",
            "astrbot.core.utils",
            "astrbot.core.utils.astrbot_path",
        ]
    }
    import logging

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("bilicard-test")
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        event_message_type=lambda *a, **k: lambda fn: fn,
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.message_components"].At = At
    modules["astrbot.api.message_components"].Json = Json
    modules["astrbot.api.message_components"].Video = Video
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: Path("unused") / name
    )
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: "unused"
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_bilicard.main")


main = load_plugin()


class Event:
    def __init__(
        self, text, *, private=False, admin=False, mentioned=False, self_message=False
    ):
        self.text, self.private, self.admin = text, private, admin
        self.components = [At("bot")] if mentioned else []
        self.unified_msg_origin = "qq:group:1"
        self.sender = "bot" if self_message else "user"
        self.stopped = False
        self.is_at_or_wake_command = False
        self.sent = []

    async def send(self, chain):
        self.sent.append(chain)

    def get_message_str(self):
        return self.text

    def get_messages(self):
        return self.components

    def get_self_id(self):
        return "bot"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_id(self):
        return self.sender

    def is_private_chat(self):
        return self.private

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return TextResult(text)

    def image_result(self, path):
        return ("image", path)


class FakeClient:
    def __init__(self):
        self.cookie = ""
        self.requests = 0

    async def resolve(self, target):
        return target

    async def video(self, target):
        self.requests += 1
        return {"bvid": "BV1GJ411x7h7", "aid": 1, "title": "示例视频"}

    async def comments(self, aid):
        return [], "ok"

    async def embed_images(self, card):
        return card

    def set_cookie(self, cookie):
        self.cookie = cookie

    async def close(self):
        pass


class FakeCards:
    async def render(self, card):
        return "card.png"

    async def close(self):
        pass


async def collect(plugin, event):
    return [reply async for reply in plugin.on_message(event)]


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.object(
            main.StarTools, "get_data_dir", return_value=Path(self.temp.name)
        ):
            self.plugin = main.BiliCard(object(), {})
        self.plugin.client = FakeClient()
        self.plugin.cards = FakeCards()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_auto_toggle_and_explicit_mention(self):
        self.assertEqual(await collect(self.plugin, Event("BV1GJ411x7h7")), [])
        event = Event("解析 BV1GJ411x7h7", mentioned=True)
        self.assertEqual(await collect(self.plugin, event), [("image", "card.png")])
        self.assertTrue(event.stopped)
        stripped_command = Event("bili BV1GJ411x7h7")
        stripped_command.is_at_or_wake_command = True
        self.assertEqual(
            await collect(self.plugin, stripped_command), [("image", "card.png")]
        )
        self.plugin.config["auto_detect"] = True
        event = Event("看这个 BV1GJ411x7h7")
        self.assertEqual(await collect(self.plugin, event), [("image", "card.png")])

    async def test_qq_cards_and_forward_contents_follow_outer_trigger_policy(self):
        from unittest.mock import AsyncMock
        from test_shares import MINIAPP

        card = Json(MINIAPP)
        ev = Event("")
        ev.components = [card]
        self.assertEqual(await collect(self.plugin, ev), [])
        self.plugin.config["auto_detect"] = True
        self.assertEqual(await collect(self.plugin, ev), [("image", "card.png")])
        self.plugin.config["auto_detect"] = False
        ev = Event("解析", mentioned=True)
        ev.components.append(card)
        self.assertEqual(await collect(self.plugin, ev), [("image", "card.png")])
        ev = Event("")
        ev.components = [types.SimpleNamespace(type="Forward", id="resource-1")]
        ev.bot = types.SimpleNamespace(
            call_action=AsyncMock(
                return_value={
                    "messages": [
                        {
                            "content": [
                                {"type": "at", "data": {"qq": "bot"}},
                                {
                                    "type": "text",
                                    "data": {"text": "/bili BV1GJ411x7h7"},
                                },
                            ]
                        }
                    ]
                }
            )
        )
        self.assertEqual(await collect(self.plugin, ev), [])
        ev.bot.call_action.assert_not_awaited()
        ev.text = "解析"
        ev.components.insert(0, At("bot"))
        self.assertEqual(await collect(self.plugin, ev), [("image", "card.png")])
        ev.bot.call_action.assert_awaited_once_with("get_forward_msg", id="resource-1")

    async def test_duplicate_auto_suppression_and_self_message(self):
        self.plugin.config["auto_detect"] = True
        event = Event("BV1GJ411x7h7")
        await collect(self.plugin, event)
        self.assertEqual(await collect(self.plugin, Event("BV1GJ411x7h7")), [])
        self.assertEqual(
            await collect(self.plugin, Event("BV1GJ411x7h7", self_message=True)), []
        )
        self.assertEqual(self.plugin.client.requests, 1)
        self.assertEqual(
            await collect(self.plugin, Event("/bili BV1GJ411x7h7")),
            [("image", "card.png")],
        )

    async def test_login_requires_both_admin_and_private(self):
        for event in [
            Event("/bili 登录", admin=True),
            Event("/bili 登录", private=True),
        ]:
            replies = await collect(self.plugin, event)
            self.assertEqual(replies[0][0], "text")
            self.assertIn("管理员私聊", replies[0][1])
        self.assertEqual(self.plugin.login_task, None)

    async def test_browser_failure_preserves_video_text(self):
        async def broken(card):
            raise RuntimeError("missing browser")

        self.plugin.cards.render = broken
        replies = await collect(self.plugin, Event("/bili BV1GJ411x7h7"))
        self.assertIn("示例视频", replies[0][1])
        self.assertIn("https://www.bilibili.com/video/", replies[0][1])

    async def test_summary_toggle_and_failure_preserve_card_and_text_fallback(self):
        from unittest.mock import AsyncMock

        rendered = []

        async def render(card):
            rendered.append(card)
            return "card.png"

        self.plugin.cards.render = render
        with patch.object(
            main.AudioSummarizer, "summarize", new_callable=AsyncMock
        ) as summary:
            await collect(self.plugin, Event("/bili BV1GJ411x7h7"))
            summary.assert_not_awaited()
            self.plugin.config["audio_summary"] = True
            summary.return_value = "这是根据语音生成的总结。"
            replies = await collect(self.plugin, Event("/bili BV1GJ411x7h7"))
            self.assertEqual(replies[-1], ("image", "card.png"))
            self.assertEqual(rendered[-1]["summary"], summary.return_value)
            summary.side_effect = main.BiliError("语音识别失败")
            replies = await collect(self.plugin, Event("/bili BV1GJ411x7h7"))
            self.assertEqual(replies[-1], ("image", "card.png"))
            self.assertEqual(rendered[-1]["summary_notice"], "语音识别失败")
            self.assertEqual(rendered[-1]["summary"], "")

            async def broken(card):
                raise RuntimeError("missing browser")

            self.plugin.cards.render = broken
            summary.side_effect = None
            replies = await collect(self.plugin, Event("/bili BV1GJ411x7h7"))
            self.assertIn("AI 音频总结", replies[-1][1])
            self.assertIn(summary.return_value, replies[-1][1])

    async def test_duration_limit_returns_direct_link_without_downloading(self):
        async def playback(video, quality, direct_only=False):
            return {
                "quality": "720P",
                "videos": ["https://v.bilivideo.com/video"],
                "audio": [],
                "duration": 301,
            }

        async def forbidden(*args, **kwargs):
            raise AssertionError("Over-limit media must never be downloaded")

        self.plugin.client.playback = playback
        self.plugin.videos.download = forbidden
        event = Event("/bili 下载 BV1GJ411x7h7")
        self.plugin.client.video = self.long_video
        replies = await collect(self.plugin, event)
        self.assertEqual(len(replies), 1)
        self.assertIn("https://v.bilivideo.com/video", replies[0][1])
        self.assertIn("超过", replies[0][1])
        self.assertIs(replies[0].use_t2i_, False)
        self.assertEqual(event.sent, [])
        self.plugin.config["send_video"] = True

        async def rounded_duration(target):
            return {"bvid": "BV1GJ411x7h7", "aid": 1, "cid": 2, "duration": 300}

        self.plugin.client.video = rounded_duration
        replies = await collect(self.plugin, Event("/bili BV1GJ411x7h7"))
        self.assertEqual(len(replies), 1)
        self.assertIn("https://v.bilivideo.com/video", replies[0][1])

    async def long_video(self, target):
        return {"bvid": "BV1GJ411x7h7", "aid": 1, "cid": 2, "duration": 301}

    async def test_native_video_sent_before_cleanup_even_on_send_failure(self):
        from astrbot_plugin_bilicard.downloads import VideoDownloader
        from test_download import FakeClient as MediaClient

        stream = {
            "quality": "720P",
            "videos": ["https://v.bilivideo.com/video"],
            "backups": [],
            "audio": [],
            "mp4": True,
            "transcode": False,
            "duration": 2,
        }

        async def playback(*args, **kwargs):
            return stream

        self.plugin.client.playback = playback
        self.plugin.videos = VideoDownloader(self.temp.name, MediaClient())
        for fail in [False, True]:
            self.plugin.config["video_send_mode"] = "local" if fail else "auto"
            event = Event("/bili 下载 BV1GJ411x7h7")
            observed = []

            async def send(chain):
                component = chain.chain[0]
                self.assertIsInstance(component, Video)
                self.assertTrue(component.file.startswith("file:///"))
                self.assertTrue(Path(component.path).exists())
                observed.append(component.path)
                if fail:
                    raise RuntimeError(
                        "upload failed retcode=1200 https://example.test/api/file/private-token Cookie: SESSDATA=secret-cookie"
                    )

            event.send = send
            replies = await collect(self.plugin, event)
            self.assertEqual(len(observed), 1)
            self.assertFalse(Path(observed[0]).exists())
            if fail:
                self.assertIn("retcode=1200", replies[-1][1])
                self.assertNotIn("secret-cookie", replies[-1][1])
                self.assertNotIn("private-token", replies[-1][1])
                self.assertIs(replies[-1].use_t2i_, False)

    async def test_cross_container_mode_carries_bytes_without_host_path_or_callback(
        self,
    ):
        import base64

        self.plugin.context = types.SimpleNamespace(get_config=lambda: {})
        self.plugin.config["video_send_mode"] = "base64"
        video_path = Path(self.temp.name) / "video.mp4"
        video_path.write_bytes(b"sample-video")
        component, mode = await self.plugin._prepare_video(str(video_path))
        payload = await component.to_dict()
        self.assertEqual(mode, "base64")
        self.assertEqual(payload["type"], "video")
        self.assertEqual(
            base64.b64decode(payload["data"]["file"].removeprefix("base64://")),
            b"sample-video",
        )
        self.assertNotIn(str(video_path), str(payload))
        # Existing callback deployments keep their URL transport in automatic mode.
        self.plugin.context.get_config = lambda: {
            "callback_api_base": "https://host.test"
        }
        self.plugin.config["video_send_mode"] = "auto"

        async def register(component):
            self.assertTrue(Path(component.path).exists())
            return "https://host.test/api/file/test-token"

        with patch.object(
            main.Video, "register_to_file_service", new=register, create=True
        ):
            component, mode = await self.plugin._prepare_video(str(video_path))
        self.assertEqual(mode, "callback")
        self.assertEqual(component.file, "https://host.test/api/file/test-token")

    async def test_auto_retries_only_unreadable_local_files_as_native_base64(self):
        import base64
        from astrbot_plugin_bilicard.downloads import VideoDownloader
        from test_download import FakeClient as MediaClient

        class ActionFailed(Exception):
            pass

        stream = {
            "quality": "720P",
            "videos": ["https://v.bilivideo.com/video"],
            "backups": [],
            "audio": [],
            "mp4": True,
            "transcode": False,
            "duration": 2,
        }

        async def playback(*args, **kwargs):
            return stream

        self.plugin.client.playback = playback
        self.plugin.videos = VideoDownloader(self.temp.name, MediaClient())
        for configured, error, attempts in [
            ("auto", "EACCES: permission denied, realpath", 2),
            ("local", "EACCES: permission denied, realpath", 1),
            ("auto", "connection timed out", 1),
        ]:
            self.plugin.config["video_send_mode"] = configured
            event = Event("/bili 下载 BV1GJ411x7h7")
            observed, source = [], []

            async def send(chain):
                component = chain.chain[0]
                observed.append(component.file)
                if component.file.startswith("file:///"):
                    source.append(component.path)
                    self.assertTrue(Path(component.path).exists())
                    raise ActionFailed(
                        f"<ActionFailed retcode=100 wording=\"{error} '{component.path}'\">"
                    )
                self.assertIsInstance(component, Video)
                self.assertTrue(Path(source[0]).exists())
                self.assertEqual(
                    base64.b64decode(component.file.removeprefix("base64://")),
                    b"abcdef",
                )

            event.send = send
            await collect(self.plugin, event)
            self.assertEqual(len(observed), attempts)
            self.assertFalse(Path(source[0]).exists())
            self.assertEqual(list(self.plugin.videos.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
