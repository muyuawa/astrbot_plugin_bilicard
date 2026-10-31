"""QQ share and forward fixtures; no protocol or network credentials."""

import html
import json
import types
import unittest
from unittest.mock import AsyncMock

from astrbot_plugin_bilicard.shares import card_target, message_target


MINIAPP = {
    "app": "com.tencent.miniapp_01",
    "meta": {
        "detail_1": {
            "appid": "1109937557",
            "desc": "示例标题",
            "qqdocurl": "https://b23.tv/abcDEF?share_source=qq",
            "url": "m.q.qq.com/a/s/example",
        }
    },
}


def event(components, responses=None, raw=None):
    return types.SimpleNamespace(
        get_messages=lambda: components,
        get_platform_name=lambda: "aiocqhttp",
        get_message_str=lambda: "",
        bot=types.SimpleNamespace(call_action=AsyncMock(side_effect=responses or [])),
        message_obj=types.SimpleNamespace(raw_message=raw),
    )


class ShareTests(unittest.IsolatedAsyncioTestCase):
    async def test_miniapp_json_escaping_xml_and_link_share(self):
        encoded = json.dumps(MINIAPP, ensure_ascii=False).replace("/", "\\/")
        for data in [MINIAPP, encoded, json.dumps(encoded), html.escape(encoded)]:
            with self.subTest(data_type=type(data).__name__):
                self.assertEqual(card_target(data), "https://b23.tv/abcDEF")
        for segment in [
            {"type": "json", "data": {"data": encoded}},
            {
                "type": "share",
                "data": {"url": "https://www.bilibili.com/video/BV1GJ411x7h7/"},
            },
            {
                "type": "xml",
                "data": {
                    "data": '<msg url="https://www.bilibili.com/video/BV1GJ411x7h7/?a=1&amp;b=2"><item/></msg>'
                },
            },
        ]:
            result = await message_target(event([segment]))
            self.assertEqual(
                result,
                "https://b23.tv/abcDEF"
                if segment["type"] == "json"
                else "BV1GJ411x7h7",
            )

    async def test_cq_and_raw_payload_retain_card_link(self):
        for text in [
            "[视频] BV1GJ411x7h7",
            '"title" BV1GJ411x7h7',
            "{title} BV1GJ411x7h7",
            "<https://www.bilibili.com/video/BV1GJ411x7h7>",
        ]:
            self.assertEqual(
                await message_target(event([{"type": "text", "data": {"text": text}}])),
                "BV1GJ411x7h7",
            )
        encoded = json.dumps(MINIAPP, ensure_ascii=False).replace("/", "\\/")
        encoded = (
            encoded.replace("&", "&amp;")
            .replace(",", "&#44;")
            .replace("[", "&#91;")
            .replace("]", "&#93;")
        )
        raw = {"message": "[CQ:json,data=" + encoded + "]"}
        self.assertEqual(
            await message_target(event([], raw=raw)), "https://b23.tv/abcDEF"
        )

    async def test_card_uses_video_link_and_ignores_title_preview_and_external_url(
        self,
    ):
        for card in [
            {
                "meta": {
                    "news": {
                        "title": "BV1GJ411x7h7",
                        "jumpUrl": "https://evil.test/BV1GJ411x7h7",
                    }
                }
            },
            {
                "title": "BV1GJ411x7h7",
                "preview": "https://i0.hdslb.com/BV1GJ411x7h7.jpg",
            },
            {"url": "https://space.bilibili.com/2"},
        ]:
            self.assertIsNone(card_target(card))
        self.assertEqual(
            card_target(
                {
                    "meta": {
                        "news": {
                            "jumpUrl": "https%3A%2F%2Fwww.bilibili.com%2Fvideo%2FBV1GJ411x7h7"
                        }
                    }
                }
            ),
            "BV1GJ411x7h7",
        )

    async def test_forward_standard_and_snowluma_response_shapes(self):
        segment = {"type": "forward", "data": {"id": "resource-1"}}
        card = {"type": "json", "data": {"data": MINIAPP}}
        for response in [
            {"message": [{"type": "node", "data": {"content": [card]}}]},
            {
                "status": "ok",
                "data": {
                    "messages": [{"sender": {"nickname": "sender"}, "message": [card]}]
                },
            },
            {
                "messages": [
                    {"content": [{"type": "text", "data": {"text": "BV1GJ411x7h7"}}]}
                ]
            },
        ]:
            ev = event([segment], [response])
            result = await message_target(ev)
            self.assertEqual(
                result,
                "BV1GJ411x7h7"
                if "content" in response.get("messages", [{}])[0]
                else "https://b23.tv/abcDEF",
            )
            ev.bot.call_action.assert_awaited_once_with(
                "get_forward_msg", id="resource-1"
            )

    async def test_inline_nodes_cycles_and_failed_forward_do_not_block_following_card(
        self,
    ):
        loop = {"type": "forward", "data": {"id": "loop"}}
        card = {"type": "json", "data": {"data": MINIAPP}}
        ev = event([loop, card], [{"messages": [{"content": [loop]}]}])
        self.assertEqual(await message_target(ev), "https://b23.tv/abcDEF")
        self.assertEqual(ev.bot.call_action.await_count, 1)
        ev = event([loop, card], [RuntimeError("protocol unavailable")])
        self.assertEqual(await message_target(ev), "https://b23.tv/abcDEF")
        ev = event(
            [
                types.SimpleNamespace(
                    type="Nodes",
                    nodes=[types.SimpleNamespace(type="Node", content=[card])],
                )
            ]
        )
        self.assertEqual(await message_target(ev), "https://b23.tv/abcDEF")
        ev.bot.call_action.assert_not_awaited()
        ev = event(
            [{"type": "forward", "data": {"id": str(i)}} for i in range(5)],
            [{"messages": []} for _ in range(3)],
        )
        self.assertIsNone(await message_target(ev))
        self.assertEqual(ev.bot.call_action.await_count, 3)
