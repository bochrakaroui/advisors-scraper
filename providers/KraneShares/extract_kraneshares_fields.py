"""Extract standard ETF fields from the latest KraneShares UCITS workbook."""

from __future__ import annotations

import argparse
import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import openpyxl


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
ISSUER = "KraneShares"

SOURCE_COLUMNS = {
    "etf_name": "ETF Name",
    "issuer": "Issuer",
    "isin": "ISIN",
    "currency": "CCY",
    "ter_pct": "TER (%)",
    "aum_m": "AUM (Millions USD)",
    "date": "Date",
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

SPACE_PATTERN = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), and Date "
            "from a KraneShares UCITS workbook."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="KraneShares workbook path. Defaults to the latest kraneshares_ucits_export.xlsx file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output path. Defaults to kraneshares_selected_fields.csv next to the source workbook.",
    )
    return parser.parse_args()


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (path for path in input_dir.rglob("kraneshares_ucits_export.xlsx") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No kraneshares_ucits_export.xlsx files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "kraneshares_selected_fields.csv"


def parse_iso_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def extract_file_date(input_path: Path) -> str:
    parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if parent_date_match:
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
    return datetime.fromtimestamp(input_path.stat().st_mtime).strftime("%d/%m/%Y")


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def format_ter_bps(value: object | None) -> str:
    cleaned = clean_text(value).replace("%", "").replace(",", ".")
    if not cleaned:
        return ""
    try:
        return format_decimal(Decimal(cleaned) * Decimal("100"), places=2)
    except InvalidOperation:
        return ""


def format_aum_m(value: object | None) -> str:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned:
        return ""
    try:
        return format_decimal(Decimal(cleaned), places=2)
    except InvalidOperation:
        return ""


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook.active

    rows_iter = worksheet.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if not header_row:
        workbook.close()
        return []

    headers = [clean_text(cell) for cell in header_row]
    rows: list[dict[str, str]] = []

    for raw_row in rows_iter:
        record = {
            headers[index]: clean_text(value)
            for index, value in enumerate(raw_row)
            if index < len(headers) and headers[index]
        }
        if clean_text(record.get(SOURCE_COLUMNS["etf_name"])):
            rows.append(record)

    workbook.close()
    return rows


def transform_row(source_row: dict[str, str], fallback_date: str) -> dict[str, str]:
    row_date = parse_iso_date(source_row.get(SOURCE_COLUMNS["date"])) or fallback_date
    issuer = clean_text(source_row.get(SOURCE_COLUMNS["issuer"])) or ISSUER
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["etf_name"])),
        "Issuer": issuer,
        "ISIN": normalize_isin(source_row.get(SOURCE_COLUMNS["isin"])),
        "CCY": clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper(),
        "TER(bps)": format_ter_bps(source_row.get(SOURCE_COLUMNS["ter_pct"])),
        "AUM(M)": format_aum_m(source_row.get(SOURCE_COLUMNS["aum_m"])),
        "Date": row_date,
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(column, "") for column in OUTPUT_COLUMNS)
        deduped.setdefault(key, row)
    return list(deduped.values())


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    source_rows = parse_snapshot_rows(resolved_input_path)
    fallback_date = extract_file_date(resolved_input_path)
    return dedupe_rows([transform_row(row, fallback_date) for row in source_rows])


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing TER : {sum(1 for row in output_rows if not row.get('TER(bps)')):,}")
    print(f"Missing AUM : {sum(1 for row in output_rows if not row.get('AUM(M)')):,}")
    print(f"Output file : {resolved_output_path}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
