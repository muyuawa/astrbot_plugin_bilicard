"""Local browser rendering; screenshots never fetch remote resources."""

import asyncio
import time
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


class CardRenderer:
    def __init__(self, root, browser_path=""):
        self.root = Path(root) / "cards"
        self.root.mkdir(parents=True, exist_ok=True)
        self.browser_path = browser_path
        self.env = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=select_autoescape(["html"]),
        )
        self.lock = asyncio.Lock()
        self.playwright = self.browser = None

    def html(self, card):
        return self.env.get_template("card.html").render(**card)

    async def _prepare(self):
        if self.browser and self.browser.is_connected():
            return
        from playwright.async_api import async_playwright

        if self.playwright is None:
            self.playwright = await async_playwright().start()
        options = {"headless": True}
        if self.browser_path:
            options["executable_path"] = self.browser_path
        try:
            self.browser = await self.playwright.chromium.launch(**options)
        except Exception:
            if self.browser_path:
                raise
            # Reuse an installed system browser when Chromium has not been downloaded.
            for channel in ("chrome", "msedge"):
                try:
                    self.browser = await self.playwright.chromium.launch(
                        headless=True, channel=channel
                    )
                    return
                except Exception:
                    continue
            raise RuntimeError(
                "请在 AstrBot 环境安装 Chromium：python -m playwright install chromium"
            ) from None

    async def render(self, card):
        async with self.lock:
            await self._prepare()
            for old in self.root.glob("*.png"):
                if old.stat().st_mtime < time.time() - 3600:
                    old.unlink(missing_ok=True)
            context = await self.browser.new_context(
                viewport={"width": 900, "height": 1200},
                device_scale_factor=1.25,
                locale="zh-CN",
                java_script_enabled=False,
            )
            try:
                page = await context.new_page()
                await page.route("**/*", lambda route: route.abort())
                await page.set_content(
                    self.html(card), wait_until="load", timeout=20000
                )
                await page.evaluate("document.fonts.ready")
                await page.evaluate(
                    "Promise.all(Array.from(document.images, i => i.decode().catch(() => {})))"
                )
                output = self.root / f"{uuid.uuid4().hex}.png"
                await page.screenshot(
                    path=str(output),
                    full_page=True,
                    animations="disabled",
                    timeout=20000,
                )
                return str(output)
            finally:
                await context.close()

    async def close(self):
        async with self.lock:
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
