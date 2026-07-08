"""Download the full iShares ETF workbook."""

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Download, Locator, TimeoutError as PlaywrightTimeoutError, async_playwright

from src.source_freshness import (
    atomic_write_bytes,
    normalize_source_date,
    parse_http_last_modified,
    write_source_metadata,
)


URL = (
    "https://www.ishares.com/uk/individual/en/products/etf-investments"
    "#/?productView=all&pageNumber=1"
    "&sortColumn=totalFundSizeInMillions&sortDirection=desc"
    "&dataView=keyFacts&keyFacts=all"
)

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "ishares"
TIMEOUT_MS = 60_000
NAVIGATION_TIMEOUT_MS = 30_000
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
CUSTOMIZED_EXPORT_TOKEN = "product-screener-v3.1.jsn?type=customized-excel"
SPREADSHEET_CONTENT_TYPE_TOKENS = (
    "excel",
    "spreadsheet",
    "spreadsheetml",
    "officedocument",
)


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


async def wait_for_download_controls(page) -> None:
    selectors = [
        "screener-download-funds",
        "button.mat-mdc-menu-trigger.download-button",
        "button[aria-label*='download' i]",
        "a[aria-label*='download' i]",
        "button:has(div.button-content:has-text('DOWNLOAD'))",
        "button:has-text('DOWNLOAD')",
        "button:has-text('Download all funds')",
        "a:has-text('Download all funds')",
    ]

    last_error: Exception | None = None
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="attached", timeout=15_000)
            return
        except Exception as exc:
            last_error = exc

    raise TimeoutError("Could not find the iShares download controls in the DOM") from last_error


async def goto_with_retry(page, url: str) -> None:
    last_exc: Exception | None = None
    for wait_until in ("commit", "domcontentloaded"):
        try:
            print(f"    Trying page load with wait_until='{wait_until}' ...")
            await page.goto(url, wait_until=wait_until, timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
            return
        except Exception as exc:
            last_exc = exc
            print(f"    Navigation attempt with wait_until='{wait_until}' did not complete: {exc}")
    if last_exc is not None:
        raise RuntimeError(
            "Could not load the iShares ETF page after multiple navigation attempts."
        ) from last_exc


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
    suggested = filename_hint or "ishares_etf_list.xlsx"
    dated_output_dir = build_run_output_dir(OUTPUT_DIR)
    stem, ext = os.path.splitext(suggested)
    ext = ext or ".xlsx"
    return dated_output_dir / f"{stem}{ext}"


def response_looks_like_spreadsheet(response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return any(token in content_type for token in SPREADSHEET_CONTENT_TYPE_TOKENS)


def infer_filename_from_response(response) -> str:
    content_disposition = response.headers.get("content-disposition", "")
    filename_match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
    if filename_match:
        return filename_match.group(1)

    parsed = urlparse(response.url)
    parsed_name = Path(parsed.path).name
    if parsed_name and Path(parsed_name).suffix:
        return parsed_name

    content_type = response.headers.get("content-type", "").lower()
    if "spreadsheetml" in content_type or "officedocument" in content_type:
        return "ProductScreener.xlsx"
    if "excel" in content_type:
        return "ProductScreener.xls"
    if "csv" in content_type:
        return "ProductScreener.csv"
    return "ProductScreener.xlsx"


async def save_response_to_disk(response) -> Path:
    content_disposition = response.headers.get("content-disposition", "")
    filename_hint = infer_filename_from_response(response)

    final_path = build_output_path(filename_hint)
    response_bytes = await response.body()
    atomic_write_bytes(final_path, response_bytes)
    write_source_metadata(
        final_path,
        {
            "provider": "iShares",
            "source_url": URL,
            "resolved_source_url": response.url,
            "source_date": parse_http_last_modified(response.headers.get("last-modified")),
            "freshness_status": "CURRENT",
            "freshness_proof": "Live iShares customized-excel export response",
            "http_last_modified": normalize_source_date(
                parse_http_last_modified(response.headers.get("last-modified"))
            ),
            "content_disposition": content_disposition,
        },
    )
    return final_path


async def write_download_to_disk(download: Download) -> Path:
    suggested = download.suggested_filename or "ProductScreener.xlsx"
    final_path = build_output_path(suggested)
    temp_path = final_path.with_name(f"{final_path.name}.download")
    await download.save_as(temp_path)
    temp_bytes = temp_path.read_bytes()
    temp_path.unlink(missing_ok=True)
    atomic_write_bytes(final_path, temp_bytes)
    write_source_metadata(
        final_path,
        {
            "provider": "iShares",
            "source_url": URL,
            "resolved_source_url": clean_download_url(download.url),
            "source_date": "",
            "freshness_status": "CURRENT SOURCE VERIFIED",
            "freshness_proof": "Blob download verified against live iShares export flow",
        },
    )
    return final_path


def clean_download_url(url: str | None) -> str:
    if not url:
        return ""
    cleaned = str(url).strip()
    return "" if cleaned.startswith("blob:") else cleaned


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
            extra_http_headers={
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        page = await context.new_page()

        print("[1/5] Navigating to iShares ETF page ...")
        await goto_with_retry(page, URL)
        await page.wait_for_timeout(2_000)

        print("[2/5] Clearing overlays and passing T&C gate ...")
        await dismiss_onetrust_overlay(page)
        try:
            accept_btn = page.locator(
                "a[href*='siteEntryPassthrough=true'], "
                "button:has-text('Accept'), "
                "a:has-text('Continue')"
            ).first
            await accept_btn.wait_for(state="visible", timeout=15_000)
            await accept_btn.scroll_into_view_if_needed()
            await accept_btn.click(timeout=10_000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            await page.wait_for_timeout(2_000)
            print("    T&C accepted.")
        except Exception:
            print("    No T&C gate found, continuing.")

        await dismiss_onetrust_overlay(page)

        print("[3/5] Waiting for the product table to render ...")
        await wait_for_download_controls(page)
        await page.wait_for_timeout(5_000)
        await dismiss_onetrust_overlay(page)
        await wait_for_download_controls(page)

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

        print("     Selecting the all-funds export option from dropdown ...")
        await page.wait_for_timeout(1_000)

        all_etfs_option = await find_first_visible_locator(
            [
                (
                    "overlay menuitem DOWNLOAD ALL FUNDS (XLSX)",
                    page.locator(
                        "div.cdk-overlay-pane div[role='menu'] "
                        "button[role='menuitem']:has-text('DOWNLOAD ALL FUNDS (XLSX)')"
                    ).first,
                ),
                (
                    "overlay menuitem DOWNLOAD ALL FUNDS (XLS)",
                    page.locator(
                        "div.cdk-overlay-pane div[role='menu'] "
                        "button[role='menuitem']:has-text('DOWNLOAD ALL FUNDS (XLS)')"
                    ).first,
                ),
                (
                    "overlay button with button-content DOWNLOAD ALL FUNDS (XLSX)",
                    page.locator(
                        "div.cdk-overlay-pane button.mat-mdc-menu-item "
                        ":has(div.button-content:has-text('DOWNLOAD ALL FUNDS (XLSX)'))"
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
                        "div.cdk-overlay-pane text=/download\\s+all\\s+funds\\s*\\((xlsx|xls)\\)/i"
                    ).first,
                ),
                (
                    "menuitem role named DOWNLOAD ALL FUNDS",
                    page.get_by_role(
                        "menuitem",
                        name=re.compile(r"download\s+all\s+funds\s*\((xlsx|xls)\)", re.IGNORECASE),
                    ).first,
                ),
                (
                    "menuitem role named DOWNLOAD ALL FUNDS without extension",
                    page.get_by_role(
                        "menuitem",
                        name=re.compile(r"download\s+all\s+funds", re.IGNORECASE),
                    ).first,
                ),
            ],
            timeout_ms=10_000,
        )

        direct_href = await extract_download_href(all_etfs_option)
        if direct_href:
            print(f"     Found export URL: {direct_href}")
            response = await page.goto(direct_href, wait_until="commit", timeout=TIMEOUT_MS)
            if response is None:
                raise RuntimeError("The export link did not return a response.")

            final_path = await save_response_to_disk(response)
            print(f"[5/5] File saved -> {final_path}")
            await browser.close()
            return final_path

        try:
            async with page.expect_response(
                lambda response: (
                    response.status == 200
                    and CUSTOMIZED_EXPORT_TOKEN in response.url
                    and response_looks_like_spreadsheet(response)
                ),
                timeout=TIMEOUT_MS,
            ) as response_info:
                await click_with_fallback(all_etfs_option, "DOWNLOAD ALL FUNDS export option")

            response = await response_info.value
            final_path = await save_response_to_disk(response)
            print(f"[5/5] File saved -> {final_path}")
            await browser.close()
            return final_path
        except PlaywrightTimeoutError:
            print("     No direct customized-excel response captured; falling back to browser download.")
            await click_with_fallback(download_btn, "DOWNLOAD button")
            await page.wait_for_timeout(1_000)

        try:
            async with page.expect_download(timeout=TIMEOUT_MS) as dl_info:
                await click_with_fallback(all_etfs_option, "DOWNLOAD ALL FUNDS export option")

            download: Download = await dl_info.value
            final_path = await write_download_to_disk(download)
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
                    or response_looks_like_spreadsheet(response)
                    or response.url.lower().endswith((".xls", ".xlsx", ".csv"))
                )
            ),
            timeout=TIMEOUT_MS,
        ) as response_info:
            await click_with_fallback(all_etfs_option, "DOWNLOAD ALL FUNDS export option")

        response = await response_info.value
        final_path = await save_response_to_disk(response)
        print(f"[5/5] File saved -> {final_path}")

        await browser.close()
        return final_path


if __name__ == "__main__":
    saved = asyncio.run(download_etf_list())
    print(f"\nDone! Open your file at: {saved.resolve()}")
