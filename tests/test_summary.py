"""Necessary audio-summary checks without external AI calls."""

import asyncio
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

from astrbot_plugin_bilicard.client import BiliError
from astrbot_plugin_bilicard.summary import AudioSummarizer


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "audio.wav"
        self.video = {"bvid": "BV1GJ411x7h7", "duration": 20}
        self.stream = {"duration": 20, "audio": ["https://v.bilivideo.com/audio"]}
        self.client = types.SimpleNamespace(
            audio_playback=AsyncMock(return_value=self.stream)
        )

        @asynccontextmanager
        async def audio(stream):
            self.path.write_bytes(b"audio-data")
            try:
                yield str(self.path)
            finally:
                self.path.unlink(missing_ok=True)

        self.downloader = types.SimpleNamespace(audio=audio)
        self.stt = types.SimpleNamespace(
            get_text=AsyncMock(return_value="音频中讲了制作咖啡的步骤。")
        )
        self.llm = types.SimpleNamespace(
            text_chat=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text="介绍了制作咖啡的步骤。"
                )
            )
        )
        self.context = types.SimpleNamespace(
            get_using_stt_provider_async=AsyncMock(return_value=self.stt),
            get_using_provider_async=AsyncMock(return_value=self.llm),
            get_provider_by_id=lambda key: {"stt": self.stt, "llm": self.llm}.get(key),
        )
        self.config = {}
        self.summary = AudioSummarizer(
            self.context, self.config, self.client, self.downloader
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_audio_is_transcribed_then_summarized_without_chat_history(self):
        async def transcribe(path):
            self.assertEqual(Path(path).read_bytes(), b"audio-data")
            return "音频中讲了制作咖啡的步骤。"

        async def chat(**kwargs):
            self.assertFalse(self.path.exists())
            self.assertIn("制作咖啡", kwargs["prompt"])
            self.assertEqual(kwargs["contexts"], [])
            self.assertNotIn("func_tool", kwargs)
            return types.SimpleNamespace(completion_text="介绍了制作咖啡的步骤。")

        self.stt.get_text.side_effect = transcribe
        self.llm.text_chat.side_effect = chat
        self.assertEqual(
            await self.summary.summarize(self.video, "group:1"),
            "介绍了制作咖啡的步骤。",
        )
        self.context.get_using_stt_provider_async.assert_awaited_once_with("group:1")
        self.context.get_using_provider_async.assert_awaited_once_with("group:1")
        self.assertFalse(self.path.exists())

    async def test_limits_and_missing_provider_stop_before_audio_download(self):
        for source_duration, playback_duration in [(301, 20), (300, 300.1), (0, 0)]:
            with self.subTest(source=source_duration, playback=playback_duration):
                self.video["duration"] = source_duration
                self.stream["duration"] = playback_duration
                with self.assertRaises(BiliError):
                    await self.summary.summarize(self.video, "group:1")
                self.stt.get_text.assert_not_awaited()
                self.assertFalse(self.path.exists())
        self.video["duration"] = 20
        self.config["summary_stt_provider_id"] = "missing"
        self.client.audio_playback.reset_mock()
        with self.assertRaisesRegex(BiliError, "语音识别"):
            await self.summary.summarize(self.video, "group:1")
        self.client.audio_playback.assert_not_awaited()

    async def test_empty_transcript_error_and_cancellation_cleanup(self):
        self.config.update(summary_stt_provider_id="stt", summary_llm_provider_id="llm")
        self.stt.get_text.return_value = "  "
        with self.assertRaisesRegex(BiliError, "语音|转写"):
            await self.summary.summarize(self.video, "group:1")
        self.llm.text_chat.assert_not_awaited()
        self.assertFalse(self.path.exists())
        self.stt.get_text.side_effect = RuntimeError("private-service-error")
        with self.assertRaisesRegex(BiliError, "语音识别"):
            await self.summary.summarize(self.video, "group:1")
        self.assertFalse(self.path.exists())

        entered = asyncio.Event()

        async def blocked(path):
            entered.set()
            await asyncio.Event().wait()

        self.stt.get_text.side_effect = blocked
        task = asyncio.create_task(self.summary.summarize(self.video, "group:1"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.path.exists())
