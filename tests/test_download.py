import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_bilicard.client import BiliError
from astrbot_plugin_bilicard.downloads import (
    VideoDownloader,
    allowed_media,
    choose_stream,
)


class SelectionTests(unittest.TestCase):
    def test_quality_limit_and_compatible_codec_with_actual_quality(self):
        data = {
            "timelength": 2000,
            "dash": {
                "video": [
                    {
                        "id": 120,
                        "codecs": "avc1.640033",
                        "baseUrl": "https://v.bilivideo.com/4k",
                    },
                    {
                        "id": 64,
                        "codecs": "hev1",
                        "baseUrl": "https://v.bilivideo.com/hevc",
                    },
                    {
                        "id": 64,
                        "codecs": "avc1.64001f",
                        "baseUrl": "https://v.bilivideo.com/avc",
                    },
                ],
                "audio": [
                    {"baseUrl": "https://v.bilivideo.com/audio", "codecs": "mp4a.40.2"}
                ],
            },
        }
        stream = choose_stream(data, "1080P")
        self.assertEqual(stream["quality"], "720P")
        self.assertEqual(stream["videos"][0], "https://v.bilivideo.com/avc")
        self.assertTrue(stream["audio"])
        self.assertFalse(allowed_media("https://127.0.0.1/x"))
        self.assertFalse(allowed_media("https://bilivideo.com.evil.test/x"))


class FakeContent:
    async def iter_chunked(self, size):
        yield b"abc"
        yield b"def"


class FakeResponse:
    status = 200
    headers = {}
    content = FakeContent()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSession:
    def get(self, *args, **kwargs):
        return FakeResponse()


class FakeClient:
    async def get_session(self):
        return FakeSession()


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_temp_files_removed_after_asr_error_and_extract_error(self):
        with tempfile.TemporaryDirectory() as root:
            downloader = VideoDownloader(root, FakeClient())
            created = []

            async def build(stream, folder):
                path = folder / "audio.wav"
                path.write_bytes(b"audio-data")
                created.append(path)
                if stream.get("fail_extract"):
                    raise BiliError("音频提取失败")
                return str(path)

            with patch.object(downloader, "_build_audio", new=build):
                for fail_extract in [False, True]:
                    with self.assertRaises((BiliError, RuntimeError)):
                        async with downloader.audio(
                            {"fail_extract": fail_extract}
                        ) as path:
                            self.assertTrue(Path(path).exists())
                            raise RuntimeError("STT failed")
                    self.assertFalse(created[-1].exists())
                    self.assertEqual(list(downloader.root.iterdir()), [])

    async def test_streaming_size_limit_and_partial_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            downloader = VideoDownloader(root, FakeClient())
            destination = Path(root) / "partial.mp4"
            with self.assertRaises(BiliError):
                await downloader.fetch(
                    ["https://v.bilivideo.com/video"], destination, 5
                )
            self.assertFalse(destination.exists())

    async def test_media_exists_during_use_and_is_removed_on_success_or_error(self):
        stream = {
            "quality": "720P",
            "videos": ["https://v.bilivideo.com/video"],
            "backups": [],
            "audio": [],
            "mp4": True,
            "transcode": False,
        }
        with tempfile.TemporaryDirectory() as root:
            downloader = VideoDownloader(root, FakeClient())
            for fail in [False, True]:
                try:
                    async with downloader.download(stream) as path:
                        self.assertEqual(Path(path).read_bytes(), b"abcdef")
                        if fail:
                            raise RuntimeError("send failed")
                except RuntimeError:
                    pass
                self.assertFalse(Path(path).exists())
                self.assertEqual(list(downloader.root.iterdir()), [])
