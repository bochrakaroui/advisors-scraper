"""Download Vanguard UK ETF overview data into a provider-specific raw snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Page, async_playwright


PAGE_URL = "https://www.vanguard.co.uk/uk-fund-directory/product?product-type=etf"
BASE_URL = "https://www.vanguard.co.uk"
ISSUER = "Vanguard"
PROVIDER = "Vanguard"

BASE_DIR = Path(__file__).resolve().parents[2]
OUTPUT_DIR = BASE_DIR / "providers" / "vanguard"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

ROW_SELECTOR = "tr[data-cy^='fund-overview-table-row-']"
FUNDS_QUERY_NAME = '"operationName":"FundsQuery"'
ISIN_PATTERN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
PORT_ID_PATTERN = re.compile(r"/uk-fund-directory/product/etf/[^/]+/([^/]+)/", flags=re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Vanguard UK ETF overview data into a raw JSON snapshot.")
    parser.add_argument("--output", type=Path, help="Optional output path. Defaults to a date folder inside providers/vanguard.")
    return parser.parse_args()


def build_run_output_dir(base_dir: Path, run_date: str) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "vanguard_etf_export.json"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def normalize_ter_bps(raw_value: str) -> str:
    cleaned = clean_text(raw_value).replace("%", "").strip()
    if not cleaned:
        return ""
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        percentage = Decimal(cleaned)
    except InvalidOperation:
        return ""
    return format_decimal(percentage * Decimal("100"), places=2)


def normalize_aum_mn(raw_value: str) -> str:
    cleaned = clean_text(raw_value)
    if not cleaned:
        return ""

    compact = cleaned.replace(",", "").replace(" ", "")
    multiplier = Decimal("1")
    if compact.endswith("B"):
        multiplier = Decimal("1000")
        compact = compact[:-1]
    elif compact.endswith("M"):
        compact = compact[:-1]

    compact = re.sub(r"^[A-Za-z$€£]+", "", compact)

    try:
        amount = Decimal(compact)
    except InvalidOperation:
        return ""

    return format_decimal(amount * multiplier, places=2)


def extract_port_id(product_url: str) -> str:
    match = PORT_ID_PATTERN.search(product_url)
    return match.group(1) if match else ""


def is_valid_product_url(product_url: str) -> bool:
    normalized = clean_text(product_url)
    return normalized.startswith(f"{BASE_URL}/uk-fund-directory/product/etf/")


def build_isin_map(funds_payload: dict[str, Any]) -> dict[str, str]:
    funds = funds_payload.get("data", {}).get("funds", [])
    if not isinstance(funds, list):
        raise ValueError("Unexpected Vanguard FundsQuery payload: missing funds list.")

    isin_by_port_id: dict[str, str] = {}
    for fund in funds:
        profile = fund.get("profile") or {}
        port_id = clean_text(profile.get("portId"))
        if not port_id:
            continue

        identifiers = profile.get("identifiers") or []
        for identifier in identifiers:
            if clean_text(identifier.get("altIdCode")).upper() == "ISIN":
                isin_value = clean_text(identifier.get("altIdValue")).upper()
                if ISIN_PATTERN.fullmatch(isin_value):
                    isin_by_port_id[port_id] = isin_value
                    break

    return isin_by_port_id


def parse_overview_cell(cell_text: str) -> dict[str, str]:
    lines = [clean_text(line) for line in cell_text.splitlines()]
    lines = [line for line in lines if line]
    details: dict[str, str] = {}

    if lines:
        details["ETF Name"] = lines[0]

    for index, line in enumerate(lines[:-1]):
        if line in {"Ticker", "Currency", "Share Class"}:
            details[line] = lines[index + 1]

    return details


async def accept_cookies(page: Page) -> None:
    for selector in (
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept all cookies')",
    ):
        try:
            button = page.locator(selector).first
            await button.click(timeout=3_000)
            await page.wait_for_timeout(1_000)
            return
        except Exception:
            continue


async def capture_funds_query_request(page: Page) -> dict[str, Any]:
    request_payload: dict[str, Any] = {}

    def on_request(request: Any) -> None:
        if request_payload:
            return
        if "gpx/graphql" not in request.url or request.method != "POST":
            return
        post_data = request.post_data or ""
        if FUNDS_QUERY_NAME not in post_data:
            return

        request_payload["url"] = request.url
        request_payload["post_data"] = post_data
        request_payload["headers"] = dict(request.headers)

    print("[1/6] Loading Vanguard ETF overview page ...")
    page.on("request", on_request)
    await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=120_000)
    await page.wait_for_function(
        """selector => document.querySelectorAll(selector).length > 0""",
        arg=ROW_SELECTOR,
        timeout=120_000,
    )

    if not request_payload:
        raise ValueError("Vanguard FundsQuery request was not captured from the overview page.")

    print("      Captured official FundsQuery request.")
    return request_payload


async def fetch_funds_query_payload(page: Page, request_payload: dict[str, Any]) -> dict[str, Any]:
    headers = dict(request_payload.get("headers") or {})
    headers["referer"] = PAGE_URL

    print("[2/6] Replaying official FundsQuery request for ISIN mapping ...")
    response = await page.context.request.post(
        str(request_payload["url"]),
        data=str(request_payload["post_data"]),
        headers=headers,
        timeout=60_000,
    )
    if response.status != 200:
        raise ValueError(f"Vanguard FundsQuery request returned HTTP {response.status}.")
    payload = await response.json()
    if "data" not in payload or "funds" not in payload["data"]:
        raise ValueError("Unexpected Vanguard FundsQuery response: missing fund data.")
    print(f"      FundsQuery returned {len(payload['data'].get('funds', [])):,} fund records.")
    return payload


async def expand_table_to_all_rows(page: Page) -> None:
    print("[3/6] Expanding Vanguard table to show all ETF rows ...")
    select = page.locator("select#nds-select-1").first
    await select.wait_for(state="attached", timeout=60_000)
    await accept_cookies(page)
    await select.select_option(value="-1")
    await page.wait_for_function(
        """selector => document.querySelectorAll(selector).length >= 100""",
        arg=ROW_SELECTOR,
        timeout=120_000,
    )
    row_count = await page.locator(ROW_SELECTOR).count()
    print(f"      Overview table expanded to {row_count:,} rows.")


async def extract_overview_rows(page: Page, scraped_at: str) -> list[dict[str, str]]:
    print("[4/6] Extracting Vanguard overview rows ...")
    row_payloads = await page.locator(ROW_SELECTOR).evaluate_all(
        """
        rows => rows.map(row => {
            const cells = Array.from(row.querySelectorAll('td')).map(cell => cell.innerText || '');
            const link = row.querySelector('a.europe-core-fund-name');
            return {
                rowDataCy: row.getAttribute('data-cy') || '',
                href: link ? link.getAttribute('href') || '' : '',
                name: link ? (link.innerText || '') : '',
                cells
            };
        })
        """
    )

    rows: list[dict[str, str]] = []
    for row_payload in row_payloads:
        cells = row_payload.get("cells") or []
        if len(cells) < 6:
            continue

        fund_details = parse_overview_cell(cells[0])
        href = clean_text(row_payload.get("href"))
        product_url = urljoin(BASE_URL, href)
        if not is_valid_product_url(product_url):
            continue

        etf_name = clean_text(row_payload.get("name")) or fund_details.get("ETF Name", "")
        port_id = extract_port_id(product_url)
        rows.append(
            {
                "provider": PROVIDER,
                "issuer": ISSUER,
                "port_id": port_id,
                "etf_name": etf_name,
                "ticker": clean_text(fund_details.get("Ticker")),
                "ccy": clean_text(fund_details.get("Currency")).upper(),
                "share_class": clean_text(fund_details.get("Share Class")),
                "aum_raw": clean_text(cells[3]),
                "aum_mn": normalize_aum_mn(cells[3]),
                "ter_raw": clean_text(cells[5]),
                "ter_bps": normalize_ter_bps(cells[5]),
                "product_url": product_url,
                "isin": "",
                "scraped_at": scraped_at,
            }
        )

    print(f"      Parsed {len(rows):,} overview rows.")
    return rows


async def fetch_isin_from_detail_page(page: Page, product_url: str) -> str:
    await page.goto(product_url, wait_until="domcontentloaded", timeout=120_000)
    await page.wait_for_timeout(3_000)
    body_text = await page.locator("body").inner_text()
    match = ISIN_PATTERN.search(body_text)
    return match.group(0).upper() if match else ""


async def enrich_isins_from_detail_pages(page: Page, rows: list[dict[str, str]]) -> None:
    missing_rows = [row for row in rows if not row["isin"] and row["product_url"]]
    if not missing_rows:
        print("[6/6] Detail-page fallback not needed.")
        return

    print(f"[6/6] Filling {len(missing_rows):,} missing ISINs from Vanguard detail pages ...")
    for row in missing_rows:
        try:
            row["isin"] = await fetch_isin_from_detail_page(page, row["product_url"])
        except Exception as exc:
            print(f"[WARN] Vanguard detail page fallback failed for {row['product_url']}: {exc}")


async def build_snapshot() -> dict[str, Any]:
    scraped_at = datetime.now().isoformat()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 1400},
        )
        list_page = await context.new_page()

        print("Official URL used: https://www.vanguard.co.uk/uk-fund-directory/product?product-type=etf")
        print("Data method used: overview table + FundsQuery GraphQL ISIN map")
        request_payload = await capture_funds_query_request(list_page)
        funds_payload = await fetch_funds_query_payload(list_page, request_payload)
        await expand_table_to_all_rows(list_page)
        overview_rows = await extract_overview_rows(list_page, scraped_at=scraped_at)

        print("[5/6] Mapping ISINs from FundsQuery payload ...")
        isin_by_port_id = build_isin_map(funds_payload)
        for row in overview_rows:
            row["isin"] = isin_by_port_id.get(row["port_id"], "")
        mapped_isins = sum(1 for row in overview_rows if row["isin"])
        print(f"      Mapped {mapped_isins:,} ISINs directly from GraphQL.")

        detail_page = await context.new_page()
        await enrich_isins_from_detail_pages(detail_page, overview_rows)
        await browser.close()

    valid_isin_count = sum(1 for row in overview_rows if ISIN_PATTERN.fullmatch(row["isin"]))
    missing_ccy_count = sum(1 for row in overview_rows if not row["ccy"])
    missing_aum_count = sum(1 for row in overview_rows if not row["aum_mn"])

    print(f"Rows extracted: {len(overview_rows):,}")
    print(f"Valid ISINs: {valid_isin_count:,}")
    print(f"Missing CCY values: {missing_ccy_count:,}")
    print(f"Missing AUM values: {missing_aum_count:,}")

    return {
        "source_url": PAGE_URL,
        "method": "overview table + FundsQuery GraphQL",
        "captured_at": scraped_at,
        "rows": overview_rows,
    }


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


async def download_vanguard_file(output_path: Path | None = None) -> Path:
    resolved_output_path = output_path.resolve() if output_path else build_output_path(datetime.now())
    snapshot = await build_snapshot()
    write_json(resolved_output_path, snapshot)
    print(f"Raw snapshot saved: {resolved_output_path}")
    return resolved_output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected Vanguard snapshot in {path}: expected a list of rows.")
    return rows


def main() -> None:
    args = parse_args()
    output_path = asyncio.run(download_vanguard_file(args.output))
    print(f"Done! Open your file at: {output_path.resolve()}")


if __name__ == "__main__":
    main()
