"""Extract the selected ETF fields from the latest downloaded UBS file."""

from __future__ import annotations

import argparse
import csv
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zipfile import ZipFile


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "UBS_etf_downloads"
OUTPUT_DIR = BASE_DIR / "UBS_processed"

XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

SOURCE_COLUMNS = {
    "fund_name": "Fund Name",
    "isin": "ISIN",
    "currency": "Currency",
    "ter": "TER (flat fee)(%)",
    "aum": "AUM(M)",
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
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded UBS .xlsx file.")
    parser.add_argument("--input", type=Path, help="Downloaded UBS .xlsx file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to ./UBS_processed.")
    return parser.parse_args()


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(input_dir.glob("*.xlsx"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .xlsx files found in {input_dir}")
    return candidates[0]


def build_output_path(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return output_dir / f"ubs_selected_fields_{timestamp}.csv"


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
        cleaned = cleaned.replace(",", "")
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
    for pattern in ("%Y%m%d_%H%M%S", "%d%m%Y_%H%M%S"):
        match = re.search(r"(\d{8}_\d{6})", input_path.stem)
        if not match:
            return ""

        timestamp = match.group(1)
        try:
            return datetime.strptime(timestamp, pattern).strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue

    return match.group(1) if match else ""


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
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])) and clean_text(record.get(SOURCE_COLUMNS["isin"])):
            rows.append(record)

    return rows


def transform_row(source_row: dict[str, str], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        "Issuer": "UBS",
        "ISIN": clean_text(source_row.get(SOURCE_COLUMNS["isin"])).upper(),
        "CCY": clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper(),
        "TER(bps)": format_ter(source_row.get(SOURCE_COLUMNS["ter"])),
        "AUM(M)": format_decimal(source_row.get(SOURCE_COLUMNS["aum"])),
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
    return [transform_row(row, file_date) for row in rows]


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(OUTPUT_DIR)

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
