"""Download the Xtrackers ETF export workbook."""

import asyncio
import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse
from zipfile import ZipFile

from playwright.async_api import Download, TimeoutError as PlaywrightTimeoutError, async_playwright


PAGE_URL = "https://etf.dws.com/en-gb/product-finder/?AssetClasses=Commodities,Equities,Fixed+Income,Multi+Asset"
BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "xtrackers"
TIMEOUT_MS = 90_000
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


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


def build_output_path() -> Path:
    return build_run_output_dir(OUTPUT_DIR) / "xtrackers_etf_export.xlsx"


def workbook_has_data_rows(body: bytes) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(body)

    try:
        with ZipFile(temp_path) as workbook:
            sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        row_numbers = [
            int(row.attrib.get("r", "0"))
            for row in sheet_root.findall(".//a:sheetData/a:row", XLSX_NS)
        ]
        return any(row_number >= 11 for row_number in row_numbers)
    finally:
        temp_path.unlink(missing_ok=True)


def build_fallback_export_url(page_url: str) -> str:
    parsed = urlparse(page_url)
    asset_classes_raw = parse_qs(parsed.query).get("AssetClasses", [""])[0]
    if not asset_classes_raw:
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


async def try_browser_download(page, download_link) -> bytes | None:
    try:
        async with page.expect_download(timeout=TIMEOUT_MS) as download_info:
            await download_link.click(force=True)
        download: Download = await download_info.value
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            await download.save_as(temp_path)
            return temp_path.read_bytes()
        finally:
            temp_path.unlink(missing_ok=True)
    except PlaywrightTimeoutError:
        return None


async def accept_xtrackers_gate(page) -> None:
    cookie_accept = page.locator("#consent_prompt_submit").first
    if await cookie_accept.is_visible():
        print("      Accepting cookies ...")
        try:
            await cookie_accept.scroll_into_view_if_needed()
            await cookie_accept.click(force=True)
        except Exception:
            await cookie_accept.evaluate("(el) => el.click()")
        await page.wait_for_timeout(2_000)

    continue_btn = page.locator("button:has-text('Accept & continue')").first
    if await continue_btn.is_visible():
        print("      Accepting entry gate ...")
        try:
            await continue_btn.scroll_into_view_if_needed()
            await continue_btn.click(force=True)
        except Exception:
            await continue_btn.evaluate("(el) => el.click()")
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

        print("[3/3] Downloading file via browser flow ...")
        download_link = page.locator("a.d-fund-finder__download-link[href*='downloadxls']").first
        body = await try_browser_download(page, download_link)
        download_method = "browser click"

        if body is None:
            print("      Browser download event not detected; falling back to browser-context request.")
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
            download_method = "browser-context request"

        if body[:2] != b"PK":
            preview = body[:300].decode("utf-8", errors="replace")
            raise RuntimeError(
                "Response is not a valid XLSX file.\n"
                f"URL: {export_url}\n"
                f"Preview: {preview}"
            )

        if not workbook_has_data_rows(body):
            raise RuntimeError(
                "Downloaded Xtrackers workbook contains headers only and no fund rows.\n"
                f"URL: {export_url}\n"
                f"Method: {download_method}"
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
