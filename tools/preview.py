"""Render a real video as a standalone PNG and HTML without starting AstrBot."""

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_bilicard.client import BiliClient  # noqa: E402
from astrbot_plugin_bilicard.models import build_card  # noqa: E402
from astrbot_plugin_bilicard.parser import extract_target  # noqa: E402
from astrbot_plugin_bilicard.renderer import CardRenderer  # noqa: E402


async def preview(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        client = BiliClient()
        renderer = CardRenderer(root, args.browser)
        try:
            target = extract_target(args.video)
            if not target:
                raise ValueError("请提供 BV 号或 B 站视频链接")
            video = await client.video(await client.resolve(target))
            comments, status = await client.comments(video["aid"])
            card = await client.embed_images(build_card(video, comments, 3, status))
            screenshot = await renderer.render(card)
            shutil.copyfile(screenshot, output)
            output.with_suffix(".html").write_text(
                renderer.html(card), encoding="utf-8"
            )
            print(
                f"Preview: {output}; comments: {len(card['comments'])}; status: {status}"
            )
        finally:
            await renderer.close()
            await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", default="BV1GJ411x7h7")
    parser.add_argument("--output", default="preview-output/bilicard.png")
    parser.add_argument("--browser", default="")
    asyncio.run(preview(parser.parse_args()))
