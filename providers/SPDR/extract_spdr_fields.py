"""Extract the selected ETF fields from the latest downloaded SPDR file."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.spdr_collector import fetch_spdr_currency_map, parse_xlsx_rows


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
ISSUER = "SPDR / State Street Global Advisors"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

SOURCE_COLUMNS = {
    "fund_name": "Fund Name",
    "currency": "Share Class Currency",
    "ter": "TER (%)",
    "aum_raw": "Total Fund Assets Raw",
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
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded SPDR .xlsx file.")
    parser.add_argument("--input", type=Path, help="Downloaded SPDR .xlsx file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a date folder inside ./SPDR.")
    return parser.parse_args()


def build_run_output_dir(base_dir: Path) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    run_date = datetime.now().strftime("%Y-%m-%d")
    output_dir = base_dir / run_date
    suffix = 1
    while output_dir.exists():
        output_dir = base_dir / f"{run_date} ({suffix})"
        suffix += 1
    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name
    return output_dir


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted((path for path in input_dir.rglob("*.xlsx") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .xlsx files found in {input_dir}")
    return candidates[0]


def build_output_path(output_dir: Path) -> Path:
    return build_run_output_dir(output_dir) / "spdr_selected_fields.csv"


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
    match = re.search(r"(\d{8}_\d{6})", input_path.stem)
    if not match:
        parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
        if not parent_date_match:
            return ""
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y 00:00:00")

    timestamp = match.group(1)
    try:
        return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return timestamp


def convert_aum_to_millions(total_fund_assets_raw: str | None) -> str:
    cleaned = clean_text(total_fund_assets_raw)
    if not cleaned:
        return ""

    try:
        amount = Decimal(cleaned.replace(",", ""))
    except InvalidOperation:
        return ""

    return format_decimal(amount / Decimal("1000000"), places=2)


def transform_row(source_row: dict[str, str], file_date: str, currency_overrides: dict[str, str]) -> dict[str, str]:
    isin = clean_text(source_row.get("ISIN")).upper()
    ccy = clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper() or currency_overrides.get(isin, "")
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        "Issuer": ISSUER,
        "ISIN": isin,
        "CCY": ccy,
        "TER(bps)": format_ter(source_row.get(SOURCE_COLUMNS["ter"])),
        "AUM(M)": convert_aum_to_millions(source_row.get(SOURCE_COLUMNS["aum_raw"])),
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
    currency_overrides = fetch_spdr_currency_map(resolved_input_path)
    return [transform_row(row, file_date, currency_overrides) for row in rows]


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
