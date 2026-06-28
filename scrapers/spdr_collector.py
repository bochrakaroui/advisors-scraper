"""Collect SPDR / State Street Global Advisors ETF data from the official SSGA website."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import shutil
import re
from urllib.error import URLError
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zipfile import ZipFile


PAGE_URL = "https://www.ssga.com/uk/en_gb/intermediary/etfs/fund-finder"
XLSX_URL = "https://www.ssga.com/uk/en_gb/intermediary/library-content/products/fund-data/etfs/emea/spdr-product-data-emea-en.xlsx"
FUND_FINDER_API_URL = "https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=uk&language=en_gb&role=intermediary&product=&ui=fund-finder"
ISSUER = "SPDR / State Street Global Advisors"

BASE_DIR = Path(__file__).resolve().parents[1]
PROVIDER_DIR = BASE_DIR / "providers" / "SPDR"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

SOURCE_COLUMNS = {
    "fund_name": "Fund Name",
    "isin": "ISIN",
    "aum_raw": "Total Fund Assets Raw",
    "currency": "Share Class Currency",
}

OUTPUT_COLUMNS = ["etf_name", "issuer", "isin", "ccy", "aum_mn"]
PREPARED_RUN_DIRS: set[Path] = set()


@dataclass(frozen=True)
class SpdrRow:
    etf_name: str
    issuer: str
    isin: str
    ccy: str
    aum_mn: str


def build_run_output_dir(base_dir: Path, run_date: str) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    resolved_output_dir = output_dir.resolve()
    if resolved_output_dir not in PREPARED_RUN_DIRS:
        output_dir.mkdir(parents=True, exist_ok=True)
        PREPARED_RUN_DIRS.add(resolved_output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def timestamp_now() -> datetime:
    return datetime.now()


def build_raw_path(now: datetime) -> Path:
    return build_run_output_dir(PROVIDER_DIR, now.strftime("%Y-%m-%d")) / "spdr_product_data.xlsx"


def build_processed_path(now: datetime) -> Path:
    return build_run_output_dir(PROVIDER_DIR, now.strftime("%Y-%m-%d")) / "spdr_etfs.csv"


def download_raw_xlsx(destination: Path) -> None:
    logging.info("Method used: official XLSX download")
    logging.info("Source page: %s", PAGE_URL)
    logging.info("Downloading raw XLSX: %s", XLSX_URL)
    request = urllib.request.Request(
        XLSX_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        destination.write_bytes(response.read())
    logging.info("Raw file saved: %s", destination)


async def download_spdr_file() -> Path:
    now = timestamp_now()
    raw_path = build_raw_path(now)
    await asyncio.to_thread(download_raw_xlsx, raw_path)
    return raw_path


def load_shared_strings(workbook: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []

    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.findall(".//a:t", XLSX_NS))
        for item in root.findall("a:si", XLSX_NS)
    ]


def extract_column_letters(cell_reference: str) -> str:
    match = re.match(r"[A-Z]+", cell_reference)
    return match.group(0) if match else ""


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


def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("\u00ad", "").strip()


def parse_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as workbook:
        shared_strings = load_shared_strings(workbook)
        sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))

    header_by_column: dict[str, str] = {}
    rows: list[dict[str, str]] = []

    for row in sheet_root.findall(".//a:sheetData/a:row", XLSX_NS):
        row_number = int(row.attrib.get("r", "0"))
        values_by_column = parse_sheet_row(row, shared_strings)

        if not values_by_column:
            continue

        row_values = [clean_text(value) for value in values_by_column.values()]
        if SOURCE_COLUMNS["fund_name"] in row_values and SOURCE_COLUMNS["isin"] in row_values:
            header_by_column = {
                column: clean_text(value)
                for column, value in values_by_column.items()
                if clean_text(value)
            }
            continue

        if row_number <= 3 or not header_by_column:
            continue

        record = {
            header_by_column[column]: value
            for column, value in values_by_column.items()
            if column in header_by_column
        }
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])) and clean_text(record.get(SOURCE_COLUMNS["isin"])):
            rows.append(record)

    return rows


def extract_spdr_isin(source_row: dict[str, object]) -> str:
    for document_group in source_row.get("documentPdf", []) or []:
        if clean_text(document_group.get("docType")) != "Key-investor-information":
            continue
        for document in document_group.get("docs", []) or []:
            match = re.search(r"isin=([A-Z0-9]{12})", clean_text(document.get("path")), flags=re.IGNORECASE)
            if match:
                return match.group(1).upper()

    match = re.search(r"\b([A-Z]{2}[A-Z0-9]{10})\b", clean_text(source_row.get("keywords")))
    return match.group(1).upper() if match else ""


def infer_currency_from_value(value: object) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    if cleaned.startswith("MXN"):
        return "MXN"
    if cleaned.startswith("CHF"):
        return "CHF"
    if cleaned.startswith("JPY"):
        return "JPY"

    first_char = cleaned[0]
    if first_char == "$":
        return "USD"
    if first_char == "€":
        return "EUR"
    if ord(first_char) == 163:
        return "GBP"

    return ""


def parse_currency_map_from_payload(payload: dict[str, object]) -> dict[str, str]:
    items = (
        payload.get("data", {})
        .get("funds", {})
        .get("uk-etfs", {})
        .get("datas", [])
    )
    currency_map: dict[str, str] = {}

    for item in items:
        isin = extract_spdr_isin(item)
        if not isin:
            continue

        ccy = ""
        for field_name in ("nav", "aum", "closePrice", "bidPrice", "offerPrice"):
            ccy = infer_currency_from_value(item.get(field_name))
            if ccy:
                break

        if ccy:
            currency_map[isin] = ccy

    return currency_map


def fetch_spdr_currency_map(xlsx_path: Path | None = None) -> dict[str, str]:
    request = urllib.request.Request(
        FUND_FINDER_API_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
    except URLError as exc:
        logging.warning("Unable to load the SPDR fund finder JSON for currency backfill: %s", exc)
        return {}
    return parse_currency_map_from_payload(payload)


def format_decimal(value: str | Decimal | None, places: int = 2) -> str:
    if isinstance(value, Decimal):
        decimal_value = value
    else:
        cleaned = clean_text(value)
        if not cleaned:
            return ""
        cleaned = cleaned.replace(",", "")
        try:
            decimal_value = Decimal(cleaned)
        except InvalidOperation:
            return ""

    quantized = decimal_value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def convert_aum_to_millions(total_fund_assets_raw: str | None) -> str:
    cleaned = clean_text(total_fund_assets_raw)
    if not cleaned:
        return ""

    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return ""

    return format_decimal(amount / Decimal("1000000"), places=2)


def transform_row(source_row: dict[str, str], currency_overrides: dict[str, str]) -> SpdrRow:
    isin = clean_text(source_row.get(SOURCE_COLUMNS["isin"])).upper()
    return SpdrRow(
        etf_name=clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        issuer=ISSUER,
        isin=isin,
        ccy=clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper() or currency_overrides.get(isin, ""),
        aum_mn=convert_aum_to_millions(source_row.get(SOURCE_COLUMNS["aum_raw"])),
    )


def is_valid_isin(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", value))


def validate_rows(rows: list[SpdrRow]) -> list[SpdrRow]:
    valid_rows: list[SpdrRow] = []
    skipped = 0

    for row in rows:
        if not row.etf_name:
            skipped += 1
            logging.warning("Skipping row with empty etf_name")
            continue
        if not row.issuer:
            skipped += 1
            logging.warning("Skipping %s because issuer is empty", row.etf_name)
            continue
        if not is_valid_isin(row.isin):
            skipped += 1
            logging.warning("Skipping %s because ISIN is invalid: %r", row.etf_name, row.isin)
            continue
        if row.ccy == "":
            skipped += 1
            logging.warning("Skipping %s because ccy is empty", row.etf_name)
            continue
        if row.aum_mn:
            try:
                Decimal(row.aum_mn)
            except InvalidOperation:
                skipped += 1
                logging.warning("Skipping %s because aum_mn is not numeric: %r", row.etf_name, row.aum_mn)
                continue

        valid_rows.append(row)

    logging.info("Validation complete. Valid rows: %s, skipped rows: %s", len(valid_rows), skipped)
    return valid_rows


def write_processed_csv(output_path: Path, rows: list[SpdrRow]) -> None:
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "etf_name": row.etf_name,
                    "issuer": row.issuer,
                    "isin": row.isin,
                    "ccy": row.ccy,
                    "aum_mn": row.aum_mn,
                }
            )
    logging.info("Processed CSV saved: %s", output_path)


def main() -> None:
    setup_logging()
    now = timestamp_now()
    raw_path = build_raw_path(now)
    processed_path = build_processed_path(now)

    download_raw_xlsx(raw_path)
    source_rows = parse_xlsx_rows(raw_path)
    logging.info("Parsed source rows: %s", len(source_rows))

    currency_overrides = fetch_spdr_currency_map(raw_path)
    transformed_rows = [transform_row(row, currency_overrides) for row in source_rows]
    valid_rows = validate_rows(transformed_rows)
    write_processed_csv(processed_path, valid_rows)

    logging.info("Done.")
    logging.info("Source page used: %s", PAGE_URL)
    logging.info("Raw file: %s", raw_path)
    logging.info("Processed file: %s", processed_path)


if __name__ == "__main__":
    main()
