"""Probe: open each Flow profile and print credit-related text visible on the page."""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402

from core.flow_runner import list_flow_profiles  # noqa: E402


async def main() -> None:
    names = sys.argv[1:]
    async with async_playwright() as pw:
        for prof in list_flow_profiles():
            if names and prof.name not in names:
                continue
            print(f"=== profile {prof.name} url={prof.project_url}")
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(prof.profile_dir),
                channel="chrome",
                headless=False,
                no_viewport=True,
                chromium_sandbox=True,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    await page.goto(prof.project_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:
                    print("goto error:", str(exc).splitlines()[0])
                await asyncio.sleep(8)
                print("final url:", page.url)
                body = await page.evaluate("document.body.innerText")
                for line in body.splitlines():
                    if re.search(r"credit|tín dụng|limit|plan|ultra|pro\b", line, re.I):
                        print("  TEXT:", line.strip()[:160])
                labels = await page.evaluate(
                    "Array.from(document.querySelectorAll('[aria-label],[title]'))"
                    ".map(e=>e.getAttribute('aria-label')||e.getAttribute('title'))"
                    ".filter(t=>/credit|account|plan/i.test(t))"
                )
                for t in labels[:20]:
                    print("  LABEL:", t[:160])
            finally:
                await ctx.close()


asyncio.run(main())
