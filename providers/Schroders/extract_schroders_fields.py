"""Extract the selected ETF fields from the latest Schroders raw JSON snapshot.

Mirrors extract_ishares_fields.py: finds the most recently scraped raw file,
pulls out the same standard set of columns, and writes a CSV next to the
source file inside its dated `providers/Schroders/<date>/` folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

RAW_FILENAME = "schroders_etf_export.json"

SOURCE_FIELDS = {
    "fund_name": "etf_name",
    "issuer": "issuer",
    "isin": "isin",
    "currency": "ccy",
    "ter": "ter_bps",
    "aum": "aum_m",
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
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded Schroders JSON snapshot.")
    parser.add_argument("--input", type=Path, help="Raw Schroders JSON file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a CSV alongside the input file.")
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="Keep rows whose extraction_method is 'failed'. By default these are dropped.",
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
        (path for path in input_dir.rglob(RAW_FILENAME) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No {RAW_FILENAME} files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "schroders_selected_fields.csv"


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    cleaned = str(value).strip()
    return "" if cleaned in {"", "-", "- ", " -", "None"} else cleaned


def format_decimal(value: Any, places: int = 2) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    try:
        decimal_value = Decimal(cleaned)
    except InvalidOperation:
        return cleaned

    quantized = decimal_value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def extract_file_date(input_path: Path) -> str:
    """Date column mirrors extract_ishares_fields.py: derived from the dated
    run folder (providers/Schroders/<date>/...), not from any in-row field,
    so it's consistent regardless of what each ISIN's own date string looked
    like (DOM-fallback rows can carry differently formatted date strings)."""
    parent_date_match = input_path.parent.name
    try:
        return datetime.strptime(parent_date_match, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


def load_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if rows is None:
        raise ValueError(f"No 'rows' key found in {path}")
    return rows


def parse_snapshot_rows(path: Path) -> list[dict[str, Any]]:
    return load_rows(path)


def transform_row(source_row: dict[str, Any], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_FIELDS["fund_name"])),
        "Issuer": clean_text(source_row.get(SOURCE_FIELDS["issuer"])),
        "ISIN": clean_text(source_row.get(SOURCE_FIELDS["isin"])).upper(),
        "CCY": clean_text(source_row.get(SOURCE_FIELDS["currency"])).upper(),
        "TER(bps)": format_decimal(source_row.get(SOURCE_FIELDS["ter"])),
        "AUM(M)": format_decimal(source_row.get(SOURCE_FIELDS["aum"])),
        "Date": file_date,
    }


def filter_rows(rows: list[dict[str, Any]], include_failed: bool) -> list[dict[str, Any]]:
    if include_failed:
        return rows
    return [row for row in rows if row.get("extraction_method") != "failed"]


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def extract_rows(input_path: Path | None = None, *, include_failed: bool = False) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    raw_rows = load_rows(resolved_input_path)
    filtered_rows = filter_rows(raw_rows, include_failed=include_failed)
    file_date = extract_file_date(resolved_input_path)
    return [transform_row(row, file_date) for row in filtered_rows]


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
    *,
    include_failed: bool = False,
) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = (
        output_path.resolve() if output_path else build_output_path(resolved_input_path)
    )

    output_rows = extract_rows(resolved_input_path, include_failed=include_failed)

    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {resolved_output_path}")
    print(f"Filter      : {'All rows (incl. failed)' if include_failed else 'Successful extractions only'}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output, include_failed=args.include_failed)


if __name__ == "__main__":
    main()
