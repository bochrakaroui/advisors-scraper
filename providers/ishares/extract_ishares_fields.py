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
from zipfile import ZipFile

from src.source_freshness import load_source_metadata, normalize_source_date, write_source_metadata


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

SPREADSHEET_NS = "urn:schemas-microsoft-com:office:spreadsheet"
NS = {"ss": SPREADSHEET_NS}
SS_INDEX = f"{{{SPREADSHEET_NS}}}Index"
XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

SOURCE_COLUMNS = {
    "fund_name": "Fund Name",
    "fund_type": "Fund type",
    "isin": "ISIN",
    "currency": "Share Class Currency",
    "ter": "TER / OCF",
    "issuer": "Issuing Company",
    "aum": "AUM (M)",
    "aum_as_of": "AUM As Of",
    "net_assets": "Net Assets",
    "shares_outstanding": "Shares Outstanding",
}

ZERO_AUM_FALLBACK_ISINS = {
    "GB00BV4B0K53",
    "GB00BV4B0N84",
    "GB00BV4B0M77",
    "GB00BV4B0P09",
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
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded iShares workbook.")
    parser.add_argument("--input", type=Path, help="Downloaded iShares .xls or .xlsx file. Defaults to the latest file.")
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
    candidates = sorted(
        (
            path
            for path in input_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".xls", ".xlsx"}
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No iShares .xls or .xlsx files found in {input_dir}")
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


def extract_column_letters(cell_reference: str) -> str:
    match = re.match(r"[A-Z]+", cell_reference)
    return match.group(0) if match else ""


def column_key_to_index(column_key: int | str) -> int:
    if isinstance(column_key, int):
        return column_key

    index = 0
    for character in str(column_key).upper():
        if not ("A" <= character <= "Z"):
            continue
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index


def disambiguate_header(column_index: int, header_value: str, headers: dict[int, str]) -> str:
    if header_value == "As Of" and headers.get(column_index - 1) == SOURCE_COLUMNS["aum"]:
        return SOURCE_COLUMNS["aum_as_of"]
    return header_value


def merge_header_values(
    values_by_column: dict[int | str, str],
    headers: dict[int | str, str],
) -> dict[int | str, str]:
    updated_headers = dict(headers)
    for column_key in sorted(values_by_column, key=column_key_to_index):
        normalized_value = normalize_header(values_by_column[column_key])
        if not normalized_value:
            continue
        updated_headers[column_key] = disambiguate_header(
            column_key_to_index(column_key),
            normalized_value,
            {
                column_key_to_index(existing_key): existing_value
                for existing_key, existing_value in updated_headers.items()
            },
        )
    return updated_headers


def has_required_headers(headers: dict[int | str, str]) -> bool:
    return all(column in headers.values() for column in SOURCE_COLUMNS.values())


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


def format_source_date(value: str | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return cleaned


def parse_numeric_decimal(value: str | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None

    normalized = cleaned.replace(",", "")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def resolve_aum_m(source_row: dict[str, str]) -> str:
    direct_aum = format_decimal(source_row.get(SOURCE_COLUMNS["aum"]))
    if direct_aum:
        return direct_aum

    net_assets = parse_numeric_decimal(source_row.get(SOURCE_COLUMNS["net_assets"]))
    if net_assets is not None:
        return format_decimal(str(net_assets / Decimal("1000000")))

    isin = clean_text(source_row.get(SOURCE_COLUMNS["isin"])).upper()
    shares_outstanding = clean_text(source_row.get(SOURCE_COLUMNS["shares_outstanding"]))
    if isin in ZERO_AUM_FALLBACK_ISINS and not shares_outstanding:
        # The official iShares workbook carries "-" for AUM, net assets, and
        # shares outstanding on these newly added UK share classes. Treat
        # that combination as zero assets rather than leaving the field blank.
        return "0.00"

    return ""


def parse_xml_spreadsheet(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".xlsx":
        return parse_xlsx_spreadsheet(path)

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
        headers = merge_header_values(read_sparse_row(row_element), headers)

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


def load_shared_strings(workbook: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []

    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.findall(".//a:t", XLSX_NS))
        for item in root.findall("a:si", XLSX_NS)
    ]


def parse_xlsx_sheet_row(row: ET.Element, shared_strings: list[str]) -> dict[str, str]:
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


def parse_xlsx_spreadsheet(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as workbook:
        shared_strings = load_shared_strings(workbook)
        sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))

    headers: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    header_complete = False

    for row in sheet_root.findall(".//a:sheetData/a:row", XLSX_NS):
        values_by_column = parse_xlsx_sheet_row(row, shared_strings)
        if not values_by_column:
            continue

        if not header_complete:
            prospective_headers = merge_header_values(values_by_column, headers)
            if has_required_headers(prospective_headers):
                headers = prospective_headers
                header_complete = True
            elif prospective_headers != headers:
                headers = prospective_headers
            continue

        record = {
            headers[column]: value
            for column, value in values_by_column.items()
            if column in headers
        }
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])):
            rows.append(record)

    if not header_complete:
        missing_headers = [column for column in SOURCE_COLUMNS.values() if column not in headers.values()]
        raise ValueError(f"Missing expected source columns in {path}: {', '.join(missing_headers)}")

    return rows


def transform_row(source_row: dict[str, str], file_date: str) -> dict[str, str]:
    share_class_currency = clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper()
    as_of_date = format_source_date(source_row.get(SOURCE_COLUMNS["aum_as_of"])) or file_date
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        "Issuer": clean_text(source_row.get(SOURCE_COLUMNS["issuer"])),
        "ISIN": clean_text(source_row.get(SOURCE_COLUMNS["isin"])).upper(),
        "CCY": share_class_currency,
        "TER(bps)": format_ter(source_row.get(SOURCE_COLUMNS["ter"])),
        "AUM(M)": resolve_aum_m(source_row),
        "AUM CCY": share_class_currency,
        "Date": as_of_date,
    }


def update_source_metadata(input_path: Path, rows: list[dict[str, str]]) -> None:
    source_dates = sorted(
        {
            normalize_source_date(row.get("Date"))
            for row in rows
            if normalize_source_date(row.get("Date"))
        }
    )
    if not source_dates:
        return

    metadata = load_source_metadata(input_path)
    metadata.update(
        {
            "source_date": source_dates[-1],
            "freshness_status": "CURRENT",
            "freshness_proof": "Workbook AUM As Of date from live iShares export",
        }
    )
    write_source_metadata(input_path, metadata)


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
    extracted_rows = [transform_row(row, file_date) for row in filtered_rows]
    update_source_metadata(resolved_input_path, extracted_rows)
    return extracted_rows


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
