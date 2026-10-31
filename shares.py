"""Read QQ cards and bounded forward trees without treating their text as commands."""

import asyncio
import html
import json
import re
from urllib.parse import unquote

from .parser import extract_target

MAX_TEXT = 65536
LINK_KEYS = (
    "qqdocurl",
    "jumpurl",
    "jump_url",
    "url",
    "targeturl",
    "target_url",
    "docurl",
    "link",
    "actiondata",
)
CQ = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")
XML_LINK = re.compile(
    r"\b(?:url|jumpUrl|href|actionData)\s*=\s*[\"']([^\"']+)[\"']", re.I
)


def _decode(value, strict=False):
    if not isinstance(value, str):
        return value
    if len(value) > MAX_TEXT:
        return None
    for _ in range(3):
        value = html.unescape(value).strip()
        structured = value.startswith(("{", '"')) or re.match(r'^\[\s*[\[{"\]]', value)
        if structured and not value.startswith("[CQ:"):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                return None if strict else value.replace("\\/", "/")
            if not isinstance(decoded, str):
                return decoded
            value = decoded
        else:
            break
    return value.replace("\\/", "/")


def card_target(payload):
    """Use actual jump fields, not a video-looking card title or preview image."""
    stack, visited = [(payload, 0)], 0
    while stack and visited < 200:
        value, depth = stack.pop()
        visited += 1
        if depth > 8:
            continue
        value = _decode(value, strict=True)
        if isinstance(value, str):
            if value.startswith("<"):
                if "<!DOCTYPE" in value.upper() or "<!ENTITY" in value.upper():
                    continue
                links = [m.group(1) for m in XML_LINK.finditer(value)]
            else:
                links = [value]
            for link in links:
                if target := extract_target(unquote(html.unescape(link))):
                    return target
        elif isinstance(value, dict):
            keys = {str(key).lower(): item for key, item in value.items()}
            for key in LINK_KEYS:
                link = keys.get(key)
                if isinstance(link, str) and len(link) <= MAX_TEXT:
                    if target := extract_target(
                        unquote(html.unescape(link)).replace("\\/", "/")
                    ):
                        return target
            nested = [item for item in value.values() if isinstance(item, (dict, list))]
            # Some protocol versions wrap the card in one or more JSON strings.
            nested += [
                item
                for key, item in value.items()
                if str(key).lower() in {"data", "content", "json"}
                and isinstance(item, str)
            ]
            stack.extend((item, depth + 1) for item in reversed(nested[:200]))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in reversed(value[:200]))
    return None


class _MessageReader:
    def __init__(self, event, logger=None):
        self.event = event
        self.logger = logger
        self.visited = 0
        self.forward_ids = set()

    async def walk(self, value, depth=0):
        self.visited += 1
        if self.visited > 200 or depth > 16:
            return None
        if isinstance(value, str):
            if len(value) > MAX_TEXT:
                return None
            # Split CQ attributes before unescaping their encoded commas/brackets.
            matches = list(CQ.finditer(value))
            if matches:
                start = 0
                for match in matches[:200]:
                    if target := extract_target(
                        html.unescape(value[start : match.start()])
                    ):
                        return target
                    data = {}
                    for field in match.group(2).lstrip(",").split(","):
                        key, _, item = field.partition("=")
                        data[key] = html.unescape(item)
                    target = await self.walk(
                        {"type": match.group(1), "data": data}, depth + 1
                    )
                    if target:
                        return target
                    start = match.end()
                return extract_target(html.unescape(value[start:]))
            decoded = _decode(value)
            if isinstance(decoded, (dict, list)):
                return await self.walk(decoded, depth + 1)
            if isinstance(decoded, str):
                return (
                    card_target(decoded)
                    if re.match(r"<(?:\?xml\b|msg(?:\s|>))", decoded, re.I)
                    else extract_target(decoded)
                )
            return None
        if isinstance(value, (list, tuple)):
            for item in value[:200]:
                if target := await self.walk(item, depth + 1):
                    return target
            return None
        if not isinstance(value, dict):
            # Read only known data fields; do not invoke component serialization or file properties.
            kind = getattr(value, "type", type(value).__name__)
            kind = str(getattr(kind, "value", kind)).lower()
            fields = {
                "json": ("data",),
                "share": ("url",),
                "unknown": ("text",),
                "plain": ("text",),
                "text": ("text",),
                "forward": ("id", "content"),
                "node": ("content",),
                "nodes": ("nodes",),
                "xml": ("data",),
            }.get(kind, ())
            value = {
                "type": kind,
                "data": {key: getattr(value, key, None) for key in fields},
            }
        kind = str(value.get("type", "")).lower()
        data = value.get("data", value)
        if kind in {"json", "xml"}:
            return card_target(
                data.get("data", data) if isinstance(data, dict) else data
            )
        if kind == "share":
            return card_target(data)
        if kind in {"text", "plain", "unknown"}:
            return await self.walk(
                data.get("text", "") if isinstance(data, dict) else data, depth + 1
            )
        if kind == "forward":
            if not isinstance(data, dict):
                return None
            for key in ("content", "message", "messages"):
                if data.get(key):
                    if target := await self.walk(data[key], depth + 1):
                        return target
            resource = str(data.get("id") or "")
            bot = getattr(self.event, "bot", None)
            if (
                not resource
                or resource in self.forward_ids
                or len(self.forward_ids) >= 3
            ):
                return None
            if not bot or self.event.get_platform_name() != "aiocqhttp":
                return None
            self.forward_ids.add(resource)
            try:
                response = await asyncio.wait_for(
                    bot.call_action("get_forward_msg", id=resource), 5
                )
            except Exception as exc:
                if self.logger:
                    self.logger.warning(
                        "BiliCard: forward_read_failed type=%s", type(exc).__name__
                    )
                return None
            return await self.walk(response, depth + 1)
        if kind in {"node", "nodes", ""} and isinstance(data, dict):
            if "app" in data or "meta" in data:
                return card_target(data)
            for key in (
                "content",
                "message",
                "messages",
                "nodes",
                "data",
                "raw_message",
            ):
                if data.get(key):
                    if target := await self.walk(data[key], depth + 1):
                        return target
        return None


async def message_target(event, logger=None):
    """Read current message only; callers must check outer trigger policy first."""
    reader = _MessageReader(event, logger)
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    try:
        return await asyncio.wait_for(
            reader.walk(
                [
                    event.get_message_str(),
                    event.get_messages(),
                    raw,
                ]
            ),
            10,
        )
    except asyncio.TimeoutError:
        if logger:
            logger.warning("BiliCard: forward_read_timeout")
        return None
