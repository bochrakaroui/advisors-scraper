"""
Amundi ETF List Downloader
==========================
Downloads the ETF search export file from:
  https://www.amundietf.co.uk/en/professional/etf-products/search

How it works:
  1. Opens the live ETF finder in Playwright
  2. Selects the professional-investor disclaimer path
  3. Clicks the visible Download control
  4. Captures the XLSX blob generated in the browser
  5. Saves the file into ./amundi_downloads

Requirements:
  pip install playwright
  python -m playwright install chromium
"""

import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import Locator, TimeoutError as PlaywrightTimeoutError, async_playwright


URL = "https://www.amundietf.co.uk/en/professional/etf-products/search"
OUTPUT_DIR = Path("./amundi_downloads")
TIMEOUT_MS = 120_000

INIT_SCRIPT = """
(() => {
    window.__amundiExportBlob = null;

    const originalCreateObjectURL = URL.createObjectURL.bind(URL);
    URL.createObjectURL = function (value) {
        if (value instanceof Blob) {
            window.__amundiExportBlob = value;
        }

        return originalCreateObjectURL(value);
    };
})();
"""


def build_output_path() -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"amundi_etf_export_{timestamp}.xlsx"


async def click_with_fallback(locator: Locator, label: str) -> None:
    await locator.wait_for(state="visible", timeout=TIMEOUT_MS)
    await locator.scroll_into_view_if_needed()

    try:
        await locator.click(timeout=10_000)
    except Exception as exc:
        print(f"    Normal click failed for {label}: {exc}")
        print(f"    Retrying {label} with force click.")
        await locator.click(timeout=10_000, force=True)


async def find_first_visible_locator(
    selectors: list[tuple[str, Locator]],
    timeout_ms: int = 5_000,
) -> tuple[str, Locator]:
    for label, locator in selectors:
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            return label, locator
        except Exception:
            continue

    raise TimeoutError("No visible matching locator found")


async def accept_disclaimer(page) -> None:
    disclaimer = page.locator("#pageDisclaimer .modal.show").first
    if not await disclaimer.is_visible():
        print("    No disclaimer gate visible.")
        return

    print("    Accepting professional-investor disclaimer ...")
    professional_btn = page.locator("#pageDisclaimer button[data-profile='INSTIT']").first
    await click_with_fallback(professional_btn, "Professional investor button")
    await page.wait_for_timeout(750)

    confirm_btn = page.locator("#confirmDisclaimer").first
    await click_with_fallback(confirm_btn, "Accept and continue button")
    await page.wait_for_timeout(2_000)

    try:
        await disclaimer.wait_for(state="hidden", timeout=15_000)
    except Exception:
        print("    Disclaimer modal still present, removing overlay nodes.")
        await page.evaluate(
            """
            () => {
                const root = document.querySelector('#pageDisclaimer');
                if (root) {
                    root.remove();
                }

                for (const node of document.querySelectorAll('.modal-backdrop')) {
                    node.remove();
                }

                document.body.classList.remove('modal-open');
                document.body.style.overflow = 'auto';
            }
            """
        )
        await page.wait_for_timeout(500)


async def wait_for_blob_bytes(page) -> bytes | None:
    for _ in range(30):
        ready = await page.evaluate("() => Boolean(window.__amundiExportBlob)")
        if ready:
            raw_bytes = await page.evaluate(
                """
                async () => {
                    const blob = window.__amundiExportBlob;
                    if (!blob) {
                        return null;
                    }

                    return Array.from(new Uint8Array(await blob.arrayBuffer()));
                }
                """
            )
            if raw_bytes:
                return bytes(raw_bytes)

        await page.wait_for_timeout(1_000)

    return None


async def download_amundi_file() -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)

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
            viewport={"width": 1440, "height": 1200},
        )
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()

        print("[1/4] Loading Amundi ETF finder ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(5_000)

        print("[2/4] Passing disclaimer gate ...")
        await accept_disclaimer(page)

        print("[3/4] Finding download control ...")
        label, download_btn = await find_first_visible_locator(
            [
                (
                    "desktop download button",
                    page.locator("button:has(i.amundi-download-icon):has-text('DOWNLOAD')").first,
                ),
                (
                    "results download button",
                    page.locator("button.ResultsTop__DownloadResults").first,
                ),
                (
                    "generic download button",
                    page.get_by_role("button", name="Download").first,
                ),
            ],
            timeout_ms=30_000,
        )
        print(f"    Using locator for {label}.")

        print("[4/4] Triggering export and saving file ...")
        final_path = build_output_path()

        try:
            async with page.expect_download(timeout=15_000) as download_info:
                await download_btn.evaluate("(el) => el.click()")

            download = await download_info.value
            await download.save_as(final_path)
            print(f"    Download event captured -> {final_path}")
        except PlaywrightTimeoutError:
            print("    No direct download event detected, waiting for browser-generated XLSX blob ...")
            blob_bytes = await wait_for_blob_bytes(page)
            if blob_bytes is None:
                raise RuntimeError("Amundi export was triggered, but no XLSX blob was captured.")

            if blob_bytes[:2] != b"PK":
                raise RuntimeError("Captured blob is not a valid XLSX file.")

            final_path.write_bytes(blob_bytes)
            print(f"    Blob captured -> {final_path}")
            print(f"    Size: {len(blob_bytes):,} bytes")

        await browser.close()
        return final_path


if __name__ == "__main__":
    saved = asyncio.run(download_amundi_file())
    print(f"\nDone! Open your file at: {saved.resolve()}")
