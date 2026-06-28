"""Extract the selected ETF fields from the latest downloaded iShares file."""

from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import xml.etree.ElementTree as ET


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

SPREADSHEET_NS = "urn:schemas-microsoft-com:office:spreadsheet"
NS = {"ss": SPREADSHEET_NS}
SS_INDEX = f"{{{SPREADSHEET_NS}}}Index"

SOURCE_COLUMNS = {
    "fund_name": "Fund Name",
    "fund_type": "Fund type",
    "isin": "ISIN",
    "currency": "Share Class Currency",
    "ter": "TER / OCF",
    "issuer": "Issuing Company",
    "aum": "AUM (M)",
}

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded iShares .xls file.")
    parser.add_argument("--input", type=Path, help="Downloaded iShares .xls file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a date folder inside ./ishares.")
    parser.add_argument(
        "--etf-only",
        action="store_true",
        help="Keep ETF rows only. By default the script keeps all source rows.",
    )
    return parser.parse_args()


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

def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted((path for path in input_dir.rglob("*.xls") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .xls files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path, include_all_funds: bool) -> Path:
    mode = "all_funds" if include_all_funds else "etf_only"
    return input_path.parent / f"ishares_selected_fields_{mode}.csv"


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


def clean_text(value: str | None) -> str:
    if value is None:
        return ""

    cleaned = value.strip()
    return "" if cleaned in {"", "-", "- ", " -"} else cleaned


def normalize_header(value: str | None) -> str:
    cleaned = clean_text(value)
    return re.sub(r"\s+", " ", cleaned)


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


def format_ter(value: str | None) -> str:
    cleaned = clean_text(value).replace("%", "").strip()
    if not cleaned:
        return ""
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return format_decimal(str(Decimal(cleaned) * Decimal("100")), places=2)
    except InvalidOperation:
        return cleaned


def extract_file_date(input_path: Path) -> str:
    match = re.search(r"(\d{8}_\d{6})", input_path.stem)
    if not match:
        parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
        if not parent_date_match:
            return ""
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")

    timestamp = match.group(1)
    try:
        return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%d/%m/%Y")
    except ValueError:
        return timestamp


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
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])):
            rows.append(record)

    return rows


def transform_row(source_row: dict[str, str], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        "Issuer": clean_text(source_row.get(SOURCE_COLUMNS["issuer"])),
        "ISIN": clean_text(source_row.get(SOURCE_COLUMNS["isin"])).upper(),
        "CCY": clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper(),
        "TER(bps)": format_ter(source_row.get(SOURCE_COLUMNS["ter"])),
        "AUM(M)": format_decimal(source_row.get(SOURCE_COLUMNS["aum"])),
        "Date": file_date,
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


def extract_rows(input_path: Path | None = None, *, include_all_funds: bool = False) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    rows = parse_xml_spreadsheet(resolved_input_path)
    filtered_rows = filter_rows(rows, include_all_funds=include_all_funds)
    file_date = extract_file_date(resolved_input_path)
    return [transform_row(row, file_date) for row in filtered_rows]


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
    *,
    include_all_funds: bool = False,
) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = (
        output_path.resolve()
        if output_path
        else build_output_path(resolved_input_path, include_all_funds=include_all_funds)
    )

    output_rows = extract_rows(resolved_input_path, include_all_funds=include_all_funds)

    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {resolved_output_path}")
    print(f"Filter      : {'All funds' if include_all_funds else 'ETF rows only'}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output, include_all_funds=not args.etf_only)


if __name__ == "__main__":
    main()
