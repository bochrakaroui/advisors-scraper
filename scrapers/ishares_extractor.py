"""Download the full iShares ETF workbook."""

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Download, Locator, TimeoutError as PlaywrightTimeoutError, async_playwright


URL = (
    "https://www.ishares.com/uk/individual/en/products/etf-investments"
    "#/?productView=all&pageNumber=1"
    "&sortColumn=totalFundSizeInMillions&sortDirection=desc"
    "&dataView=keyFacts&keyFacts=all"
)

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "ishares" / "ishares_downloads"
TIMEOUT_MS = 60_000


async def click_with_fallback(locator: Locator, label: str) -> None:
    await locator.wait_for(state="visible", timeout=TIMEOUT_MS)
    await locator.scroll_into_view_if_needed()

    try:
        await locator.click(timeout=10_000)
    except Exception as exc:
        print(f"    Normal click failed for {label}: {exc}")
        print(f"    Retrying {label} with force click.")
        await locator.click(timeout=10_000, force=True)


async def find_first_visible_locator(selectors: list[tuple[str, Locator]], timeout_ms: int = 5_000) -> Locator:
    for label, locator in selectors:
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            print(f"    Using locator for {label}.")
            return locator
        except Exception:
            continue

    raise TimeoutError("No visible matching locator found")


async def dismiss_onetrust_overlay(page) -> None:
    print("    Checking for OneTrust overlay ...")

    consent_buttons = [
        ("button#onetrust-accept-btn-handler", page.locator("button#onetrust-accept-btn-handler").first),
        ("button:has-text('Accept All')", page.locator("button:has-text('Accept All')").first),
        ("button:has-text('Allow All')", page.locator("button:has-text('Allow All')").first),
        ("button:has-text('Confirm My Choices')", page.locator("button:has-text('Confirm My Choices')").first),
        ("button:has-text('Save Preferences')", page.locator("button:has-text('Save Preferences')").first),
        ("button.onetrust-close-btn-handler", page.locator("button.onetrust-close-btn-handler").first),
    ]

    for label, locator in consent_buttons:
        try:
            await locator.wait_for(state="visible", timeout=2_000)
            print(f"    Dismissing OneTrust via {label}.")
            await click_with_fallback(locator, label)
            await page.wait_for_timeout(1_000)
            break
        except Exception:
            continue

    overlay_present = await page.locator("#onetrust-consent-sdk, .onetrust-pc-dark-filter").count() > 0
    if overlay_present:
        print("    Removing leftover OneTrust overlay nodes.")
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


def build_output_path(filename_hint: str) -> Path:
    suggested = filename_hint or "ishares_etf_list.xls"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem, ext = os.path.splitext(suggested)
    ext = ext or ".xls"
    return OUTPUT_DIR / f"{stem}_{timestamp}{ext}"


async def save_response_to_disk(response) -> Path:
    content_disposition = response.headers.get("content-disposition", "")
    filename_match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
    if filename_match:
        filename_hint = filename_match.group(1)
    else:
        parsed = urlparse(response.url)
        filename_hint = Path(parsed.path).name or "ishares_etf_list.xls"

    final_path = build_output_path(filename_hint)
    final_path.write_bytes(await response.body())
    return final_path


async def download_etf_list() -> Path:
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
        )
        page = await context.new_page()

        print("[1/5] Navigating to iShares ETF page ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)

        print("[2/5] Looking for T&C accept button ...")
        try:
            accept_btn = page.locator(
                "a[href*='siteEntryPassthrough=true'], "
                "button:has-text('Accept'), "
                "a:has-text('Continue')"
            ).first
            await accept_btn.wait_for(state="visible", timeout=15_000)
            await click_with_fallback(accept_btn, "T&C accept button")
            print("    T&C accepted.")
        except Exception:
            print("    No T&C gate found, continuing.")

        await dismiss_onetrust_overlay(page)

        print("[3/5] Waiting for the product table to render ...")
        await page.wait_for_selector(
            "button:has-text('DOWNLOAD'), [class*='download']",
            timeout=TIMEOUT_MS,
        )
        await page.wait_for_timeout(3_000)
        await dismiss_onetrust_overlay(page)

        print("[4/5] Clicking DOWNLOAD button ...")
        download_btn = await find_first_visible_locator(
            [
                (
                    "screener-download-funds button.download-button",
                    page.locator("screener-download-funds button.download-button").first,
                ),
                (
                    "button.mat-mdc-menu-trigger.download-button",
                    page.locator("button.mat-mdc-menu-trigger.download-button").first,
                ),
                (
                    "button download text in button-content",
                    page.locator("button:has(div.button-content:has-text('DOWNLOAD'))").first,
                ),
                (
                    "button role named DOWNLOAD",
                    page.get_by_role(
                        "button",
                        name=re.compile(r"^\s*download\s*$", re.IGNORECASE),
                    ).first,
                ),
            ],
            timeout_ms=10_000,
        )
        await click_with_fallback(download_btn, "DOWNLOAD button")

        print("     Selecting 'DOWNLOAD ALL FUNDS (XLS)' from dropdown ...")
        await page.wait_for_timeout(1_000)

        all_etfs_option = await find_first_visible_locator(
            [
                (
                    "overlay menuitem DOWNLOAD ALL FUNDS (XLS)",
                    page.locator(
                        "div.cdk-overlay-pane div[role='menu'] "
                        "button[role='menuitem']:has-text('DOWNLOAD ALL FUNDS (XLS)')"
                    ).first,
                ),
                (
                    "overlay button with button-content DOWNLOAD ALL FUNDS (XLS)",
                    page.locator(
                        "div.cdk-overlay-pane button.mat-mdc-menu-item "
                        ":has(div.button-content:has-text('DOWNLOAD ALL FUNDS (XLS)'))"
                    ).first,
                ),
                (
                    "overlay text matching DOWNLOAD ALL FUNDS",
                    page.locator(
                        "div.cdk-overlay-pane text=/download\\s+all\\s+funds\\s*\\(xls\\)/i"
                    ).first,
                ),
                (
                    "menuitem role named DOWNLOAD ALL FUNDS",
                    page.get_by_role(
                        "menuitem",
                        name=re.compile(r"download\s+all\s+funds\s*\(xls\)", re.IGNORECASE),
                    ).first,
                ),
            ],
            timeout_ms=10_000,
        )

        direct_href = await extract_download_href(all_etfs_option)
        if direct_href:
            print(f"     Found export URL: {direct_href}")
            response = await page.goto(direct_href, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            if response is None:
                raise RuntimeError("The export link did not return a response.")

            final_path = await save_response_to_disk(response)
            print(f"[5/5] File saved -> {final_path}")
            await browser.close()
            return final_path

        try:
            async with page.expect_download(timeout=TIMEOUT_MS) as dl_info:
                await click_with_fallback(all_etfs_option, "DOWNLOAD ALL FUNDS (XLS) option")

            download: Download = await dl_info.value
            suggested = download.suggested_filename or "ishares_etf_list.xls"
            final_path = build_output_path(suggested)
            await download.save_as(final_path)
            print(f"[5/5] File saved -> {final_path}")
            await browser.close()
            return final_path
        except PlaywrightTimeoutError:
            print("     No Playwright download event detected, looking for export response ...")

        async with page.expect_response(
            lambda response: (
                response.status == 200
                and (
                    "content-disposition" in response.headers
                    or "excel" in response.headers.get("content-type", "").lower()
                    or "spreadsheet" in response.headers.get("content-type", "").lower()
                    or response.url.lower().endswith((".xls", ".xlsx", ".csv"))
                )
            ),
            timeout=TIMEOUT_MS,
        ) as response_info:
            await click_with_fallback(all_etfs_option, "DOWNLOAD ALL FUNDS (XLS) option")

        response = await response_info.value
        final_path = await save_response_to_disk(response)
        print(f"[5/5] File saved -> {final_path}")

        await browser.close()
        return final_path


if __name__ == "__main__":
    saved = asyncio.run(download_etf_list())
    print(f"\nDone! Open your file at: {saved.resolve()}")
