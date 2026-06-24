"""Download the full VanEck ETF workbook (UK, Excel export)."""

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Download, Locator, TimeoutError as PlaywrightTimeoutError, async_playwright


URL = (
    "https://www.vaneck.com/uk/en/fundlisting/overview/etfs/"
    "?InvType=etf"
    "&AssetClass=di,gl,haa,ree,re,ste,th,cob,emmb,eugb,mu"
    "&Funds=haamf,emmbi"
    "&ShareClass=_none"
    "&TableType=ov"
    "&Sort=name"
    "&SortDesc=true"
)

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "vaneck"
TIMEOUT_MS = 60_000
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as the iShares extractor)
# ---------------------------------------------------------------------------

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


async def dismiss_cookie_overlay(page) -> None:
    """Dismiss common cookie / consent banners that VanEck or OneTrust may show."""
    print("    Checking for cookie/consent overlay ...")

    consent_buttons = [
        # OneTrust variants
        ("button#onetrust-accept-btn-handler", page.locator("button#onetrust-accept-btn-handler").first),
        ("button:has-text('Accept All')", page.locator("button:has-text('Accept All')").first),
        ("button:has-text('Allow All')", page.locator("button:has-text('Allow All')").first),
        ("button:has-text('Confirm My Choices')", page.locator("button:has-text('Confirm My Choices')").first),
        ("button:has-text('Save Preferences')", page.locator("button:has-text('Save Preferences')").first),
        ("button.onetrust-close-btn-handler", page.locator("button.onetrust-close-btn-handler").first),
        # VanEck-specific cookie notice variants
        ("button:has-text('Accept Cookies')", page.locator("button:has-text('Accept Cookies')").first),
        ("button:has-text('I Accept')", page.locator("button:has-text('I Accept')").first),
        (".cookie-accept", page.locator(".cookie-accept").first),
        ("#cookie-accept", page.locator("#cookie-accept").first),
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

    # Force-remove any leftover overlay nodes so they don't block clicks
    overlay_present = (
        await page.locator(
            "#onetrust-consent-sdk, .onetrust-pc-dark-filter, "
            ".cookie-overlay, .cookie-banner, .cookie-notice"
        ).count()
        > 0
    )
    if overlay_present:
        print("    Removing leftover overlay nodes via JS.")
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
                    '.cookie-overlay',
                    '.cookie-banner',
                    '.cookie-notice',
                ];
                for (const sel of selectors) {
                    for (const node of document.querySelectorAll(sel)) {
                        node.remove();
                    }
                }
                document.body.style.overflow = 'auto';
            }
            """
        )
        await page.wait_for_timeout(500)


async def goto_with_retry(page, url: str) -> None:
    last_exc: Exception | None = None
    for wait_until in ("commit", "domcontentloaded"):
        try:
            await page.goto(url, wait_until=wait_until, timeout=TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
            return
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc


# ---------------------------------------------------------------------------
# Output-path helpers
# ---------------------------------------------------------------------------

def build_run_output_dir(base_dir: Path) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    run_date = datetime.now().strftime("%Y-%m-%d")
    output_dir = base_dir / run_date
    suffix = 1
    while output_dir.exists():
        output_dir = base_dir / f"{run_date} ({suffix})"
        suffix += 1
    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name
    return output_dir


def build_output_path(filename_hint: str) -> Path:
    suggested = filename_hint or "vaneck_etf_list.xlsx"
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
        filename_hint = Path(parsed.path).name or "vaneck_etf_list.xlsx"

    final_path = build_output_path(filename_hint)
    final_path.write_bytes(await response.body())
    return final_path


# ---------------------------------------------------------------------------
# VanEck-specific: wait for the fund table to be ready
# ---------------------------------------------------------------------------

async def wait_for_fund_table(page) -> None:
    """Block until the fund-explorer table and the Download Excel link are in the DOM."""
    selectors = [
        "a#excel-download-lint",
        "a.fund-explorer-table__download-excel",
        "a[data-ve-gtm='download-excel']",
        "a:has-text('Download Excel')",
    ]
    last_error: Exception | None = None
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="attached", timeout=20_000)
            print(f"    Fund table ready (found via '{selector}').")
            return
        except Exception as exc:
            last_error = exc

    raise TimeoutError(
        "Could not find the VanEck Download Excel link in the DOM"
    ) from last_error


# ---------------------------------------------------------------------------
# Main download coroutine
# ---------------------------------------------------------------------------

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

        # ------------------------------------------------------------------
        # Step 1 — Navigate
        # ------------------------------------------------------------------
        print("[1/5] Navigating to VanEck ETF listing page ...")
        await goto_with_retry(page, URL)

        # ------------------------------------------------------------------
        # Step 2 — Cookie / T&C gate
        # ------------------------------------------------------------------
        print("[2/5] Checking for cookie / T&C overlay ...")
        await dismiss_cookie_overlay(page)

        # ------------------------------------------------------------------
        # Step 3 — Wait for the fund table (and the Download Excel link)
        # ------------------------------------------------------------------
        print("[3/5] Waiting for the fund table to render ...")
        await wait_for_fund_table(page)
        # Give JS a moment to finish any late rendering
        await page.wait_for_timeout(2_000)
        await dismiss_cookie_overlay(page)   # dismiss again in case it re-appeared

        # ------------------------------------------------------------------
        # Step 4 — Locate the Download Excel link
        # ------------------------------------------------------------------
        print("[4/5] Locating 'Download Excel' link ...")
        download_link = await find_first_visible_locator(
            [
                (
                    "a#excel-download-lint",
                    page.locator("a#excel-download-lint").first,
                ),
                (
                    "a.fund-explorer-table__download-excel",
                    page.locator("a.fund-explorer-table__download-excel").first,
                ),
                (
                    "a[data-ve-gtm='download-excel']",
                    page.locator("a[data-ve-gtm='download-excel']").first,
                ),
                (
                    "link role named Download Excel",
                    page.get_by_role(
                        "link",
                        name=re.compile(r"download\s+excel", re.IGNORECASE),
                    ).first,
                ),
            ],
            timeout_ms=15_000,
        )

        # ------------------------------------------------------------------
        # Try to resolve a direct href first (avoids needing a download event)
        # ------------------------------------------------------------------
        href = await download_link.get_attribute("href")
        if href and not href.strip().lower().startswith(("javascript:", "#", "")):
            # Resolve relative hrefs
            if href.startswith("/"):
                base_url = "https://www.vaneck.com"
                href = base_url + href
            print(f"    Found direct export URL: {href}")
            response = await page.goto(href, wait_until="commit", timeout=TIMEOUT_MS)
            if response is None:
                raise RuntimeError("The export link did not return a response.")
            final_path = await save_response_to_disk(response)
            print(f"[5/5] File saved -> {final_path}")
            await browser.close()
            return final_path

        # ------------------------------------------------------------------
        # The href is '#' or JavaScript — intercept the network response
        # ------------------------------------------------------------------
        print("    href is '#' or JS; intercepting network response ...")

        # Strategy A: Playwright download event
        try:
            async with page.expect_download(timeout=20_000) as dl_info:
                await click_with_fallback(download_link, "Download Excel link")

            download: Download = await dl_info.value
            suggested = download.suggested_filename or "vaneck_etf_list.xlsx"
            final_path = build_output_path(suggested)
            await download.save_as(final_path)
            print(f"[5/5] File saved -> {final_path}")
            await browser.close()
            return final_path

        except PlaywrightTimeoutError:
            print("    No Playwright download event; falling back to response interception ...")

        # Strategy B: Intercept the XHR / fetch response that carries the file
        async with page.expect_response(
            lambda r: (
                r.status == 200
                and (
                    "content-disposition" in r.headers
                    or "excel" in r.headers.get("content-type", "").lower()
                    or "spreadsheet" in r.headers.get("content-type", "").lower()
                    or r.url.lower().endswith((".xls", ".xlsx", ".csv"))
                )
            ),
            timeout=TIMEOUT_MS,
        ) as response_info:
            await click_with_fallback(download_link, "Download Excel link (response intercept)")

        response = await response_info.value
        final_path = await save_response_to_disk(response)
        print(f"[5/5] File saved -> {final_path}")

        await browser.close()
        return final_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    saved = asyncio.run(download_etf_list())
    print(f"\nDone! Open your file at: {saved.resolve()}")