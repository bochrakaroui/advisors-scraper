"""Extract the required ETF fields from the latest downloaded Xtrackers file."""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zipfile import ZipFile

from playwright.async_api import Page, async_playwright


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "xtrackers_downloads"
OUTPUT_DIR = BASE_DIR / "xtrackers_processed"

PRODUCT_FINDER_URL = (
    "https://etf.dws.com/en-gb/product-finder/"
    "?AssetClasses=Commodities,Equities,Fixed+Income,Multi+Asset"
)
DATATABLE_API_URL = "https://etf.dws.com/api/fundfinder/en-gb/datatable"
PRODUCT_BASE_URL = "https://etf.dws.com"
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
TIMEOUT_MS = 90_000
TICKER_CONCURRENCY = 4
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
ECB_NS = {"fx": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}

SOURCE_COLUMNS = {
    "fund_name": "Name",
    "isin": "ISIN",
    "asset_class": "Asset class",
    "ter_percent": "TER p.a. (%)",
    "currency": "Share class currency",
    "aum_gbp": "AuM (GBP)",
    "distribution": "Distribution policy",
    "launch_date": "Sub-fund launch",
    "as_of_date": "As of",
}

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "Asset Class",
    "CCY",
    "TER (bps)",
    "Listing Date",
    "Distribution",
    "ISIN",
    "Ticker",
    "AUM",
]

ASSET_CLASS_MAP = {
    "Equities": "Equity",
    "Commodities": "Commodity",
}

DISTRIBUTION_MAP = {
    "Capitalizing": "Accumulating",
}


@dataclass
class XtrackersRow:
    etf_name: str
    issuer: str
    asset_class: str
    ccy: str
    ter_bps: str
    listing_date: str
    distribution: str
    isin: str
    ticker: str = ""
    aum_gbp_raw: str = ""
    as_of_date: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract the required ETF fields from a downloaded Xtrackers .xlsx file.")
    parser.add_argument("--input", type=Path, help="Downloaded Xtrackers .xlsx file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to ./xtrackers_processed.")
    parser.add_argument(
        "--enrich-tickers",
        action="store_true",
        help="Look up official Bloomberg tickers from Xtrackers product pages. This is slower than the default blank-ticker output.",
    )
    return parser.parse_args()


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(input_dir.glob("*.xlsx"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .xlsx files found in {input_dir}")
    return candidates[0]


def build_output_path(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return output_dir / f"xtrackers_selected_fields_{timestamp}.csv"


def clean_text(value: str | None) -> str:
    if value is None:
        return ""

    cleaned = value.replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -"} else cleaned


def format_decimal(value: str | Decimal | None, places: int = 2) -> str:
    if isinstance(value, Decimal):
        decimal_value = value
    else:
        cleaned = clean_text(value)
        if not cleaned:
            return ""
        try:
            decimal_value = Decimal(cleaned)
        except InvalidOperation:
            return cleaned

    quantized = decimal_value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def format_ter_bps(value: str | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    try:
        bps = Decimal(cleaned) * Decimal("100")
    except InvalidOperation:
        return cleaned

    return format_decimal(bps, places=2)


def normalize_asset_class(value: str | None) -> str:
    cleaned = clean_text(value)
    return ASSET_CLASS_MAP.get(cleaned, cleaned)


def normalize_distribution(value: str | None) -> str:
    cleaned = clean_text(value)
    return DISTRIBUTION_MAP.get(cleaned, cleaned)


def excel_serial_to_date_string(value: str | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    try:
        serial = int(Decimal(cleaned))
    except InvalidOperation:
        return cleaned

    excel_base = datetime(1899, 12, 30)
    return (excel_base + timedelta(days=serial)).date().isoformat()


def parse_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as workbook:
        shared_strings = load_shared_strings(workbook)
        sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))

    header_by_column: dict[str, str] = {}
    rows: list[dict[str, str]] = []

    for row in sheet_root.findall(".//a:sheetData/a:row", XLSX_NS):
        row_number = int(row.attrib.get("r", "0"))
        values_by_column = parse_sheet_row(row, shared_strings)

        if row_number == 7:
            header_by_column = {column: clean_text(value) for column, value in values_by_column.items() if clean_text(value)}
            continue

        if row_number < 8 or not header_by_column:
            continue

        record = {
            header_by_column[column]: value
            for column, value in values_by_column.items()
            if column in header_by_column
        }
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])) and clean_text(record.get(SOURCE_COLUMNS["isin"])):
            rows.append(record)

    return rows


def load_shared_strings(workbook: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []

    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.findall(".//a:t", XLSX_NS))
        for item in root.findall("a:si", XLSX_NS)
    ]


def parse_sheet_row(row: ET.Element, shared_strings: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}

    for cell in row.findall("a:c", XLSX_NS):
        column = extract_column_letters(cell.attrib.get("r", ""))
        if not column:
            continue

        cell_type = cell.attrib.get("t")
        raw_value = cell.find("a:v", XLSX_NS)

        if cell_type == "s" and raw_value is not None and raw_value.text is not None:
            value = shared_strings[int(raw_value.text)]
        elif cell_type == "inlineStr":
            value = "".join(node.text or "" for node in cell.findall(".//a:t", XLSX_NS))
        else:
            value = "" if raw_value is None or raw_value.text is None else raw_value.text

        values[column] = value

    return values


def extract_column_letters(cell_reference: str) -> str:
    match = re.match(r"[A-Z]+", cell_reference)
    return match.group(0) if match else ""


def transform_workbook_rows(records: list[dict[str, str]]) -> list[XtrackersRow]:
    return [
        XtrackersRow(
            etf_name=clean_text(record.get(SOURCE_COLUMNS["fund_name"])),
            issuer="Xtrackers",
            asset_class=normalize_asset_class(record.get(SOURCE_COLUMNS["asset_class"])),
            ccy=clean_text(record.get(SOURCE_COLUMNS["currency"])).upper(),
            ter_bps=format_ter_bps(record.get(SOURCE_COLUMNS["ter_percent"])),
            listing_date=excel_serial_to_date_string(record.get(SOURCE_COLUMNS["launch_date"])),
            distribution=normalize_distribution(record.get(SOURCE_COLUMNS["distribution"])),
            isin=clean_text(record.get(SOURCE_COLUMNS["isin"])),
            aum_gbp_raw=clean_text(record.get(SOURCE_COLUMNS["aum_gbp"])),
            as_of_date=excel_serial_to_date_string(record.get(SOURCE_COLUMNS["as_of_date"])),
        )
        for record in records
    ]


async def accept_product_finder_gate(page: Page) -> None:
    for selector in ("#consent_prompt_submit", "button:has-text('Accept & continue')"):
        try:
            button = page.locator(selector).first
            if await button.is_visible():
                await button.click(force=True)
                await page.wait_for_timeout(1_000)
        except Exception:
            continue


async def fetch_product_url_map(page: Page) -> dict[str, str]:
    loop = asyncio.get_running_loop()
    datatable_future: asyncio.Future[dict[str, object]] = loop.create_future()

    async def capture_response(response) -> None:
        if datatable_future.done():
            return
        if response.status != 200 or not response.url.startswith(DATATABLE_API_URL):
            return

        try:
            payload = await response.json()
        except Exception:
            return

        if payload.get("values"):
            datatable_future.set_result(payload)

    page.on("response", lambda response: asyncio.create_task(capture_response(response)))

    await page.goto(PRODUCT_FINDER_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(3_000)
    await accept_product_finder_gate(page)

    payload = await asyncio.wait_for(datatable_future, timeout=45)
    product_urls: dict[str, str] = {}

    for item in payload.get("values", []):
        isin = clean_text(item.get("ID", {}).get("value"))
        url = clean_text(item.get("column_0", {}).get("column_0_0", {}).get("value", {}).get("url"))
        if isin and url:
            product_urls[isin] = url

    return product_urls


async def extract_bloomberg_ticker(page: Page, product_url: str, target_ccy: str) -> str:
    full_url = product_url if product_url.startswith("http") else f"{PRODUCT_BASE_URL}{product_url}"

    await page.goto(full_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    await accept_product_finder_gate(page)

    try:
        await page.wait_for_function(
            "() => Array.from(document.querySelectorAll('th')).some(th => th.innerText.toLowerCase().includes('bloomberg'))",
            timeout=20_000,
        )
    except Exception:
        return ""

    return clean_text(
        await page.evaluate(
            """
            (currencyTarget) => {
                const normalize = (value) =>
                    (value || "")
                        .replaceAll("\\u00ad", "")
                        .replace(/\\s+/g, " ")
                        .trim();

                const target = normalize(currencyTarget).toUpperCase();

                for (const table of document.querySelectorAll("table")) {
                    const headers = Array.from(table.querySelectorAll("th")).map((th) => normalize(th.innerText));
                    const bloombergIndex = headers.findIndex((header) => header.toLowerCase().includes("bloomberg ticker"));
                    if (bloombergIndex === -1) {
                        continue;
                    }

                    const currencyIndex = headers.findIndex((header) => header.toLowerCase() === "currency");
                    const rows = Array.from(table.querySelectorAll("tbody tr")).map((tr) =>
                        Array.from(tr.querySelectorAll("td")).map((td) => normalize(td.innerText))
                    );

                    for (const row of rows) {
                        const ticker = row[bloombergIndex] || "";
                        const rowCurrency = currencyIndex >= 0 ? (row[currencyIndex] || "").toUpperCase() : "";
                        if (ticker && rowCurrency === target) {
                            return ticker;
                        }
                    }

                    for (const row of rows) {
                        const ticker = row[bloombergIndex] || "";
                        if (ticker) {
                            return ticker;
                        }
                    }
                }

                return "";
            }
            """,
            target_ccy,
        )
    )


async def enrich_tickers(rows: list[XtrackersRow]) -> dict[str, str]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1200},
        )

        async def route_handler(route) -> None:
            url = route.request.url
            resource_type = route.request.resource_type
            if (
                resource_type in {"image", "font", "media"}
                or "linkedin" in url
                or "doubleclick" in url
                or "mit.dws.com" in url
                or "google.com/ccm" in url
                or "performancechart" in url
                or "holdings" in url
            ):
                await route.abort()
                return

            await route.continue_()

        await context.route("**/*", route_handler)

        bootstrap_page = await context.new_page()
        print("[1/3] Loading Xtrackers product finder and capturing product URLs ...")
        product_url_map = await fetch_product_url_map(bootstrap_page)
        await bootstrap_page.close()
        print(f"    Product URLs captured: {len(product_url_map):,}")

        pending_rows = [row for row in rows if row.isin in product_url_map]
        print("[2/3] Looking up official product-page tickers ...")
        print(f"    Pending ticker lookups: {len(pending_rows):,}")

        ticker_map: dict[str, str] = {}
        queue: asyncio.Queue[XtrackersRow | None] = asyncio.Queue()
        for row in pending_rows:
            queue.put_nowait(row)

        async def worker(worker_number: int) -> None:
            page = await context.new_page()
            processed = 0
            try:
                while True:
                    row = await queue.get()
                    if row is None:
                        queue.task_done()
                        break

                    try:
                        ticker = await extract_bloomberg_ticker(page, product_url_map[row.isin], row.ccy)
                        if ticker:
                            ticker_map[row.isin] = ticker
                    except Exception as exc:
                        print(f"    Worker {worker_number}: ticker lookup failed for {row.isin}: {exc}")
                    finally:
                        processed += 1
                        if processed % 20 == 0:
                            print(f"    Worker {worker_number}: processed {processed} pages")
                        queue.task_done()
            finally:
                await page.close()

        workers = [asyncio.create_task(worker(index + 1)) for index in range(TICKER_CONCURRENCY)]
        for _ in workers:
            queue.put_nowait(None)

        await queue.join()
        await asyncio.gather(*workers)

        print("[3/3] Ticker enrichment complete.")
        print(f"    Tickers found: {len(ticker_map):,}")

        await browser.close()
        return ticker_map


def load_ecb_rates() -> dict[date, dict[str, Decimal]]:
    root = ET.fromstring(urllib.request.urlopen(ECB_RATES_URL, timeout=60).read())
    rates_by_date: dict[date, dict[str, Decimal]] = {}

    for daily_cube in root.findall(".//fx:Cube[@time]", ECB_NS):
        cube_date = date.fromisoformat(daily_cube.attrib["time"])
        daily_rates = {"EUR": Decimal("1")}

        for currency_cube in daily_cube.findall("fx:Cube", ECB_NS):
            daily_rates[currency_cube.attrib["currency"]] = Decimal(currency_cube.attrib["rate"])

        rates_by_date[cube_date] = daily_rates

    if not rates_by_date:
        raise RuntimeError("ECB FX feed returned no rates.")

    return rates_by_date


def find_rate_date(preferred_date: date | None, rates_by_date: dict[date, dict[str, Decimal]]) -> date:
    available_dates = sorted(rates_by_date)
    if not available_dates:
        raise RuntimeError("No ECB FX dates available.")

    target_date = preferred_date or available_dates[-1]
    valid_dates = [current_date for current_date in available_dates if current_date <= target_date]
    return valid_dates[-1] if valid_dates else available_dates[0]


def convert_gbp_to_target_millions(
    amount_gbp_raw: str,
    target_ccy: str,
    preferred_date_text: str,
    fallback_date_text: str,
    rates_by_date: dict[date, dict[str, Decimal]],
) -> str:
    cleaned_amount = clean_text(amount_gbp_raw)
    target = clean_text(target_ccy).upper()
    if not cleaned_amount or not target:
        return ""

    try:
        amount_gbp = Decimal(cleaned_amount)
    except InvalidOperation:
        return ""

    rate_date_text = clean_text(preferred_date_text) or clean_text(fallback_date_text)
    preferred_date = date.fromisoformat(rate_date_text) if rate_date_text else None
    daily_rates = rates_by_date[find_rate_date(preferred_date, rates_by_date)]

    if target == "GBP":
        amount_target = amount_gbp
    else:
        if "GBP" not in daily_rates or target not in daily_rates:
            raise RuntimeError(f"ECB rates do not include the required pair GBP/{target}.")
        amount_target = (amount_gbp / daily_rates["GBP"]) * daily_rates[target]

    return format_decimal(amount_target / Decimal("1000000"), places=2)


def build_output_row(row: XtrackersRow, rates_by_date: dict[date, dict[str, Decimal]], fallback_fx_date_text: str) -> dict[str, str]:
    return {
        "ETF Name": row.etf_name,
        "Issuer": row.issuer,
        "Asset Class": row.asset_class,
        "CCY": row.ccy,
        "TER (bps)": row.ter_bps,
        "Listing Date": row.listing_date,
        "Distribution": row.distribution,
        "ISIN": row.isin,
        "Ticker": row.ticker,
        "AUM": convert_gbp_to_target_millions(row.aum_gbp_raw, row.ccy, row.as_of_date, fallback_fx_date_text, rates_by_date),
    }


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


async def async_main() -> None:
    args = parse_args()
    input_path = args.input.resolve() if args.input else find_latest_download(INPUT_DIR)
    output_path = args.output.resolve() if args.output else build_output_path(OUTPUT_DIR)
    fallback_fx_date_text = datetime.fromtimestamp(input_path.stat().st_mtime).date().isoformat()

    print("Parsing downloaded Xtrackers workbook ...")
    rows = transform_workbook_rows(parse_xlsx_rows(input_path))
    print(f"    ETF rows found: {len(rows):,}")

    if args.enrich_tickers:
        ticker_map = await enrich_tickers(rows)
        for row in rows:
            row.ticker = ticker_map.get(row.isin, "")
    else:
        print("Skipping ticker enrichment. Ticker column will be left blank.")

    print("Loading official ECB FX rates for AUM conversion ...")
    rates_by_date = load_ecb_rates()
    print(f"    FX dates loaded: {len(rates_by_date):,}")

    output_rows = [build_output_row(row, rates_by_date, fallback_fx_date_text) for row in rows]
    write_csv(output_path, output_rows)

    print(f"Source file : {input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {output_path}")
    print(f"Ticker mode : {'Official enrichment enabled' if args.enrich_tickers else 'Blank ticker column'}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
