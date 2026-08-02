from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


def wait_for_text_change(page: Page, selector: str, initial: str) -> None:
    page.wait_for_function(
        "([selector, initial]) => !document.querySelector(selector)?.textContent.includes(initial)",
        arg=[selector, initial],
        timeout=30_000,
    )


def activate(page: Page, tab: str, ready_selector: str, loading_text: str | None = None) -> None:
    page.locator(f'[data-tab="{tab}"]').click()
    page.wait_for_selector(f"#tab-{tab}.active {ready_selector}")
    if loading_text:
        wait_for_text_change(page, ready_selector, loading_text)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(350)


def capture(page: Page, output: Path) -> None:
    output.write_bytes(page.screenshot(full_page=False, animations="disabled"))


def capture_element(page: Page, selector: str, output: Path) -> None:
    output.write_bytes(page.locator(selector).screenshot(animations="disabled"))


def build_screenshots(base_url: str, output_dir: Path, zero_query: str | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1050},
            device_scale_factor=1,
            color_scheme="dark",
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(base_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_function(
            "document.querySelector('#proof-source-count')?.textContent === '10'"
        )
        capture(page, output_dir / "01-home.png")

        activate(page, "config", "#config-save-state", "正在读取")
        capture(page, output_dir / "02-config.png")
        telegram_card = 'article.config-card[data-config-group="telegram"]'
        page.locator(telegram_card).scroll_into_view_if_needed()
        page.wait_for_timeout(350)
        capture_element(page, telegram_card, output_dir / "09-telegram-config.png")
        slack_card = 'article.config-card[data-config-group="slack"]'
        page.locator(slack_card).scroll_into_view_if_needed()
        page.wait_for_timeout(350)
        capture_element(page, slack_card, output_dir / "10-slack-config.png")

        activate(page, "subscriptions", "#scheduler-health", "正在检查")
        capture(page, output_dir / "03-subscriptions.png")
        if page.locator(".subscription-card .view-log").count():
            page.locator(".subscription-card .view-log").first.click()
            page.wait_for_selector(".subscription-card .subscription-log:not(.hidden)")
            page.wait_for_function(
                "!document.querySelector('.subscription-card .subscription-log')?.textContent.includes('正在读取')"
            )
            page.locator(".subscription-card .subscription-log").first.scroll_into_view_if_needed()
            page.wait_for_timeout(350)
            capture(page, output_dir / "11-delivery-outbox.png")

        activate(page, "opportunities", "#opportunity-summary", "正在读取")
        capture(page, output_dir / "04-opportunities.png")

        page.locator('[data-workspace-view="buyers"]').click()
        page.wait_for_selector("#buyer-radar-view:not(.hidden)")
        wait_for_text_change(page, "#buyer-radar-summary", "正在读取")
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(350)
        capture(page, output_dir / "05-buyers.png")

        activate(page, "decision", "#profile-fields")
        page.wait_for_selector("#profile-fields:not([disabled])", timeout=30_000)
        capture(page, output_dir / "06-decision.png")

        activate(page, "sources", "#source-summary", "正在读取")
        capture(page, output_dir / "07-sources.png")

        if zero_query:
            activate(page, "search", "#query-input")
            page.locator("#query-input").fill(zero_query)
            page.locator("#run-button").click()
            page.wait_for_selector("#results-panel:not(.hidden)", timeout=300_000)
            page.wait_for_function(
                "!document.querySelector('#run-button')?.disabled",
                timeout=300_000,
            )
            summary = page.locator("#result-summary").inner_text()
            if not summary.startswith("0 条可信结果"):
                raise RuntimeError(f"零结果截图查询意外返回结果：{summary}")
            page.locator("#results-panel").scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            capture(page, output_dir / "08-zero-results.png")

        context.close()
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="从正在运行的 BidPilot 捕获 v0.8.0 手册截图")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/manuals/assets"))
    parser.add_argument("--zero-query", default=None)
    args = parser.parse_args()
    build_screenshots(args.base_url, args.output_dir.resolve(), args.zero_query)
    print(args.output_dir.resolve())


if __name__ == "__main__":
    main()
