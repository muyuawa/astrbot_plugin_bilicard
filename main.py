"""AstrBot integration for Bilibili video cards and administrator QR login."""

import asyncio
import base64
import re
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Video
from astrbot.api.star import Context, Star, StarTools

from .auth import CredentialVault, QRLogin
from .client import BiliClient, BiliError
from .downloads import VideoDownloader, direct_links, part_duration
from .models import build_card, format_duration, text_fallback
from .parser import trigger_mode
from .renderer import CardRenderer
from .summary import AudioSummarizer
from .shares import message_target

HELP = (
    "发送 @机器人 解析 BV号/视频链接，或 /bili BV号/视频链接。\n"
    "支持 B 站链接、QQ 小程序/分享卡片和合并转发中的视频。\n"
    "可在插件配置开启 auto_detect，直接发送链接自动解析。\n"
    "视频下载：/bili 下载 BV号/链接，或 @机器人 下载 BV号/链接。\n"
    "管理员私聊：/bili 登录、/bili 登录状态、/bili 退出登录。"
)


def safe_error(exc):
    """Keep protocol return codes, but never print cookies or temporary URL tokens."""
    message = str(exc) or type(exc).__name__
    message = re.sub(r"https?://[^\s<>\"']+", "<url>", message)
    message = re.sub(r"base64://[A-Za-z0-9+/=]+", "base64://<hidden>", message)
    message = re.sub(
        r"(?i)\b(cookie|authorization)\b['\"]?\s*[:=]\s*[^\r\n]+",
        r"\1=<hidden>",
        message,
    )
    message = re.sub(
        r"(?i)\b(SESSDATA|bili_jct|refresh_token|access_token)\b['\"]?\s*[:=]\s*[^;,\s'\"}]+",
        r"\1=<hidden>",
        message,
    )
    return " ".join(message.split())[:500]


def local_file_unreadable(exc):
    if isinstance(exc, (PermissionError, FileNotFoundError)):
        return True
    detail = str(exc).lower()
    return any(
        code in detail
        for code in ("eacces", "enoent", "no such file", "file not found", "文件不存在")
    ) and any(
        marker in detail
        for marker in ("realpath", "open", "file", "path", "文件", "路径")
    )


class InlineVideo(Video):
    """Send native OneBot video bytes without the SDK replacing them with a callback."""

    async def to_dict(self):
        return {"type": "video", "data": {"file": self.file}}


class BiliCard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = StarTools.get_data_dir("astrbot_plugin_bilicard")
        self.vault = CredentialVault(self.root)
        try:
            cookie = self.vault.load()
        except BiliError:
            cookie = ""
            logger.warning("BiliCard: 本地凭据无法读取，请重新扫码。")
        self.client = BiliClient(
            cookie=cookie, timeout=config.get("request_timeout", 12)
        )
        self.cards = CardRenderer(self.root, str(config.get("browser_path", "")))
        self.auth = QRLogin(self.client)
        self.videos = VideoDownloader(
            self.root,
            self.client,
            max_size_mb=config.get("max_video_size_mb", 100),
            timeout=config.get("video_timeout", 300),
            ffmpeg_path=str(config.get("ffmpeg_path", "")),
        )
        self.gate = asyncio.Semaphore(2)
        self.inflight = set()
        self.recent = OrderedDict()
        self.login_lock = asyncio.Lock()
        self.login_task = None

    async def _notify(self, origin, text):
        try:
            await self.context.send_message(origin, MessageChain().message(text))
        except Exception:
            logger.warning("BiliCard: 扫码登录状态通知发送失败。")

    async def _cancel_login(self):
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.login_task
        self.login_task = None

    async def _watch_login(self, key, origin, image_path):
        scanned = False
        deadline = time.monotonic() + 180
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                state, cookie = await self.auth.poll(key)
                if state == "success":
                    self.vault.save(cookie)
                    self.client.set_cookie(cookie)
                    await self._notify(
                        origin,
                        "B 站扫码登录成功，凭据已加密保存在本机，后续解析将自动使用。",
                    )
                    return
                if state == "expired":
                    break
                if state == "scanned" and not scanned:
                    scanned = True
                    await self._notify(origin, "已扫码，请在哔哩哔哩 App 中确认登录。")
            await self._notify(origin, "二维码已过期，请发送 /bili 登录 重新获取。")
        except asyncio.CancelledError:
            raise
        except BiliError as exc:
            await self._notify(origin, str(exc))
        except Exception:
            await self._notify(
                origin, "登录凭据保存失败，请检查插件数据目录权限后重新扫码。"
            )
        finally:
            Path(image_path).unlink(missing_ok=True)

    async def _account(self, event, action):
        if not event.is_private_chat() or not event.is_admin():
            return "请由 AstrBot 管理员私聊机器人使用登录功能。", None
        async with self.login_lock:
            if action in {"退出登录", "注销"}:
                await self._cancel_login()
                self.vault.clear()
                self.client.set_cookie("")
                return "已清除本插件保存的 B 站凭据，并取消正在进行的扫码登录。", None
            if action in {"登录状态", "状态"}:
                if not self.client.cookie:
                    return "尚未登录。发送 /bili 登录 获取二维码。", None
                response = await self.client._json("/x/web-interface/nav")
                data = response.get("data") or {}
                if response.get("code") == 0 and data.get("isLogin"):
                    return f"B 站已登录：{data.get('uname') or '哔哩哔哩用户'}。", None
                return "登录凭据已失效，请发送 /bili 登录 重新扫码。", None
            await self._cancel_login()
            url, key = await self.auth.generate()
            image_path = self.root / f"login-{uuid.uuid4().hex}.png"
            self.auth.make_image(url, image_path)
            self.login_task = asyncio.create_task(
                self._watch_login(key, event.unified_msg_origin, image_path),
                name="bilicard-qr-login",
            )
            return (
                "请用哔哩哔哩 App 扫描二维码并确认登录，约 3 分钟内有效。此账号将供本机器人所有会话的视频解析使用。",
                str(image_path),
            )

    @staticmethod
    def _video_text(event, text):
        # Direct links and failure details must remain copyable even when global T2I is on.
        return event.plain_result(text).use_t2i(False)

    async def _prepare_video(self, path, mode_override=None):
        get_config = getattr(self.context, "get_config", None)
        config = (get_config() or {}) if get_config else {}
        mode = (
            str(mode_override or self.config.get("video_send_mode", "auto"))
            .strip()
            .lower()
        )
        if mode not in {"auto", "local", "base64", "callback"}:
            mode = "auto"
        if mode == "auto":
            if config.get("callback_api_base"):
                mode = "callback"
            else:
                mode = "local"
        if mode == "base64":
            if Path(path).stat().st_size > 64 * 1024 * 1024:
                raise BiliError(
                    "跨容器 Base64 视频发送上限为 64 MiB；较大视频请配置文件回调，或共享目录并选择 local 模式。"
                )
            encoded = await asyncio.to_thread(
                lambda: base64.b64encode(Path(path).read_bytes()).decode("ascii")
            )
            return InlineVideo(file="base64://" + encoded), "base64"
        component = Video.fromFileSystem(path)
        if mode == "callback":
            if not config.get("callback_api_base"):
                raise BiliError(
                    "请先在 AstrBot 配置 callback_api_base 为协议端可访问的文件回调地址。"
                )
            # This helper registers the resolved local path, avoiding older SDKs registering a file URI.
            return Video.fromURL(await component.register_to_file_service()), "callback"
        return component, "file_uri"

    async def _video_reply(self, event, video, stream):
        quality = str(self.config.get("video_quality", "720P")).upper()
        limit = max(0, min(86400, int(self.config.get("max_video_duration", 300))))
        duration = part_duration(video)
        duration = max(duration, float(stream.get("duration") or 0))
        if limit > 0 and duration > limit:
            yield self._video_text(
                event,
                direct_links(
                    stream,
                    f"视频时长 {format_duration(duration)}（{duration:g} 秒），超过配置上限 {limit} 秒，不下载。",
                ),
            )
            return
        if duration <= 0:
            yield self._video_text(
                event, direct_links(stream, "无法确认视频时长，本次仅提供直链。")
            )
            return
        note = f"正在下载视频，实际画质 {stream['quality']}。"
        if stream["quality"] != quality:
            note += f"（设置为 {quality}，当前可用画质较低；更高画质可能需要登录或会员权限。）"
        yield self._video_text(event, note)
        trace = uuid.uuid4().hex[:8]
        started = time.monotonic()
        stage, path = "download", None
        logger.info(
            "BiliCard[%s]: video_download_start bvid=%s quality=%s duration=%.3fs",
            trace,
            video.get("bvid", ""),
            stream["quality"],
            duration,
        )
        try:
            async with self.videos.download(stream) as path:
                logger.info(
                    "BiliCard[%s]: video_ready bytes=%d path=%s elapsed=%.2fs",
                    trace,
                    Path(path).stat().st_size,
                    path,
                    time.monotonic() - started,
                )
                stage = "prepare"
                component, mode = await self._prepare_video(path)
                stage = "send"
                logger.info("BiliCard[%s]: video_send_start mode=%s", trace, mode)
                # Await the platform upload before the download context deletes the file.
                try:
                    await event.send(MessageChain(chain=[component]))
                except Exception as exc:
                    configured = (
                        str(self.config.get("video_send_mode", "auto")).strip().lower()
                    )
                    automatic = configured not in {"local", "base64", "callback"}
                    if not (
                        automatic
                        and mode == "file_uri"
                        and event.get_platform_name() == "aiocqhttp"
                        and local_file_unreadable(exc)
                    ):
                        raise
                    logger.warning(
                        "BiliCard[%s]: video_send_fallback from=file_uri to=base64 reason=%s",
                        trace,
                        safe_error(exc),
                    )
                    stage = "prepare"
                    component, mode = await self._prepare_video(
                        path, mode_override="base64"
                    )
                    stage = "send"
                    logger.info("BiliCard[%s]: video_send_start mode=%s", trace, mode)
                    await event.send(MessageChain(chain=[component]))
                logger.info(
                    "BiliCard[%s]: video_send_returned elapsed=%.2fs",
                    trace,
                    time.monotonic() - started,
                )
            stage = "complete"
        except Exception as exc:
            detail = safe_error(exc)
            logger.warning(
                "BiliCard[%s]: video_failed stage=%s error=%s: %s",
                trace,
                stage,
                type(exc).__name__,
                detail,
            )
            label = {
                "download": "下载或合成",
                "prepare": "视频消息准备",
                "send": "视频上传",
            }.get(stage, "视频处理")
            reason = (
                f"{label}失败（{type(exc).__name__}：{detail}）。诊断编号：{trace}。"
            )
            yield self._video_text(event, direct_links(stream, reason))
        finally:
            logger.info(
                "BiliCard[%s]: video_job_finished stage=%s local_file_removed=%s elapsed=%.2fs",
                trace,
                stage,
                not Path(path).exists() if path else True,
                time.monotonic() - started,
            )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        components = event.get_messages()
        mentioned = any(
            isinstance(item, At) and str(item.qq) == str(event.get_self_id())
            for item in components
        )
        plain = event.get_message_str().strip()
        woken = bool(event.is_at_or_wake_command)
        command = re.match(r"^([/!]?bili)(?:\s+(.*))?$", plain, re.I)
        if command and (
            plain.startswith(("/", "!"))
            or mentioned
            or woken
            or event.is_private_chat()
        ):
            action = (command.group(2) or "").strip()
            if action in {"登录", "登录状态", "状态", "退出登录", "注销"}:
                try:
                    message, image = await self._account(event, action)
                    yield event.plain_result(message)
                    if image:
                        yield event.image_result(image)
                except BiliError as exc:
                    yield event.plain_result(str(exc))
                except Exception:
                    yield event.plain_result(
                        "登录操作暂时失败，请检查网络和插件数据目录权限后重试。"
                    )
                event.stop_event()
                return
            if not action or action in {"帮助", "help"}:
                yield event.plain_result(HELP)
                event.stop_event()
                return
        explicit_download = bool(
            re.match(
                r"^(?:[/!]?bili\s+)?[/!]?下载(?=\s|BV|https?://|b23\.|$)", plain, re.I
            )
        ) and (
            mentioned
            or woken
            or event.is_private_chat()
            or plain.startswith(("/bili", "!bili", "/下载", "!下载"))
        )
        outer_mode = trigger_mode(
            plain, mentioned, event.is_private_chat(), False, woken
        )
        automatic = bool(self.config.get("auto_detect", False))
        if not (automatic or mentioned or woken or outer_mode or explicit_download):
            return
        target = await message_target(event, logger=logger)
        if not target:
            if outer_mode or explicit_download:
                yield event.plain_result(HELP)
                event.stop_event()
            return
        mode = (
            "manual"
            if mentioned or woken or outer_mode or explicit_download
            else "auto"
        )
        key = (event.unified_msg_origin, target)
        cooldown = max(0, min(600, int(self.config.get("cooldown_seconds", 60))))
        if mode == "auto" and (
            key in self.inflight
            or time.monotonic() - self.recent.get(key, -float("inf")) < cooldown
        ):
            return
        claimed = set()
        if mode == "auto":
            self.inflight.add(key)
            claimed.add(key)
        try:
            async with self.gate:
                target = await self.client.resolve(target)
                video = await self.client.video(target)
                canonical = (event.unified_msg_origin, video["bvid"])
                if (
                    mode == "auto"
                    and time.monotonic() - self.recent.get(canonical, -float("inf"))
                    < cooldown
                ):
                    return
                if mode == "auto" and canonical != key:
                    if canonical in self.inflight:
                        return
                    self.inflight.add(canonical)
                    claimed.add(canonical)
                want_video = explicit_download or bool(
                    self.config.get("send_video", False)
                )
                duration_limit = max(
                    0, min(86400, int(self.config.get("max_video_duration", 300)))
                )
                over_limit = (
                    duration_limit > 0 and part_duration(video) > duration_limit
                )
                stream = None
                if want_video:
                    stream = await self.client.playback(
                        video,
                        str(self.config.get("video_quality", "720P")).upper(),
                        direct_only=over_limit,
                    )
                    over_limit = (
                        duration_limit > 0
                        and max(
                            part_duration(video), float(stream.get("duration") or 0)
                        )
                        > duration_limit
                    )
                if not explicit_download and not (want_video and over_limit):
                    count = max(0, min(5, int(self.config.get("comments_count", 3))))
                    comments, status = (
                        await self.client.comments(video["aid"])
                        if count
                        else ([], "ok")
                    )
                    card = build_card(video, comments, count, status)
                    if self.config.get("audio_summary", False):
                        yield self._video_text(
                            event, "正在提取音频并生成 AI 总结，请稍候。"
                        )
                        try:
                            card["summary"] = await AudioSummarizer(
                                self.context, self.config, self.client, self.videos
                            ).summarize(video, event.unified_msg_origin)
                        except BiliError as exc:
                            card["summary_notice"] = str(exc)
                        except Exception as exc:
                            logger.warning(
                                "BiliCard: audio_summary_failed type=%s",
                                type(exc).__name__,
                            )
                            card["summary_notice"] = (
                                "音频总结暂不可用，请检查模型配置或稍后重试。"
                            )
                    try:
                        card = await self.client.embed_images(card)
                        image = await self.cards.render(card)
                    except Exception:
                        logger.warning(
                            "BiliCard: 图片渲染失败，使用文字回复。请检查浏览器安装和中文字体。"
                        )
                        yield event.plain_result(
                            text_fallback(card) + "\n（图片生成失败，已返回文字信息。）"
                        )
                    else:
                        yield event.image_result(image)
                if want_video:
                    async for reply in self._video_reply(event, video, stream):
                        yield reply
                # Only automatic replies create cooldown; users can explicitly request again.
                if mode == "auto":
                    for item in {key, canonical}:
                        self.recent[item] = time.monotonic()
                        self.recent.move_to_end(item)
                    while len(self.recent) > 512:
                        self.recent.popitem(last=False)
        except BiliError as exc:
            yield event.plain_result(str(exc))
        except Exception:
            logger.warning("BiliCard: 视频解析失败，请检查网络和接口状态。")
            yield event.plain_result("视频暂时无法解析，请稍后重试。")
        finally:
            self.inflight.difference_update(claimed)
            event.stop_event()

    async def terminate(self):
        await self._cancel_login()
        await self.cards.close()
        await self.client.close()
