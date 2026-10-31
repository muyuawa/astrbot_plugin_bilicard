"""Select public playback streams and own temporary media until upload completes."""

import asyncio
import shutil
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from .client import BiliError

QUALITIES = {"360P": 16, "480P": 32, "720P": 64, "1080P": 80, "1080P60": 116, "4K": 120}
QUALITY_NAMES = {
    16: "360P",
    32: "480P",
    64: "720P",
    80: "1080P",
    112: "1080P+",
    116: "1080P60",
    120: "4K",
}


class DownloadLimitError(BiliError):
    pass


def allowed_media(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        trusted = any(
            host == domain or host.endswith("." + domain)
            for domain in ("bilivideo.com", "bilivideo.cn", "bilivideo.net")
        )
        trusted = trusted or (
            host.startswith("upos-") and host.endswith(".akamaized.net")
        )
        return (
            trusted
            and parsed.scheme in {"http", "https"}
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 80, 443)
        )
    except ValueError:
        return False


def urls(item):
    primary = item.get("baseUrl") or item.get("base_url") or item.get("url") or ""
    values = [primary, *(item.get("backupUrl") or item.get("backup_url") or [])]
    return list(dict.fromkeys(url for url in values if allowed_media(url)))


def choose_stream(data, quality):
    ceiling = QUALITIES.get(str(quality).upper(), 64)
    duration = float(data.get("timelength") or 0) / 1000
    segments = data.get("durl") or []
    if segments:
        candidates = [urls(item) for item in segments]
        if any(not item for item in candidates):
            raise BiliError("没有获取到可用的视频下载地址。")
        actual = int(data.get("quality") or 0)
        if actual > ceiling:
            raise BiliError("接口返回的画质高于设定上限，请调整清晰度后重试。")
        return {
            "quality": QUALITY_NAMES.get(actual, str(actual)),
            "quality_id": actual,
            "duration": duration,
            "videos": [item[0] for item in candidates],
            "backups": [item[1:] for item in candidates],
            "audio": [],
            "mp4": str(data.get("format", "")).startswith("mp4") and len(segments) == 1,
            "transcode": False,
        }
    dash = data.get("dash") or {}
    videos = [
        item
        for item in dash.get("video") or []
        if 0 < int(item.get("id") or 0) <= ceiling and urls(item)
    ]
    if not videos:
        raise BiliError("当前账号没有可用的目标画质，请降低清晰度或扫码登录。")
    video = max(
        videos,
        key=lambda item: (
            int(item["id"]),
            str(item.get("codecs", "")).startswith("avc1"),
            int(item.get("bandwidth") or 0),
        ),
    )
    audios = [item for item in dash.get("audio") or [] if urls(item)]
    audio = (
        max(
            audios,
            key=lambda item: (
                str(item.get("codecs", "")).startswith("mp4a"),
                int(item.get("bandwidth") or 0),
            ),
        )
        if audios
        else None
    )
    candidates = urls(video)
    actual = int(video["id"])
    return {
        "quality": QUALITY_NAMES.get(actual, str(actual)),
        "quality_id": actual,
        "duration": duration or float(dash.get("duration") or 0),
        "videos": [candidates[0]],
        "backups": [candidates[1:]],
        "audio": urls(audio) if audio else [],
        "mp4": False,
        "transcode": not str(video.get("codecs", "")).startswith("avc1"),
    }


def part_duration(video):
    pages = video.get("pages") or []
    return float((pages[0].get("duration") if pages else video.get("duration")) or 0)


def direct_links(stream, reason):
    lines = [reason, f"实际画质：{stream['quality']}；以下为临时直链："]
    for index, url in enumerate(stream["videos"], 1):
        label = "视频直链" if len(stream["videos"]) == 1 else f"视频分段 {index}"
        lines.append(f"{label}：\n{url}")
    if stream.get("audio"):
        lines.append(f"音频直链（需与视频合并）：\n{stream['audio'][0]}")
    lines.append("直链有时效，部分播放器需使用 B 站 Referer。")
    return "\n".join(lines)


class VideoDownloader:
    def __init__(self, root, client, max_size_mb=100, timeout=300, ffmpeg_path=""):
        self.root = Path(root).resolve() / "videos"
        self.root.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.max_bytes = max(1, min(500, int(max_size_mb))) * 1024 * 1024
        self.timeout = max(30, min(1800, int(timeout)))
        self.ffmpeg_path = ffmpeg_path
        self.gate = asyncio.Semaphore(1)

    async def fetch(self, candidates, destination, limit):
        session = await self.client.get_session()
        for url in candidates:
            if not allowed_media(url):
                continue
            complete = False
            try:
                for _ in range(4):
                    if not allowed_media(url):
                        break
                    async with session.get(
                        url,
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=self.timeout, sock_read=30),
                    ) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            url = urljoin(url, response.headers.get("Location", ""))
                            continue
                        if response.status != 200:
                            break
                        declared = response.headers.get("Content-Length", "")
                        if declared.isdigit() and int(declared) > limit:
                            raise DownloadLimitError(
                                "视频超过配置的大小上限，已停止下载。"
                            )
                        size = 0
                        with Path(destination).open("wb") as output:
                            async for chunk in response.content.iter_chunked(65536):
                                size += len(chunk)
                                if size > limit:
                                    raise DownloadLimitError(
                                        "视频超过配置的大小上限，已停止下载。"
                                    )
                                output.write(chunk)
                        if size == 0:
                            break
                        complete = True
                        return size
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            finally:
                if not complete:
                    Path(destination).unlink(missing_ok=True)
        raise BiliError("视频下载失败，直链可能已过期或 B 站暂时限制访问。")

    async def _mux(self, sources, audio, stream, output):
        executable = self.ffmpeg_path or shutil.which("ffmpeg")
        if not executable:
            raise BiliError(
                "该画质需要 FFmpeg 合并音视频，请安装 ffmpeg 或设置 ffmpeg_path。"
            )
        args = [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if len(sources) > 1:
            playlist = output.parent / "concat.txt"
            playlist.write_text(
                "".join(f"file '{source.name}'\n" for source in sources),
                encoding="utf-8",
            )
            args += [
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(playlist),
            ]
        else:
            args += ["-protocol_whitelist", "file,pipe", "-i", str(sources[0])]
        if audio:
            args += [
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        else:
            args += ["-map", "0:v:0", "-map", "0:a:0?"]
        if stream.get("transcode"):
            args += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
            ]
        else:
            args += ["-c:v", "copy"]
        args += ["-c:a", "aac", "-movflags", "+faststart", str(output)]
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError:
            raise BiliError(
                "FFmpeg 无法启动，请检查 ffmpeg_path 和运行权限。"
            ) from None
        completion = asyncio.create_task(process.communicate())
        try:
            while not completion.done():
                await asyncio.wait({completion}, timeout=0.25)
                if output.exists() and output.stat().st_size > self.max_bytes:
                    raise DownloadLimitError("合成后的视频超过大小上限，已停止处理。")
            await completion
            if process.returncode != 0:
                raise BiliError("视频合成失败，请检查 FFmpeg 是否支持 H.264/AAC。")
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await completion

    async def _build(self, stream, folder):
        total, sources = 0, []
        for index, url in enumerate(stream["videos"]):
            source = folder / f"segment-{index}.bin"
            backups = stream.get("backups") or []
            candidates = [url, *(backups[index] if index < len(backups) else [])]
            total += await self.fetch(candidates, source, self.max_bytes - total)
            sources.append(source)
        audio = None
        if stream.get("audio"):
            audio = folder / "audio.bin"
            total += await self.fetch(stream["audio"], audio, self.max_bytes - total)
        output = folder / "video.mp4"
        if stream.get("mp4") and len(sources) == 1 and not audio:
            sources[0].replace(output)
        else:
            await self._mux(sources, audio, stream, output)
        if not output.exists() or output.stat().st_size == 0:
            raise BiliError("没有生成有效的视频文件。")
        if output.stat().st_size > self.max_bytes:
            raise DownloadLimitError("合成后的视频超过大小上限，已停止发送。")
        return str(output)

    @asynccontextmanager
    async def audio(self, stream):
        """Keep extracted speech audio alive for ASR, then delete all job files."""
        async with self.gate:
            with tempfile.TemporaryDirectory(
                prefix="audio-", dir=self.root
            ) as directory:
                try:
                    path = await asyncio.wait_for(
                        self._build_audio(stream, Path(directory)), self.timeout
                    )
                except asyncio.TimeoutError:
                    raise BiliError("音频下载或提取超时，临时文件已清理。") from None
                yield path

    async def _build_audio(self, stream, folder):
        executable = self.ffmpeg_path or shutil.which("ffmpeg")
        if not executable:
            raise BiliError("音频总结需要 FFmpeg，请安装或设置 ffmpeg_path。")
        # DASH only downloads audio. Legacy combined streams are extracted locally.
        total, sources = 0, []
        candidates = (
            [stream["audio"]]
            if stream.get("audio")
            else [
                [
                    url,
                    *(
                        (stream.get("backups") or [])[index]
                        if index < len(stream.get("backups") or [])
                        else []
                    ),
                ]
                for index, url in enumerate(stream.get("videos") or [])
            ]
        )
        if not candidates:
            raise BiliError("没有可用于总结的音轨。")
        for index, choices in enumerate(candidates):
            source = folder / f"source-{index}.bin"
            total += await self.fetch(choices, source, self.max_bytes - total)
            sources.append(source)
        args = [
            str(executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
        ]
        if len(sources) > 1:
            playlist = folder / "concat.txt"
            playlist.write_text(
                "".join(f"file '{source.name}'\n" for source in sources),
                encoding="utf-8",
            )
            args += ["-f", "concat", "-safe", "1", "-i", str(playlist)]
        else:
            args += ["-i", str(sources[0])]
        output = folder / "audio.wav"
        limit = 20 * 1024 * 1024
        args += [
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-fs",
            str(limit + 1),
            str(output),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError:
            raise BiliError(
                "FFmpeg 无法启动，请检查 ffmpeg_path 和运行权限。"
            ) from None
        completion = asyncio.create_task(process.communicate())
        try:
            while not completion.done():
                await asyncio.wait({completion}, timeout=0.25)
                if output.exists() and output.stat().st_size > limit:
                    raise DownloadLimitError(
                        "提取音频超过 20 MiB，请降低下载时长上限。"
                    )
            await completion
            if process.returncode != 0:
                raise BiliError(
                    "音频提取失败，视频可能没有音轨，或 FFmpeg 不支持此格式。"
                )
            if not output.exists() or output.stat().st_size <= 44:
                raise BiliError("视频中没有可用音频。")
            if output.stat().st_size > limit:
                raise DownloadLimitError("提取音频超过 20 MiB，请降低下载时长上限。")
            return str(output)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await completion

    @asynccontextmanager
    async def download(self, stream):
        async with self.gate:
            with tempfile.TemporaryDirectory(prefix="job-", dir=self.root) as directory:
                folder = Path(directory).resolve()
                folder.relative_to(self.root.resolve())
                try:
                    path = await asyncio.wait_for(
                        self._build(stream, folder), self.timeout
                    )
                except asyncio.TimeoutError:
                    raise BiliError("视频下载或处理超时，临时文件已清理。") from None
                yield path
