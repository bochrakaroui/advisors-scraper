"""Download the UBS ETF workbook."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import Download, Locator, TimeoutError as PlaywrightTimeoutError, async_playwright


URL = "https://www.ubs.com/uk/en/assetmanagement/funds/etf.html"
BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "UBS" / "UBS_etf_downloads"
TIMEOUT_MS = 120_000


def build_output_path(filename_hint: str | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if filename_hint:
        return OUTPUT_DIR / filename_hint.replace(".xlsx", f"_{timestamp}.xlsx")
    return OUTPUT_DIR / f"UBSFunds_List_{timestamp}.xlsx"


async def click_with_fallback(locator: Locator, label: str) -> None:
    await locator.wait_for(state="visible", timeout=TIMEOUT_MS)
    await locator.scroll_into_view_if_needed()

    try:
        await locator.click(timeout=10_000)
    except Exception as exc:
        print(f"    Normal click failed for {label}: {exc}")
        print(f"    Retrying {label} with force click.")
        try:
            await locator.click(timeout=10_000, force=True)
        except Exception:
            print(f"    Falling back to DOM click for {label}.")
            await locator.evaluate("(element) => element.click()")


async def accept_ubs_context(page) -> None:
    print("    Selecting UBS role context ...")
    await page.locator("input#financialintermediaries--id-1").evaluate(
        """
        (element) => {
            element.checked = true;
            element.setAttribute('checked', 'checked');
            element.dispatchEvent(new Event('click', { bubbles: true }));
            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """
    )
    await page.wait_for_timeout(500)
    await page.locator(".contextdisclaimer__buttonConfirm").first.evaluate("(element) => element.click()")
    await page.wait_for_timeout(8_000)


async def dismiss_cookie_banner(page) -> None:
    for selector in (
        "button[name='senddata']",
        "button:has-text('Agree to all')",
        "button:has-text('Decline all')",
    ):
        try:
            button = page.locator(selector).first
            await button.wait_for(state="visible", timeout=2_500)
            print("    Dismissing cookie banner ...")
            await button.evaluate("(element) => element.click()")
            await page.wait_for_timeout(1_000)
            return
        except Exception:
            continue


async def download_ubs_file() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            viewport={"width": 1440, "height": 1400},
        )
        page = await context.new_page()

        print("[1/4] Loading UBS ETF page ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(3_000)

        print("[2/4] Passing UBS context gate ...")
        await accept_ubs_context(page)
        await dismiss_cookie_banner(page)

        print("[3/4] Waiting for the fund list and download link ...")
        download_link = page.locator("a[download][data-testid='downloadURL']").first
        await download_link.wait_for(state="visible", timeout=TIMEOUT_MS)

        print("[4/4] Downloading workbook ...")
        try:
            async with page.expect_download(timeout=TIMEOUT_MS) as download_info:
                await click_with_fallback(download_link, "Download Excel link")

            download: Download = await download_info.value
            suggested = download.suggested_filename or await download_link.get_attribute("download") or "UBSFunds_List.xlsx"
            final_path = build_output_path(suggested)
            await download.save_as(final_path)
            print(f"    File saved -> {final_path}")
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("UBS Excel download did not start.") from exc

        await browser.close()
        return final_path


if __name__ == "__main__":
    saved = asyncio.run(download_ubs_file())
    print(f"\nDone! Open your file at: {saved.resolve()}")
