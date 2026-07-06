"""Extract the selected ETF fields from the latest downloaded Invesco file."""

from __future__ import annotations

import argparse
import csv
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zipfile import ZipFile


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

SOURCE_COLUMNS = {
    "fund_name": "fundName",
    "isin": "isin",
    "currency": "currency",
    "ter": "terocf",
    "aum": "aum",
}

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "AUM CCY",
    "Date",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded Invesco .xlsx file.")
    parser.add_argument("--input", type=Path, help="Downloaded Invesco .xlsx file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a date folder inside ./invesco.")
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
    candidates = sorted((path for path in input_dir.rglob("*.xlsx") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .xlsx files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "invesco_selected_fields.csv"


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


def parse_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as workbook:
        shared_strings = load_shared_strings(workbook)
        sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))

    header_by_column: dict[str, str] = {}
    rows: list[dict[str, str]] = []

    for row in sheet_root.findall(".//a:sheetData/a:row", XLSX_NS):
        row_number = int(row.attrib.get("r", "0"))
        values_by_column = parse_sheet_row(row, shared_strings)

        if row_number == 1:
            header_by_column = {column: clean_text(value) for column, value in values_by_column.items() if clean_text(value)}
            continue

        if row_number < 2 or not header_by_column:
            continue

        record = {
            header_by_column[column]: value
            for column, value in values_by_column.items()
            if column in header_by_column
        }
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])):
            rows.append(record)

    return rows


def find_latest_nonempty_historical_aum(isin: str, current_input_path: Path) -> str:
    if not isin:
        return ""

    current_resolved = current_input_path.resolve()
    candidates = sorted(
        (
            path
            for path in INPUT_DIR.rglob("invesco_etf_export.xlsx")
            if path.is_file() and path.resolve() != current_resolved
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for candidate in candidates:
        try:
            for row in parse_xlsx_rows(candidate):
                if clean_text(row.get(SOURCE_COLUMNS["isin"])).upper() != isin:
                    continue
                aum_value = clean_text(row.get(SOURCE_COLUMNS["aum"]))
                if aum_value:
                    return aum_value
        except Exception:
            continue

    return ""


def millions_from_raw_amount(value: str | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return cleaned

    return format_decimal(amount / Decimal("1000000"), places=2)


def transform_row(source_row: dict[str, str], file_date: str, input_path: Path) -> dict[str, str]:
    isin = clean_text(source_row.get(SOURCE_COLUMNS["isin"])).upper()
    aum_raw = clean_text(source_row.get(SOURCE_COLUMNS["aum"])) or find_latest_nonempty_historical_aum(isin, input_path)
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        "Issuer": "Invesco",
        "ISIN": isin,
        "CCY": clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper(),
        "TER(bps)": format_ter(source_row.get(SOURCE_COLUMNS["ter"])),
        "AUM(M)": millions_from_raw_amount(aum_raw),
        "AUM CCY": clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper(),
        "Date": file_date,
    }


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    rows = parse_xlsx_rows(resolved_input_path)
    file_date = extract_file_date(resolved_input_path)
    return [transform_row(row, file_date, resolved_input_path) for row in rows]


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)

    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {resolved_output_path}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
