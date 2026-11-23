#!/usr/bin/env python3
"""Regenerate docs/images/webui-*.png from the fictional demo workspace.

    python -m venv /tmp/demo-venv && /tmp/demo-venv/bin/pip install -r dop-agent/requirements.txt playwright pillow
    /tmp/demo-venv/bin/playwright install chromium
    /tmp/demo-venv/bin/python dop-agent/scripts/demo/take_screenshots.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seed_demo  # noqa: E402  (redirects HOME/config to a temp dir on import)

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "images"
MAX_BYTES = 400 * 1024


def _save(page, name: str) -> None:
    path = OUT / name
    page.screenshot(path=str(path))
    img = Image.open(path).convert("RGB")
    img.save(path, optimize=True)
    if path.stat().st_size > MAX_BYTES:
        img.quantize(colors=128, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)
    print(f"{name}: {path.stat().st_size // 1024} KB")


def _open(page, cid, wait_sel=None):
    page.click(f".session-item[data-conversation-id='{cid}']")
    if wait_sel:
        page.wait_for_selector(wait_sel, timeout=10000)
    page.wait_for_timeout(1200)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base, ids, stop = seed_demo.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, bypass_csp=True, color_scheme="dark")
            ctx.add_init_script("try{localStorage.setItem('dop-theme','dark')}catch(e){}")
            page = ctx.new_page()
            page.goto(f"{base}/login")
            page.fill("#password", seed_demo.DEMO_PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{base}/")
            page.wait_for_selector(".session-item", timeout=10000)
            page.wait_for_timeout(1500)

            _open(page, ids["Fix flaky retry in API client"], ".bubble.assistant pre")
            _save(page, "webui-sessions.png")

            _open(page, ids["Checkout service architecture"], ".bubble.assistant svg")
            page.click(".bubble.assistant button:has-text('Fit')")
            page.wait_for_timeout(1000)
            _save(page, "webui-mermaid.png")

            _open(page, ids["Alerting rollout decisions"], ".ask-chip")
            page.click(".ask-chip[data-ask-item=D1]")
            page.wait_for_timeout(500)
            _save(page, "webui-ask.png")
            page.keyboard.press("Escape")

            _open(page, ids["Speed up the nightly build"], ".ideas-widget")
            page.wait_for_timeout(500)
            _save(page, "webui-ideas.png")

            page.click("button.sidebar-mode[data-mode=inbox]")
            page.wait_for_selector("#inbox-list > *", timeout=10000)
            page.locator("#inbox-list > *", has_text="Alerting rollout").first.click()
            page.wait_for_timeout(1200)
            _save(page, "webui-inbox.png")

            page.click("button.sidebar-mode[data-mode=sessions]")
            page.click("#settings-link")
            page.click('#settings-tabs [data-stab="agent"]')
            page.wait_for_timeout(2000)
            # The temp config path is machine-specific; show the documented default instead.
            page.evaluate(
                """(tmp) => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                for (let n; (n = w.nextNode());) if (n.nodeValue.includes(tmp)) n.nodeValue = n.nodeValue.replace(tmp, '~/.dop-agent/config.yaml'); }""",
                str(seed_demo._TMP / "config.yaml"),
            )
            page.wait_for_timeout(300)
            _save(page, "webui-settings.png")
            browser.close()
    finally:
        stop()


if __name__ == "__main__":
    main()
