"""Extract the selected ETF fields from the latest downloaded J.P. Morgan snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
ISSUER = "J.P. Morgan Asset Management"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

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
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded J.P. Morgan .json snapshot.")
    parser.add_argument("--input", type=Path, help="Downloaded J.P. Morgan .json snapshot. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a date folder inside ./jpmorgan.")
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
    candidates = sorted((path for path in input_dir.rglob("*.json") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .json files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "jpmorgan_selected_fields.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


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


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected J.P. Morgan snapshot in {path}: expected a list.")
    return payload


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def is_etf_row(row: dict[str, object]) -> bool:
    return clean_text(row.get("categoryCode")) == "ETF" or clean_text(row.get("fundTypeCode")) == "N_ETF"


def format_ter_bps(value: object | None) -> str:
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        try:
            return format_decimal(Decimal(str(value)) * Decimal("100"), places=2)
        except InvalidOperation:
            return ""

    cleaned = clean_text(value).replace("%", "").strip()
    if not cleaned:
        return ""

    if cleaned.lower().endswith("bps"):
        cleaned = cleaned[:-3].strip()
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return format_decimal(Decimal(cleaned), places=2)
        except InvalidOperation:
            return ""

    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return format_decimal(Decimal(cleaned) * Decimal("100"), places=2)
    except InvalidOperation:
        return ""


def normalize_aum_millions(value: object | None) -> str:
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        try:
            return format_decimal(Decimal(str(value)) / Decimal("1000000"), places=2)
        except InvalidOperation:
            return ""

    cleaned = clean_text(value)
    if not cleaned:
        return ""

    cleaned = cleaned.replace("\u00a3", "").replace("$", "").replace("€", "")
    cleaned = re.sub(r"\b[A-Z]{3}\b", "", cleaned).strip()
    compact = cleaned.lower().replace(" ", "").replace(",", "")

    multiplier = Decimal("0.000001")
    if compact.endswith("bn"):
        multiplier = Decimal("1000")
        compact = compact[:-2]
    elif compact.endswith("b"):
        multiplier = Decimal("1000")
        compact = compact[:-1]
    elif compact.endswith("million"):
        multiplier = Decimal("1")
        compact = compact[:-7]
    elif compact.endswith("mn"):
        multiplier = Decimal("1")
        compact = compact[:-2]
    elif compact.endswith("m"):
        multiplier = Decimal("1")
        compact = compact[:-1]

    if "," in compact and "." not in compact:
        compact = compact.replace(",", ".")

    try:
        amount = Decimal(compact)
    except InvalidOperation:
        return ""

    return format_decimal(amount * multiplier, places=2)


def transform_row(source_row: dict[str, object], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get("shareclassName")) or clean_text(source_row.get("displayName")),
        "Issuer": ISSUER,
        "ISIN": clean_text(source_row.get("identifier")).upper(),
        "CCY": clean_text(source_row.get("shareclassCurrencyCode") or source_row.get("currencyCode")).upper(),
        "TER(bps)": format_ter_bps(source_row.get("ongoingCharge")),
        "AUM(M)": normalize_aum_millions(source_row.get("assetsUnderManagement")),
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
    rows = parse_snapshot_rows(resolved_input_path)
    file_date = extract_file_date(resolved_input_path)
    return [transform_row(row, file_date) for row in rows if is_etf_row(row)]


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
