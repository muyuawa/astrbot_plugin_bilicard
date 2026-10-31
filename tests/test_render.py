import tempfile
import unittest

from astrbot_plugin_bilicard.models import build_card
from astrbot_plugin_bilicard.renderer import CardRenderer


class RenderTests(unittest.TestCase):
    def test_html_escapes_untrusted_content_and_has_no_external_resources(self):
        card = build_card(
            {
                "bvid": "BV1GJ411x7h7",
                "title": '<script>alert("x")</script>',
                "desc": '<img src="https://evil.test/a">',
            },
            [],
            3,
            "unavailable",
        )
        with tempfile.TemporaryDirectory() as root:
            html = CardRenderer(root).html(card)
        self.assertNotIn("<script>", html)
        self.assertNotIn('<img src="https://evil.test/a">', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("评论暂时无法读取", html)
        self.assertIn("grid-template-columns: repeat(3", html)
        self.assertNotIn("fonts.googleapis", html)
        self.assertNotIn("弹幕", html)

        card["summary"] = "<script>bad()</script> 音频总结"
        with tempfile.TemporaryDirectory() as root:
            html = CardRenderer(root).html(card)
        self.assertIn("AI 音频总结", html)
        self.assertLess(html.index("视频简介</h2>"), html.index("AI 音频总结"))
        self.assertLess(html.index("AI 音频总结"), html.index("热门评论<span"))
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_missing_media_uses_renderable_placeholders(self):
        card = build_card({"bvid": "BV1GJ411x7h7"}, [], 0)
        with tempfile.TemporaryDirectory() as root:
            html = CardRenderer(root).html(card)
        self.assertNotIn('src=""', html)
        self.assertIn("封面暂不可用", html)
        self.assertNotIn("热门评论", html)
        self.assertNotIn("AI 音频总结", html)
