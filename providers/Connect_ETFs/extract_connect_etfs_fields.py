"""
Connect ETFs Fields Extractor
=============================

Reads the latest raw Connect ETFs file from:

    providers/Connect_ETFs/YYYY-MM-DD/connect_etfs_export.json

Creates:

    providers/Connect_ETFs/YYYY-MM-DD/connect_etfs_selected_fields.csv

Output columns:
    ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), AUM CCY, Date
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
ISSUER = "Connect ETFs"

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

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from Connect ETFs raw JSON file."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to connect_etfs_export.json. Defaults to latest under providers/Connect_ETFs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to same folder as the raw input.",
    )
    return parser.parse_args()


def find_latest_input(input_dir: Path) -> Path:
    candidates = sorted(
        (path for path in input_dir.rglob("connect_etfs_export.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No connect_etfs_export.json found under {input_dir}. "
            "Run scrapers/Connect_ETFs_extractor.py first."
        )
    print(f"Auto-selected latest input: {candidates[0]}")
    return candidates[0]


def find_latest_download(input_dir: Path) -> Path:
    return find_latest_input(input_dir)


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "connect_etfs_selected_fields.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    return "" if text in {"", "-", "--", "- ", " -", "nan", "NaN", "None", "null"} else text


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def is_valid_isin(value: object | None) -> bool:
    return bool(ISIN_RE.fullmatch(normalize_isin(value)))


def decimal_from_value(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    cleaned = cleaned.replace("%", "").strip()
    cleaned = re.sub(r"[^\d.,-]", "", cleaned)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantizer = Decimal("1." + "0" * places)
    return format(value.quantize(quantizer, rounding=ROUND_HALF_UP), f".{places}f")


def format_aum_m(value: object | None) -> str:
    d = decimal_from_value(value)
    if d is None:
        return ""
    return format_decimal(d, places=2)


def format_ter_bps(value: object | None) -> str:
    d = decimal_from_value(value)
    if d is None:
        return ""
    return format_decimal(d, places=2)


def infer_aum_currency(raw_row: dict) -> str:
    for key in ("aum_ccy", "aum_currency", "assets_currency", "net_assets_currency", "base_currency"):
        currency = clean_text(raw_row.get(key)).upper()
        if currency:
            return currency
    return clean_text(raw_row.get("ccy")).upper()


def normalize_date(value: object | None, fallback_path: Path) -> str:
    cleaned = clean_text(value)
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    if cleaned:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%d/%m/%Y",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(cleaned[:26], fmt).strftime("%d/%m/%Y")
            except ValueError:
                continue
        return cleaned
    for part in (fallback_path.parent.name, fallback_path.name):
        match = re.search(r"(\d{4}-\d{2}-\d{2})", part)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                pass
    return datetime.now().strftime("%d/%m/%Y")


def load_snapshot(input_path: Path) -> dict:
    return json.loads(input_path.read_text(encoding="utf-8"))


def parse_snapshot_rows(input_path: Path) -> list[dict]:
    snapshot = load_snapshot(input_path)
    rows = snapshot.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(
            f"Unexpected Connect ETFs snapshot in {input_path}: expected listing_rows to be a list."
        )
    return rows


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    snapshot = load_snapshot(resolved_input)

    captured_at = clean_text(snapshot.get("captured_at", ""))
    raw_rows: list[dict] = parse_snapshot_rows(resolved_input)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for raw in raw_rows:
        isin = normalize_isin(raw.get("isin"))
        if not is_valid_isin(isin):
            continue
        if isin in seen:
            continue
        seen.add(isin)

        name = clean_text(raw.get("etf_name"))
        if not name:
            continue

        row_date = normalize_date(raw.get("date") or captured_at, resolved_input)
        rows.append(
            {
                "ETF Name": name,
                "Issuer": clean_text(raw.get("issuer")) or ISSUER,
                "ISIN": isin,
                "CCY": clean_text(raw.get("ccy")).upper(),
                "TER(bps)": format_ter_bps(raw.get("ter_bps")),
                "AUM(M)": format_aum_m(raw.get("aum_mn")),
                "AUM CCY": infer_aum_currency(raw),
                "Date": row_date,
            }
        )

    return rows


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    resolved_output = output_path.resolve() if output_path else build_output_path(resolved_input)

    rows = extract_rows(resolved_input)
    write_csv(resolved_output, rows)

    print("=" * 60)
    print("Connect ETFs Fields Extractor")
    print("=" * 60)
    print(f"Input file  : {resolved_input}")
    print(f"Output file : {resolved_output}")
    print(f"Rows written: {len(rows):,}")
    print("=" * 60)

    return resolved_output


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
