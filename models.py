"""Convert Bilibili responses into bounded, display-ready card data."""

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def format_count(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "--"
    if number < 0:
        return "--"
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}".rstrip("0").rstrip(".") + "亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
    return str(number)


def format_duration(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"
    )


def stamp(value) -> str:
    try:
        return datetime.fromtimestamp(int(value), CST).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "时间未知"


def shorten(value, size):
    text = str(value or "").strip()
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def build_card(video: dict, comments: list, limit=3, comment_status="ok") -> dict:
    dimension = video.get("dimension") or {}
    width, height = int(dimension.get("width") or 0), int(dimension.get("height") or 0)
    # Source dimensions are not evidence of a particular frame rate or stream entitlement.
    edge = min(width, height)
    quality = (
        "4K"
        if edge >= 2160
        else "2K"
        if edge >= 1440
        else "1080P"
        if edge >= 1080
        else "720P"
        if edge >= 720
        else f"{edge}P"
        if edge
        else "视频"
    )
    owner = video.get("owner") or {}
    stat = video.get("stat") or {}
    labels = [
        ("view", "播放", "play", "purple"),
        ("reply", "评论", "chat", "blue"),
        ("like", "点赞", "like", "pink"),
        ("coin", "投币", "coin", "orange"),
        ("favorite", "收藏", "star", "amber"),
        ("share", "分享", "share", "green"),
    ]
    result = {
        "bvid": video.get("bvid", ""),
        "url": f"https://www.bilibili.com/video/{video.get('bvid', '')}",
        "title": shorten(video.get("title") or "未命名视频", 240),
        "description": shorten(video.get("desc") or "UP 主还没有填写视频简介。", 800),
        "summary": "",
        "summary_notice": "",
        "published": stamp(video.get("pubdate")),
        "duration": format_duration(video.get("duration")),
        "quality": quality,
        "cover": video.get("pic", ""),
        "owner": {
            "name": shorten(owner.get("name") or "未知 UP 主", 40),
            "avatar": owner.get("face", ""),
            "url": f"https://space.bilibili.com/{owner.get('mid', 0)}",
        },
        "stats": [
            {
                "label": label,
                "value": format_count(stat.get(key)),
                "icon": icon,
                "color": color,
            }
            for key, label, icon, color in labels
        ],
        "comments": [],
        "show_comments": limit > 0,
        "comment_notice": "评论暂时无法读取，可稍后重试或请管理员扫码登录。"
        if comment_status == "unavailable"
        else "暂无热门评论，或 UP 主已关闭评论。",
    }
    for comment in comments[: max(0, min(int(limit), 5))]:
        member = comment.get("member") or {}
        result["comments"].append(
            {
                "name": shorten(member.get("uname") or "哔哩哔哩用户", 32),
                "avatar": member.get("avatar", ""),
                "level": max(
                    0,
                    min(
                        6,
                        int((member.get("level_info") or {}).get("current_level") or 0),
                    ),
                ),
                "content": shorten((comment.get("content") or {}).get("message"), 300),
                "time": stamp(comment.get("ctime")),
                "likes": format_count(comment.get("like", 0)),
            }
        )
    return result


def text_fallback(card: dict) -> str:
    counts = " · ".join(f"{item['label']} {item['value']}" for item in card["stats"])
    text = f"{card['title']}\nUP 主：{card['owner']['name']}\n{card['published']} 发布 · {card['duration']}\n{counts}\n{card['url']}\n{shorten(card['description'], 240)}"
    if card.get("summary") or card.get("summary_notice"):
        text += "\n\nAI 音频总结\n" + (card.get("summary") or card["summary_notice"])
    return text
