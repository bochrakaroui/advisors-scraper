"""Scrape M&G ETF data from official M&G fund pages.

For the current M&G ETF pages, the reliable source is the live
`singleClassSearch` Kurtosys response triggered by each factsheet page.
This scraper loads each target page with Playwright, captures that official
response, and extracts the ETF fields from `properties_pub` plus the
statistics payload.

Output: providers/M&G/YYYY-MM-DD/mg_etf_export.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Page, async_playwright


ISSUER = "M&G"
MANDG_BASE = "https://www.mandg.com/investments/professional-investor/en-gb/funds"

IE_ISIN_SLUGS: dict[str, str] = {
    "IE0000DO92H7": "mg-us-treasury-bond-active-ucits-etf",
    "IE000AEM1K78": "mg-global-maxima-equity-ucits-etf",
    "IE000KEV0H41": "mg-uk-index-linked-gilts-active-ucits-etf",
    "IE000PTM74B6": "mg-uk-gilts-active-ucits-etf",
    "IE000YFPL0I4": "mg-us-treasury-bond-active-ucits-etf",
}

TARGET_ISINS: list[str] = list(IE_ISIN_SLUGS)

PAGE_TIMEOUT_MS = 60_000
RESPONSE_WAIT_MS = 20_000
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
IE_FETCH_RETRIES = 2

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "M&G"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

INTERNAL_SPACE = re.compile(r"\s+")
INVISIBLE_CHARS = ("\u00A0", "\u2007", "\u202F", "\u200B", "\uFEFF")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now()


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "mg_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = INTERNAL_SPACE.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "N/A", "n/a", "None"} else cleaned


def normalize_isin(value: object | None) -> str:
    if value is None:
        return ""
    normalized = str(value)
    for character in INVISIBLE_CHARS:
        normalized = normalized.replace(character, "")
    return INTERNAL_SPACE.sub("", normalized.strip().upper())


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def percentage_to_bps(raw: object | None) -> str:
    cleaned = clean_text(raw).replace("%", "").replace(",", ".")
    if not cleaned:
        return ""
    try:
        return format_decimal(Decimal(cleaned) * Decimal("100"), places=2)
    except InvalidOperation:
        return ""


def amount_to_millions(raw: object | None) -> str:
    if raw is None or raw == "":
        return ""

    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
        if abs(value) >= Decimal("1000000"):
            value = value / Decimal("1000000")
        return format_decimal(value, places=2)

    cleaned = clean_text(raw)
    if not cleaned:
        return ""

    normalized = re.sub(r"[$Â£â‚¬,\s]", "", cleaned)
    multiplier = Decimal("1")
    lower = normalized.lower()
    if lower.endswith(("bn", "b")):
        multiplier = Decimal("1000")
        normalized = re.sub(r"(?i)(bn|b)$", "", normalized)
    elif lower.endswith(("mn", "m")):
        normalized = re.sub(r"(?i)(mn|m)$", "", normalized)

    try:
        value = Decimal(normalized)
        if multiplier == Decimal("1") and abs(value) >= Decimal("1000000"):
            value = value / Decimal("1000000")
        return format_decimal(value * multiplier, places=2)
    except InvalidOperation:
        return ""


def factsheet_url(isin: str) -> str:
    slug = IE_ISIN_SLUGS.get(isin, isin.lower())
    return f"{MANDG_BASE}/{slug}/{isin.lower()}"


def coerce_value(value: object | None) -> object | None:
    if isinstance(value, dict):
        for key in ("value", "VALUE", "value_string", "VALUE_STRING"):
            if key in value:
                return coerce_value(value.get(key))
    return value


def first_record(body: Any) -> dict[str, Any]:
    record: Any = body
    if isinstance(record, dict):
        values = record.get("values")
        if isinstance(values, list) and values and isinstance(values[0], dict):
            record = values[0]
    if isinstance(record, list) and record and isinstance(record[0], dict):
        record = record[0]
    return record if isinstance(record, dict) else {}


def get_property(record: dict[str, Any], *keys: str) -> object | None:
    properties = record.get("properties_pub")
    if isinstance(properties, dict):
        for key in keys:
            if key in properties:
                return coerce_value(properties.get(key))
    for key in keys:
        direct = coerce_value(record.get(key))
        if direct not in (None, "", []):
            return direct
    return None


def get_stat_values(record: dict[str, Any], code: str) -> list[dict[str, Any]]:
    statistics = record.get("statistics")
    if not isinstance(statistics, list):
        return []
    for statistic in statistics:
        if not isinstance(statistic, dict):
            continue
        if clean_text(statistic.get("code")).lower() != code.lower():
            continue
        values = statistic.get("values")
        return values if isinstance(values, list) else []
    return []


def get_stat_row(record: dict[str, Any], code: str, label: str | None = None) -> dict[str, Any]:
    values = get_stat_values(record, code)
    if not values:
        return {}
    if label is not None:
        wanted = label.lower()
        for value in values:
            if isinstance(value, dict) and clean_text(value.get("label")).lower() == wanted:
                return value
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def extract_objective_from_text(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"Investment Objective\s+(.*?)\s+Fund Risks\b", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return clean_text(match.group(1))


def build_empty_row(isin: str) -> dict[str, str]:
    return {
        "etf_name": "",
        "issuer": ISSUER,
        "isin": isin,
        "ccy": "",
        "ter_bps": "",
        "aum_mn": "",
        "date": "",
        "nav": "",
        "nav_date": "",
        "distribution_type": "",
        "fund_type": "",
        "fund_domicile": "",
        "index_name": "",
        "index_ticker": "",
        "primary_ticker": "",
        "share_class": "",
        "management_company": "",
        "product_range": "",
        "objective": "",
        "factsheet_url": factsheet_url(isin),
        "api_url": "",
        "fetch_status": "pending",
    }


def extract_mandg_fields(body: Any, isin: str) -> dict[str, str]:
    record = first_record(body)

    latest_nav_row = get_stat_row(record, "latest_nav", "NAV_Per_Unit") or get_stat_row(record, "latest_nav")
    fund_charge_row = get_stat_row(record, "fund_charges", "Ongoing charge") or get_stat_row(record, "fund_charges")
    fund_size_row = get_stat_row(record, "fund_size", "Fund Size") or get_stat_row(record, "fund_size")
    listings = get_stat_values(record, "listings")
    manual_data = get_stat_values(record, "manual_data")

    primary_listing = None
    for listing in listings:
        if isinstance(listing, dict) and listing.get("is_primary"):
            primary_listing = listing
            break
    if primary_listing is None and listings and isinstance(listings[0], dict):
        primary_listing = listings[0]

    bloomberg_ticker = next(
        (
            clean_text(item.get("value_string"))
            for item in manual_data
            if isinstance(item, dict) and clean_text(item.get("label")).lower() == "bloomberg ticker"
        ),
        "",
    )

    nav_date = clean_text(
        coerce_value(latest_nav_row.get("nav_date") or latest_nav_row.get("date")) if latest_nav_row else None
    )
    fund_date = (
        clean_text(coerce_value(fund_size_row.get("fund_size_date")) if fund_size_row else None)
        or nav_date
        or clean_text(coerce_value(fund_charge_row.get("charge_date")) if fund_charge_row else None)
        or clean_text(get_property(record, "share_class_launch_date", "fund_launch_date", "inceptionDate", "launchDate"))
    )

    return {
        "etf_name": clean_text(
            get_property(
                record,
                "product_name",
                "share_class_name",
                "shareClassName",
                "shareclassName",
                "fund_name",
                "displayName",
                "fundName",
                "name",
                "title",
            )
        ),
        "issuer": ISSUER,
        "isin": normalize_isin(get_property(record, "isin", "ISIN", "shareClassIsin", "isinCode") or isin),
        "ccy": clean_text(
            get_property(record, "share_class_currency", "currency", "fund_base_currency", "share_class_currency_alt")
        ),
        "ter_bps": percentage_to_bps(coerce_value(fund_charge_row.get("value")) if fund_charge_row else None),
        "aum_mn": amount_to_millions(coerce_value(fund_size_row.get("value")) if fund_size_row else None),
        "date": fund_date,
        "nav": clean_text(coerce_value(latest_nav_row.get("value")) if latest_nav_row else None),
        "nav_date": nav_date,
        "distribution_type": clean_text(
            get_property(record, "share_class_unit_type", "distributionType", "incomeType", "dividendType")
        ),
        "fund_type": clean_text(get_property(record, "product_type", "fundType", "vehicleType", "legalStructure", "fundStructure")),
        "fund_domicile": clean_text(
            get_property(record, "domicile", "fundDomicile", "countryOfDomicile", "registrationCountry")
        ),
        "index_name": clean_text(get_property(record, "benchmark_name", "indexName", "benchmark", "comparatorName")),
        "index_ticker": "",
        "primary_ticker": clean_text(
            (primary_listing or {}).get("ticker")
            or (primary_listing or {}).get("bloomberg_ticker")
            or bloomberg_ticker
        ),
        "share_class": clean_text(get_property(record, "share_class_name", "shareClass", "shareClassName", "shareClassType")),
        "management_company": "",
        "product_range": clean_text(get_property(record, "fund_umbrella_name", "productRange", "fundRange", "range", "productFamily")),
        "objective": "",
    }


async def scrape_ie_isin(page: Page, isin: str) -> dict[str, str]:
    row = build_empty_row(isin)
    target_url = factsheet_url(isin)
    body_text = ""
    last_status = "singleclasssearch_not_found"

    for attempt in range(1, IE_FETCH_RETRIES + 1):
        try:
            async with page.expect_response(
                lambda response: (
                    "singleclasssearch" in response.url.lower()
                    and f"ISIN={isin}" in response.url.upper()
                    and response.status == 200
                ),
                timeout=RESPONSE_WAIT_MS,
            ) as response_info:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            response = await response_info.value
            captured_body = await response.json()
            body_text = await page.locator("body").inner_text()
        except PlaywrightTimeoutError:
            last_status = "singleclasssearch_not_found"
            logging.warning("M&G %s did not emit singleClassSearch on attempt %d/%d", isin, attempt, IE_FETCH_RETRIES)
            continue
        except Exception as exc:
            row["fetch_status"] = f"error:{type(exc).__name__}"
            return row

        extracted_row = extract_mandg_fields(captured_body, isin)
        extracted_isin = normalize_isin(extracted_row.get("isin"))
        if extracted_isin and extracted_isin != isin:
            last_status = f"singleclasssearch_mismatch:{extracted_isin}"
            logging.warning(
                "M&G %s returned singleClassSearch data for %s on attempt %d/%d",
                isin,
                extracted_isin,
                attempt,
                IE_FETCH_RETRIES,
            )
            continue

        row.update(extracted_row)
        row["api_url"] = response.url
        if not row["objective"]:
            row["objective"] = extract_objective_from_text(body_text)
        row["fetch_status"] = "ok" if row["etf_name"] else "missing_data"
        return row

    row["fetch_status"] = last_status
    return row


async def build_snapshot_async(now: datetime) -> dict[str, Any]:
    listing_rows: list[dict[str, str]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent=BROWSER_UA,
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1400, "height": 1000},
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        await context.add_cookies(
            [
                {
                    "name": "OptanonAlertBoxClosed",
                    "value": "accepted",
                    "domain": ".mandg.com",
                    "path": "/",
                },
                {
                    "name": "lvs_expiry",
                    "value": "investments/gb",
                    "domain": ".mandg.com",
                    "path": "/",
                },
            ]
        )

        total = len(TARGET_ISINS)
        for index, isin in enumerate(TARGET_ISINS, 1):
            logging.info("[%d/%d] Fetching %s", index, total, isin)
            page = await context.new_page()
            try:
                listing_rows.append(await scrape_ie_isin(page, isin))
            finally:
                await page.close()

        await browser.close()

    status_counts: dict[str, int] = {}
    for row in listing_rows:
        status = row["fetch_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    logging.info("M&G fetch summary: %s", status_counts)

    return {
        "source": {
            "provider": ISSUER,
            "mandg_base_url": MANDG_BASE,
            "kurtosys_api": "https://api-uk.kurtosys.app/fund/savedSearchEntity/singleClassSearch",
        },
        "method": (
            "Sequential Playwright loads of the official factsheet pages; "
            "share classes are parsed from the live Kurtosys singleClassSearch response."
        ),
        "captured_at": now.isoformat(),
        "listing_rows": listing_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now = timestamp_now()
    snapshot = asyncio.run(build_snapshot_async(now))
    write_json(destination, snapshot)
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved : %s", destination)


async def download_mg_file() -> Path:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)
    snapshot = await build_snapshot_async(now)
    write_json(output_path, snapshot)
    logging.info("Snapshot saved: %s", output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def main() -> None:
    output_path = build_output_path(timestamp_now())
    download_snapshot(output_path)


if __name__ == "__main__":
    main()
