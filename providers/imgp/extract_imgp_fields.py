"""
iM Global Partner Fields Extractor
====================================

Reads the latest raw iMGP file from:

    providers/imgp/YYYY-MM-DD/imgp_etf_export.json

Creates:

    providers/imgp/YYYY-MM-DD/imgp_selected_fields.csv

Output columns:
    ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), Date
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
ISSUER = "iM Global Partner"

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from iMGP raw JSON file."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to imgp_etf_export.json. Defaults to latest under providers/imgp.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to same folder as the raw input.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def find_latest_input(input_dir: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in input_dir.rglob("imgp_etf_export.json")
            if path.is_file()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No imgp_etf_export.json found under {input_dir}. "
            "Run scrapers/imgp_extractor.py first."
        )
    print(f"Auto-selected latest input: {candidates[0]}")
    return candidates[0]


def find_latest_download(input_dir: Path) -> Path:
    return find_latest_input(input_dir)


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "imgp_selected_fields.csv"


# ---------------------------------------------------------------------------
# Cleaners  (identical conventions to HANetf / FinEx extractors)
# ---------------------------------------------------------------------------

def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00ad", "").strip()
    return "" if text in {"", "-", "--", "- ", " -", "nan", "NaN", "None"} else text


def is_valid_isin(value: object | None) -> bool:
    return bool(ISIN_RE.fullmatch(clean_text(value).upper()))


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
    """
    Already stored as millions by the scraper (e.g. '538.40').
    Just validates and normalises the decimal representation.
    """
    raw = clean_text(value)
    if not raw:
        return ""
    d = decimal_from_value(raw)
    if d is None:
        return ""
    raw_lower = raw.lower()
    if "bn" in raw_lower or "billion" in raw_lower:
        return format_decimal(d * Decimal("1000"), places=2)
    if "m" in raw_lower or "million" in raw_lower:
        return format_decimal(d, places=2)
    if d >= Decimal("1000000"):
        return format_decimal(d / Decimal("1000000"), places=2)
    return format_decimal(d, places=2)


def format_ter_bps(value: object | None) -> str:
    raw = clean_text(value)
    if not raw:
        return ""
    d = decimal_from_value(raw)
    if d is None:
        return ""
    if "%" in raw:
        bps = d * Decimal("100")
    elif d < Decimal("0.05"):
        bps = d * Decimal("10000")
    elif d < Decimal("5"):
        bps = d * Decimal("100")
    else:
        bps = d
    return format_decimal(bps, places=2)


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
    for part in [fallback_path.parent.name, fallback_path.name]:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", part)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                pass
    return datetime.now().strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def load_snapshot(input_path: Path) -> dict:
    return json.loads(input_path.read_text(encoding="utf-8"))


def parse_snapshot_rows(input_path: Path) -> list[dict]:
    snapshot = load_snapshot(input_path)
    rows = snapshot.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected iMGP snapshot in {input_path}: expected listing_rows to be a list.")
    return rows


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    snapshot = load_snapshot(resolved_input)

    captured_at = clean_text(snapshot.get("captured_at", ""))
    run_date = normalize_date(captured_at, resolved_input)

    raw_rows: list[dict] = parse_snapshot_rows(resolved_input)

    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for raw in raw_rows:
        isin = clean_text(raw.get("isin", "")).upper()
        if not is_valid_isin(isin):
            continue

        # Deduplicate on ISIN
        if isin in seen:
            continue
        seen.add(isin)

        name = clean_text(raw.get("etf_name", ""))
        if not name:
            continue

        rows.append(
            {
                "ETF Name": name,
                "Issuer": clean_text(raw.get("issuer", "")) or ISSUER,
                "ISIN": isin,
                "CCY": clean_text(raw.get("ccy", "")).upper(),
                "TER(bps)": format_ter_bps(raw.get("ter_bps")),
                "AUM(M)": format_aum_m(raw.get("aum_mn")),
                "Date": run_date,
            }
        )

    return rows


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    resolved_output = (
        output_path.resolve() if output_path else build_output_path(resolved_input)
    )

    rows = extract_rows(resolved_input)
    write_csv(resolved_output, rows)

    print("=" * 60)
    print("iM Global Partner Fields Extractor")
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
