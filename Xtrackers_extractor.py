"""
Xtrackers ETF List Downloader
=============================
Downloads the Xtrackers export file from:
  https://etf.dws.com/en-gb/product-finder/?AssetClasses=Commodities,Equities,Fixed+Income,Multi+Asset

Strategy:
  1. Open the real product-finder page in Playwright.
  2. Scroll the actual "Download data (XLSX)" link into view.
  3. Read the hydrated anchor href from the DOM.
  4. If the page never hydrates the href, build the same query shape manually.
  5. Download the file through the browser context so cookies/referer are preserved.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

from playwright.async_api import async_playwright


PAGE_URL = "https://etf.dws.com/en-gb/product-finder/?AssetClasses=Commodities,Equities,Fixed+Income,Multi+Asset"
OUTPUT_DIR = Path("./xtrackers_downloads")
TIMEOUT_MS = 90_000


def build_output_path() -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"xtrackers_etf_export_{timestamp}.xlsx"


def build_fallback_export_url(page_url: str) -> str:
    parsed = urlparse(page_url)
    asset_classes_raw = parse_qs(parsed.query).get("AssetClasses", [""])[0]
    if not asset_classes_raw:
        # The SPA may drop the original query params from page.url after hydration.
        asset_classes_raw = parse_qs(urlparse(PAGE_URL).query).get("AssetClasses", [""])[0]

    asset_classes = [value.strip().replace("+", " ") for value in asset_classes_raw.split(",") if value.strip()]

    payload = {
        "selectedTabIndex": 0,
        "totalReturnType": 0,
        "searchTerm": "",
        "filters": [
            {
                "identifier": "AssetClasses",
                "filterOptions": {
                    "AssetClasses": [{"identifier": asset_class} for asset_class in asset_classes]
                },
            }
        ],
    }

    encoded = quote(json.dumps(payload, separators=(",", ":")), safe="")
    return urljoin(page_url, f"/en-gb/product-finder/downloadxls/?query={encoded}")


async def resolve_export_url(page) -> tuple[str, str]:
    download_link = page.locator("a.d-fund-finder__download-link[href*='downloadxls']").first
    await download_link.wait_for(state="visible", timeout=TIMEOUT_MS)
    await download_link.scroll_into_view_if_needed()
    await page.wait_for_timeout(2_000)

    for _ in range(30):
        href = await page.evaluate(
            """
            () => {
                const link = document.querySelector("a.d-fund-finder__download-link[href*='downloadxls']");
                return link ? (link.href || link.getAttribute('href') || '') : '';
            }
            """
        )
        if href and "downloadxls" in href and "identifier" in href and "%22filters%22%3A%5B%5D" not in href:
            return href, "hydrated page link"
        await page.wait_for_timeout(1_000)

    return build_fallback_export_url(page.url), "fallback query built from page URL"


async def accept_xtrackers_gate(page) -> None:
    cookie_accept = page.locator("#consent_prompt_submit").first
    if await cookie_accept.is_visible():
        print("      Accepting cookies ...")
        await cookie_accept.click(force=True)
        await page.wait_for_timeout(2_000)

    continue_btn = page.locator("button:has-text('Accept & continue')").first
    if await continue_btn.is_visible():
        print("      Accepting entry gate ...")
        await continue_btn.click(force=True)
        await page.wait_for_timeout(5_000)


async def download_xtrackers_file() -> Path:
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
        )
        page = await context.new_page()

        print("[1/3] Loading product finder page ...")
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(3_000)
        await accept_xtrackers_gate(page)

        print("[2/3] Resolving export URL ...")
        export_url, source = await resolve_export_url(page)
        print(f"      Using {source}.")
        print(f"      {export_url}")

        print("[3/3] Downloading file via browser context ...")
        response = await context.request.get(
            export_url,
            headers={
                "Referer": PAGE_URL,
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=TIMEOUT_MS,
        )

        if not response.ok:
            raise RuntimeError(
                f"Download failed - HTTP {response.status}\n"
                f"URL: {export_url}"
            )

        body = await response.body()
        if body[:2] != b"PK":
            preview = body[:300].decode("utf-8", errors="replace")
            raise RuntimeError(
                "Response is not a valid XLSX file.\n"
                f"URL: {export_url}\n"
                f"Preview: {preview}"
            )

        out_path = build_output_path()
        out_path.write_bytes(body)
        print(f"      Size: {len(body):,} bytes")
        print(f"\nFile saved -> {out_path}")

        await browser.close()
        return out_path


if __name__ == "__main__":
    saved = asyncio.run(download_xtrackers_file())
    print(f"\nDone! Open your file at: {saved.resolve()}")
