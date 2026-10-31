"""Pure message recognition; network resolution lives in client.py."""

import re
from urllib.parse import urlsplit

BV = re.compile(r"(?<![A-Za-z0-9])BV([A-Za-z0-9]{10})(?![A-Za-z0-9])", re.I)
URL = re.compile(
    r"https?://[^\s<>\"'，。！？、）】]+|"
    r"(?<![\w./])(?:(?:www\.|m\.)?bilibili\.com|b23\.tv|bili2233\.cn)/[^\s<>\"'，。！？、）】]+",
    re.I,
)
VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
SHORT_HOSTS = {"b23.tv", "bili2233.cn"}
COMMAND = re.compile(
    r"^(?:[/!]?bili|[/!]?b站解析|[/!]?解析)(?=\s|BV|https?://|b23\.|$)", re.I
)


def video_id_from_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname not in VIDEO_HOSTS or parsed.username or parsed.password:
        return None
    if parsed.port not in (None, 80, 443):
        return None
    match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av\d+)/?", parsed.path, re.I)
    if not match:
        return None
    value = match.group(1)
    return "BV" + value[2:] if value[:2].lower() == "bv" else value.lower()


def extract_target(text: str) -> str | None:
    candidates = []
    for match in URL.finditer(text):
        url = match.group().rstrip(".,;:!?)]}")
        if not url.lower().startswith(("https://", "http://")):
            url = "https://" + url
        try:
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                continue
            if parsed.hostname in SHORT_HOSTS and re.fullmatch(
                r"/[A-Za-z0-9]+/?", parsed.path
            ):
                candidates.append(
                    (match.start(), "https://" + parsed.hostname + parsed.path)
                )
            elif value := video_id_from_url(url):
                candidates.append((match.start(), value))
        except ValueError:
            continue
    # A BV-looking string in an unrelated URL must not be recognized as a bare ID.
    masked = URL.sub(lambda m: " " * len(m.group()), text)
    for match in BV.finditer(masked):
        candidates.append((match.start(), "BV" + match.group(1)))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def trigger_mode(
    text: str, mentioned: bool, private: bool, auto: bool, woken: bool = False
) -> str | None:
    text = text.strip()
    command = COMMAND.match(text)
    if command and (
        mentioned
        or private
        or woken
        or text.lower().startswith(("/bili", "!bili", "/b站解析", "/解析"))
    ):
        return "manual"
    if (mentioned or woken) and extract_target(text):
        return "manual"
    if auto and extract_target(text):
        return "auto"
    return None
