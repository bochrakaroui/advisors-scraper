"""Extract the required ETF fields from the latest downloaded iShares file."""

from __future__ import annotations

import argparse
import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import xml.etree.ElementTree as ET


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "ishares_downloads"
OUTPUT_DIR = BASE_DIR / "ishares_processed"

SPREADSHEET_NS = "urn:schemas-microsoft-com:office:spreadsheet"
NS = {"ss": SPREADSHEET_NS}
SS_INDEX = f"{{{SPREADSHEET_NS}}}Index"

SOURCE_COLUMNS = {
    "ticker": "Ticker",
    "fund_name": "Fund Name",
    "fund_type": "Fund type",
    "isin": "ISIN",
    "currency": "Share Class Currency",
    "asset_class": "Asset Class",
    "distribution": "Distribution Type",
    "listing_date": "Inception Date",
    "issuer": "Issuing Company",
    "ter": "TER / OCF",
    "aum": "AUM (M)",
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
    "AUM(M)",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract the required ETF fields from a downloaded iShares .xls file.")
    parser.add_argument("--input", type=Path, help="Downloaded iShares .xls file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to ./ishares_processed.")
    parser.add_argument(
        "--all-funds",
        action="store_true",
        help="Include every source row. By default the script keeps ETF rows only.",
    )
    return parser.parse_args()


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(input_dir.glob("*.xls"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .xls files found in {input_dir}")
    return candidates[0]


def build_output_path(output_dir: Path, include_all_funds: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = "all_funds" if include_all_funds else "etf_only"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return output_dir / f"ishares_selected_fields_{mode}_{timestamp}.csv"


def read_sparse_row(row_element: ET.Element) -> dict[int, str]:
    values: dict[int, str] = {}
    column_index = 1

    for cell in row_element.findall("ss:Cell", NS):
        explicit_index = cell.attrib.get(SS_INDEX)
        if explicit_index:
            column_index = int(explicit_index)

        data = cell.find("ss:Data", NS)
        values[column_index] = "" if data is None or data.text is None else data.text
        column_index += 1

    return values


def normalize_header(value: str | None) -> str:
    cleaned = clean_text(value)
    return re.sub(r"\s+", " ", cleaned)


def parse_xml_spreadsheet(path: Path) -> list[dict[str, str]]:
    root = ET.parse(path).getroot()
    worksheet = root.find(".//ss:Worksheet", NS)
    if worksheet is None:
        raise ValueError(f"No worksheet found in {path}")

    table = worksheet.find("ss:Table", NS)
    if table is None:
        raise ValueError(f"No table found in {path}")

    row_elements = table.findall("ss:Row", NS)
    if len(row_elements) < 3:
        raise ValueError(f"Expected at least 3 rows in {path}, found {len(row_elements)}")

    headers: dict[int, str] = {}
    for row_element in row_elements[:2]:
        headers.update(
            {
                column_index: normalize_header(value)
                for column_index, value in read_sparse_row(row_element).items()
            }
        )

    missing_headers = [column for column in SOURCE_COLUMNS.values() if column not in headers.values()]
    if missing_headers:
        raise ValueError(f"Missing expected source columns: {', '.join(missing_headers)}")

    rows: list[dict[str, str]] = []
    for row_element in row_elements[2:]:
        sparse_values = read_sparse_row(row_element)
        if not sparse_values:
            continue

        record = {
            headers[column_index]: value
            for column_index, value in sparse_values.items()
            if column_index in headers
        }
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])) and clean_text(record.get(SOURCE_COLUMNS["isin"])):
            rows.append(record)

    return rows


def clean_text(value: str | None) -> str:
    if value is None:
        return ""

    cleaned = value.strip()
    return "" if cleaned in {"", "-", "- ", " -"} else cleaned


def format_decimal(value: str | None, places: int = 2) -> str:
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

    return format_decimal(str(bps), places=2)


def format_date(value: str | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    try:
        return datetime.fromisoformat(cleaned).date().isoformat()
    except ValueError:
        return cleaned.split("T", 1)[0]


def transform_row(source_row: dict[str, str]) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        "Issuer": clean_text(source_row.get(SOURCE_COLUMNS["issuer"])),
        "Asset Class": clean_text(source_row.get(SOURCE_COLUMNS["asset_class"])),
        "CCY": clean_text(source_row.get(SOURCE_COLUMNS["currency"])),
        "TER (bps)": format_ter_bps(source_row.get(SOURCE_COLUMNS["ter"])),
        "Listing Date": format_date(source_row.get(SOURCE_COLUMNS["listing_date"])),
        "Distribution": clean_text(source_row.get(SOURCE_COLUMNS["distribution"])),
        "ISIN": clean_text(source_row.get(SOURCE_COLUMNS["isin"])),
        "Ticker": clean_text(source_row.get(SOURCE_COLUMNS["ticker"])),
        "AUM(M)": format_decimal(source_row.get(SOURCE_COLUMNS["aum"])),
    }


def filter_rows(rows: list[dict[str, str]], include_all_funds: bool) -> list[dict[str, str]]:
    if include_all_funds:
        return rows

    return [row for row in rows if clean_text(row.get(SOURCE_COLUMNS["fund_type"])) == "ETF"]


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve() if args.input else find_latest_download(INPUT_DIR)
    output_path = args.output.resolve() if args.output else build_output_path(OUTPUT_DIR, include_all_funds=args.all_funds)

    rows = parse_xml_spreadsheet(input_path)
    filtered_rows = filter_rows(rows, include_all_funds=args.all_funds)
    output_rows = [transform_row(row) for row in filtered_rows]

    write_csv(output_path, output_rows)

    print(f"Source file : {input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {output_path}")
    print(f"Filter      : {'All funds' if args.all_funds else 'ETF rows only'}")


if __name__ == "__main__":
    main()
