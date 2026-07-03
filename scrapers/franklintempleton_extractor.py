"""Download the full Franklin Templeton ETF workbook."""

import asyncio
import os
import shutil
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Download, Locator, TimeoutError as PlaywrightTimeoutError, async_playwright

try:
    from scrapers.tls_compat import browser_launch_args, context_https_kwargs
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from tls_compat import browser_launch_args, context_https_kwargs


URL = "https://www.franklintempleton.co.uk/download-the-complete-range-of-etfs-with-all-data"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "franklintempleton"
TIMEOUT_MS = 60_000
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"


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
    selectors: list[tuple[str, Locator]], timeout_ms: int = 5_000
) -> Locator:
    for label, locator in selectors:
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            print(f"    Using locator for {label}.")
            return locator
        except Exception:
            continue

    raise TimeoutError("No visible matching locator found")


async def dismiss_consent_overlay(page) -> None:
    """Handle OneTrust and any other cookie/consent banners."""
    print("    Checking for consent overlay ...")

    consent_buttons = [
        ("button#onetrust-accept-btn-handler", page.locator("button#onetrust-accept-btn-handler").first),
        ("button:has-text('Accept All')",        page.locator("button:has-text('Accept All')").first),
        ("button:has-text('Allow All')",         page.locator("button:has-text('Allow All')").first),
        ("button:has-text('Accept all cookies')", page.locator("button:has-text('Accept all cookies')").first),
        ("button:has-text('Confirm My Choices')", page.locator("button:has-text('Confirm My Choices')").first),
        ("button.onetrust-close-btn-handler",    page.locator("button.onetrust-close-btn-handler").first),
    ]

    for label, locator in consent_buttons:
        try:
            await locator.wait_for(state="visible", timeout=2_000)
            print(f"    Dismissing consent via {label}.")
            await click_with_fallback(locator, label)
            await page.wait_for_timeout(1_000)
            break
        except Exception:
            continue

    overlay_present = await page.locator("#onetrust-consent-sdk, .onetrust-pc-dark-filter").count() > 0
    if overlay_present:
        print("    Removing leftover consent overlay nodes.")
        await page.evaluate(
            """
            () => {
                const selectors = [
                    '#onetrust-consent-sdk',
                    '.onetrust-pc-dark-filter',
                    '.onetrust-pc-sdk',
                    '.onetrust-banner-sdk',
                    '.ot-sdk-container',
                    '.ot-sdk-row',
                ];
                for (const selector of selectors) {
                    for (const node of document.querySelectorAll(selector)) {
                        node.remove();
                    }
                }
                document.body.style.overflow = 'auto';
            }
            """
        )
        await page.wait_for_timeout(500)


async def dismiss_jquery_ui_overlay(page) -> bool:
    """
    Dismiss any jQuery UI dialog / modal overlay (.ui-widget-overlay).

    These overlays block pointer events on the entire page.  Franklin Templeton
    uses one for their investor-type / disclaimer dialog.  We try three things
    in order:
      1. Click any visible 'confirm / accept / continue' button inside the dialog.
      2. If the dialog has a close (×) button, click that.
      3. As a last resort, forcibly remove the overlay and dialog from the DOM
         so that normal clicks on the page can proceed.

    Returns True if an overlay was found and dealt with, False otherwise.
    """
    overlay = page.locator("div.ui-widget-overlay.ui-front")
    try:
        await overlay.wait_for(state="visible", timeout=3_000)
    except Exception:
        return False  # No jQuery UI overlay present

    print("    jQuery UI overlay detected — attempting to dismiss ...")

    # 1. Try buttons inside the visible jQuery UI dialog
    dialog_btn_candidates = [
        page.locator(".ui-dialog button:has-text('Confirm')").first,
        page.locator(".ui-dialog button:has-text('Accept')").first,
        page.locator(".ui-dialog button:has-text('Continue')").first,
        page.locator(".ui-dialog button:has-text('I confirm')").first,
        page.locator(".ui-dialog button:has-text('I Agree')").first,
        page.locator(".ui-dialog button:has-text('Agree')").first,
        page.locator(".ui-dialog button:has-text('OK')").first,
        page.locator(".ui-dialog button:has-text('Close')").first,
        # Generic primary / submit buttons
        page.locator(".ui-dialog .ui-button").first,
        page.locator(".ui-dialog button[type='submit']").first,
        # jQuery UI dialog close icon (top-right ×)
        page.locator(".ui-dialog-titlebar-close").first,
    ]

    for btn in dialog_btn_candidates:
        try:
            await btn.wait_for(state="visible", timeout=1_500)
            label = await btn.inner_text()
            print(f"    Clicking dialog button: '{label.strip()}'")
            await btn.click(timeout=5_000)
            await page.wait_for_timeout(1_000)
            # Check if the overlay is gone
            if await page.locator("div.ui-widget-overlay.ui-front").count() == 0:
                print("    jQuery UI overlay dismissed successfully.")
                return True
        except Exception:
            continue

    # 2. Fallback: nuke the overlay + dialog from the DOM
    print("    Could not click dialog button — removing overlay from DOM.")
    await page.evaluate(
        """
        () => {
            // Remove the backdrop
            for (const el of document.querySelectorAll('.ui-widget-overlay, .ui-front')) {
                el.remove();
            }
            // Remove any open jQuery UI dialog
            for (const el of document.querySelectorAll('.ui-dialog')) {
                el.remove();
            }
            // Re-enable body scrolling / pointer events (jQuery UI sets overflow:hidden)
            document.body.style.overflow = 'auto';
            document.body.style.pointerEvents = 'auto';
            // Also clear any inline z-index trickery on body
            document.documentElement.style.overflow = 'auto';
        }
        """
    )
    await page.wait_for_timeout(500)
    print("    jQuery UI overlay removed from DOM.")
    return True


async def extract_download_href(locator: Locator) -> str | None:
    script = """
    (element) => {
        const candidates = [
            element,
            element.closest('a'),
            element.querySelector('a'),
            element.parentElement,
            element.parentElement ? element.parentElement.closest('a') : null,
        ].filter(Boolean);

        for (const node of candidates) {
            const href = node.getAttribute('href') || node.href || node.getAttribute('data-href');
            if (href && !href.toLowerCase().startsWith('javascript:')) {
                return href;
            }
        }
        return null;
    }
    """
    return await locator.evaluate(script)


def build_run_output_dir(base_dir: Path) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        run_date = datetime.now().strftime("%Y-%m-%d")
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_output_path(filename_hint: str) -> Path:
    suggested = filename_hint or "franklin_templeton_etf_list.xlsx"
    dated_output_dir = build_run_output_dir(OUTPUT_DIR)
    stem, ext = os.path.splitext(suggested)
    ext = ext or ".xlsx"
    return dated_output_dir / f"{stem}{ext}"


async def save_response_to_disk(response) -> Path:
    content_disposition = response.headers.get("content-disposition", "")
    filename_match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
    if filename_match:
        filename_hint = filename_match.group(1)
    else:
        parsed = urlparse(response.url)
        filename_hint = Path(parsed.path).name or "franklin_templeton_etf_list.xlsx"

    final_path = build_output_path(filename_hint)
    final_path.write_bytes(await response.body())
    return final_path


async def download_etf_list() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=browser_launch_args("--no-sandbox", "--disable-dev-shm-usage"),
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
            **context_https_kwargs(),
        )
        page = await context.new_page()

        print("[1/4] Navigating to Franklin Templeton ETF download page ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(3_000)

        await dismiss_consent_overlay(page)

        print("[2/4] Checking for investor-type gate ...")
        # Franklin Templeton UK may present a suitability / investor-type modal
        try:
            gate_btn = page.locator(
                "button:has-text('Individual Investor'), "
                "button:has-text('Retail Investor'), "
                "a:has-text('Individual Investor'), "
                "button:has-text('I confirm'), "
                "button:has-text('Continue')"
            ).first
            await gate_btn.wait_for(state="visible", timeout=5_000)
            await click_with_fallback(gate_btn, "investor-type gate button")
            print("    Investor-type gate dismissed.")
            await page.wait_for_timeout(2_000)
            await dismiss_consent_overlay(page)
        except Exception:
            print("    No investor-type gate found, continuing.")

        # ------------------------------------------------------------------ #
        # NEW: dismiss any jQuery UI overlay that may still be blocking input #
        # ------------------------------------------------------------------ #
        await dismiss_jquery_ui_overlay(page)

        print("[3/4] Waiting for the ETF table and 'Download all' button ...")
        await page.wait_for_selector(
            "button.ft__btn--has-icon, button.ft__btn--secondary, button:has-text('Download all')",
            timeout=TIMEOUT_MS,
        )
        await page.wait_for_timeout(2_000)
        await dismiss_consent_overlay(page)

        # Check again — the table load sometimes triggers another overlay
        await dismiss_jquery_ui_overlay(page)

        download_btn = await find_first_visible_locator(
            [
                (
                    "ft__btn--has-icon + text 'Download all'",
                    page.locator("button.ft__btn--has-icon:has-text('Download all')").first,
                ),
                (
                    "ft__btn--secondary + text 'Download all'",
                    page.locator("button.ft__btn--secondary:has-text('Download all')").first,
                ),
                (
                    "data-di-id button",
                    page.locator("button[data-di-id]:has-text('Download all')").first,
                ),
                (
                    "button role named 'Download all'",
                    page.get_by_role(
                        "button",
                        name=re.compile(r"download\s+all", re.IGNORECASE),
                    ).first,
                ),
                (
                    "button:has-text('Download all') fallback",
                    page.locator("button:has-text('Download all')").first,
                ),
            ],
            timeout_ms=15_000,
        )

        # --- Strategy 1: button wraps or lives inside a direct <a href> ---
        direct_href = await extract_download_href(download_btn)
        if direct_href:
            if direct_href.startswith("/"):
                direct_href = "https://www.franklintempleton.co.uk" + direct_href
            print(f"    Found direct export URL: {direct_href}")
            response = await page.goto(direct_href, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            if response is None:
                raise RuntimeError("The export link did not return a response.")
            final_path = await save_response_to_disk(response)
            print(f"[4/4] File saved -> {final_path}")
            await browser.close()
            return final_path

        print("[4/4] Clicking 'Download all' button ...")

        # Final safety net: make absolutely sure no overlay is blocking the button
        await dismiss_jquery_ui_overlay(page)

        # --- Strategy 2: Playwright native download event ---
        try:
            async with page.expect_download(timeout=TIMEOUT_MS) as dl_info:
                await click_with_fallback(download_btn, "'Download all' button")

            download: Download = await dl_info.value
            suggested = download.suggested_filename or "franklin_templeton_etf_list.xlsx"
            final_path = build_output_path(suggested)
            await download.save_as(final_path)
            print(f"    File saved -> {final_path}")
            await browser.close()
            return final_path
        except PlaywrightTimeoutError:
            print("    No Playwright download event — intercepting network response ...")

        # --- Strategy 3: intercept the XHR/fetch response carrying the file ---
        async with page.expect_response(
            lambda r: (
                r.status == 200
                and (
                    "content-disposition" in r.headers
                    or "excel" in r.headers.get("content-type", "").lower()
                    or "spreadsheet" in r.headers.get("content-type", "").lower()
                    or "csv" in r.headers.get("content-type", "").lower()
                    or r.url.lower().endswith((".xls", ".xlsx", ".csv"))
                )
            ),
            timeout=TIMEOUT_MS,
        ) as response_info:
            await click_with_fallback(download_btn, "'Download all' button")

        response = await response_info.value
        final_path = await save_response_to_disk(response)
        print(f"    File saved -> {final_path}")

        await browser.close()
        return final_path


if __name__ == "__main__":
    saved = asyncio.run(download_etf_list())
    print(f"\nDone! Open your file at: {saved.resolve()}")
