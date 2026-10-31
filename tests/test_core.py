import unittest

from astrbot_plugin_bilicard.parser import extract_target, trigger_mode
from astrbot_plugin_bilicard.models import build_card, format_count, format_duration


class RecognitionTests(unittest.TestCase):
    def test_recognizes_bv_full_and_short_urls(self):
        for text, expected in [
            ("看看 BV1GJ411x7h7", "BV1GJ411x7h7"),
            ("bv1GJ411x7h7", "BV1GJ411x7h7"),
            ("https://www.bilibili.com/video/BV1GJ411x7h7/?p=1", "BV1GJ411x7h7"),
            ("【分享】https://b23.tv/abcDEF。", "https://b23.tv/abcDEF"),
            ("b23.tv/abcDEF", "https://b23.tv/abcDEF"),
            ("https://www.bilibili.com/video/av170001", "av170001"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(extract_target(text), expected)

    def test_rejects_partial_ids_and_nonvideo_urls(self):
        for text in [
            "BV123",
            "xBV1GJ411x7h7y",
            "https://space.bilibili.com/2",
            "https://b23.tv.evil.test/x",
            "https://evil.test/BV1GJ411x7h7",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(extract_target(text))

    def test_manual_and_automatic_policy(self):
        self.assertIsNone(trigger_mode("BV1GJ411x7h7", False, False, False))
        self.assertEqual(trigger_mode("BV1GJ411x7h7", False, False, True), "auto")
        self.assertEqual(
            trigger_mode("解析 BV1GJ411x7h7", True, False, False), "manual"
        )
        self.assertEqual(trigger_mode("BV1GJ411x7h7", True, False, False), "manual")
        self.assertEqual(
            trigger_mode("/bili BV1GJ411x7h7", False, False, False), "manual"
        )
        self.assertEqual(
            trigger_mode("解析 BV1GJ411x7h7", False, True, False), "manual"
        )
        self.assertIsNone(trigger_mode("解析 BV1GJ411x7h7", False, False, False))
        self.assertEqual(trigger_mode("解析", True, False, False), "manual")
        self.assertIsNone(trigger_mode("你好", True, False, False))


class ModelTests(unittest.TestCase):
    def test_human_readable_counts_and_duration(self):
        for value, expected in [
            (0, "0"),
            (9999, "9999"),
            (72000, "7.2万"),
            (10000, "1万"),
            (123000000, "1.2亿"),
            (-1, "--"),
            (None, "--"),
        ]:
            self.assertEqual(format_count(value), expected)
        self.assertEqual(format_duration(263), "04:23")
        self.assertEqual(format_duration(3661), "1:01:01")

    def test_build_card_limits_and_honest_resolution(self):
        video = {
            "bvid": "BV1GJ411x7h7",
            "title": "长" * 400,
            "desc": "文" * 3000,
            "pubdate": 0,
            "duration": 263,
            "dimension": {"width": 3840, "height": 2160},
            "owner": {"mid": 1, "name": "UP"},
            "stat": {"view": 72000},
        }
        comments = [
            {
                "rpid": i,
                "member": {"uname": "用户", "level_info": {"current_level": 6}},
                "content": {"message": "评" * 1000},
                "like": i,
                "ctime": 0,
            }
            for i in range(5)
        ]
        card = build_card(video, comments, 3, "ok")
        self.assertEqual(card["quality"], "4K")
        self.assertNotIn("60", card["quality"])
        self.assertEqual(len(card["stats"]), 6)
        self.assertEqual(card["stats"][0]["value"], "7.2万")
        self.assertEqual(len(card["comments"]), 3)
        self.assertLess(len(card["title"]), 260)
        self.assertLess(len(card["description"]), 1000)
        self.assertLess(len(card["comments"][0]["content"]), 400)
        self.assertEqual(card["url"], "https://www.bilibili.com/video/BV1GJ411x7h7")
        self.assertEqual(card["published"], "1970-01-01 08:00")

    def test_missing_resolution_and_comment_notice(self):
        card = build_card({"bvid": "BV1GJ411x7h7"}, [], 3, "unavailable")
        self.assertEqual(card["quality"], "视频")
        self.assertIn("暂时", card["comment_notice"])


if __name__ == "__main__":
    unittest.main()
