"""
FinEx Fields Extractor
======================

Reads the latest raw FinEx file from:

    providers/finex/YYYY-MM-DD/finex_etf_export.json

Creates:

    providers/finex/YYYY-MM-DD/finex_selected_fields.csv

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

ISSUER = "FinEx"

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
CORE_DETAIL_FIELD_LABELS = {
    "ccy": "CCY",
    "ter_bps": "TER(bps)",
    "aum_mn": "AUM(M)",
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from FinEx raw JSON file."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to finex_etf_export.json. Defaults to latest under providers/finex.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to same folder as the raw input.",
    )
    parser.add_argument(
        "--include-terminated",
        action="store_true",
        default=False,
        help="Include terminated funds in the output (excluded by default).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def find_latest_input(input_dir: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in input_dir.rglob("finex_etf_export.json")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No finex_etf_export.json found under {input_dir}. "
            "Run scrapers/finex_extractor.py first."
        )
    print(f"Auto-selected latest input: {candidates[0]}")
    return candidates[0]


def find_latest_download(input_dir: Path) -> Path:
    return find_latest_input(input_dir)


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "finex_selected_fields.csv"


# ---------------------------------------------------------------------------
# Cleaners  (same conventions as HANetf extractor)
# ---------------------------------------------------------------------------

def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00ad", "").strip()
    return "" if text in {"", "-", "--", "- ", " -", "nan", "NaN", "None"} else text


def is_valid_isin(value: object | None) -> bool:
    isin = clean_text(value).upper()
    return bool(ISIN_RE.fullmatch(isin))


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
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
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
        raise ValueError(f"Unexpected FinEx snapshot in {input_path}: expected listing_rows to be a list.")
    return rows


def extract_rows_with_summary(
    input_path: Path | None = None,
    *,
    include_terminated: bool = False,
) -> tuple[list[dict[str, str]], dict[str, int], list[str]]:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    snapshot = load_snapshot(resolved_input)

    captured_at = clean_text(snapshot.get("captured_at", ""))
    run_date = normalize_date(captured_at, resolved_input)

    raw_rows: list[dict] = parse_snapshot_rows(resolved_input)

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    exclusion_counts: dict[str, int] = {}
    inclusion_warnings: list[str] = []

    for raw in raw_rows:
        if not include_terminated and clean_text(raw.get("terminated")) == "true":
            exclusion_counts["terminated"] = exclusion_counts.get("terminated", 0) + 1
            continue

        isin = clean_text(raw.get("isin", "")).upper()
        if not is_valid_isin(isin):
            exclusion_counts["invalid_isin"] = exclusion_counts.get("invalid_isin", 0) + 1
            continue

        if isin in seen:
            exclusion_counts["duplicate_isin"] = exclusion_counts.get("duplicate_isin", 0) + 1
            continue

        name = clean_text(raw.get("etf_name", ""))
        if not name:
            exclusion_counts["missing_name"] = exclusion_counts.get("missing_name", 0) + 1
            continue

        seen.add(isin)

        row = {
            "ETF Name": name,
            "Issuer": clean_text(raw.get("issuer", "")) or ISSUER,
            "ISIN": isin,
            "CCY": clean_text(raw.get("ccy", "")).upper(),
            "TER(bps)": format_ter_bps(raw.get("ter_bps")),
            "AUM(M)": format_aum_m(raw.get("aum_mn")),
            "Date": run_date,
        }
        rows.append(row)

        detail_status = clean_text(raw.get("detail_fetch_status", ""))
        missing_labels = [
            label
            for source_field, label in CORE_DETAIL_FIELD_LABELS.items()
            if not clean_text(raw.get(source_field, ""))
        ]
        if detail_status and detail_status != "ok":
            inclusion_warnings.append(f"{isin}: detail_status={detail_status}")
        elif missing_labels:
            inclusion_warnings.append(f"{isin}: missing core fields={', '.join(missing_labels)}")

    return rows, exclusion_counts, inclusion_warnings


def extract_rows(
    input_path: Path | None = None,
    *,
    include_terminated: bool = False,
) -> list[dict[str, str]]:
    rows, _, _ = extract_rows_with_summary(
        input_path,
        include_terminated=include_terminated,
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
    *,
    include_terminated: bool = False,
) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    resolved_output = (
        output_path.resolve() if output_path else build_output_path(resolved_input)
    )

    rows, exclusion_counts, inclusion_warnings = extract_rows_with_summary(
        resolved_input,
        include_terminated=include_terminated,
    )
    write_csv(resolved_output, rows)

    print("=" * 60)
    print("FinEx Fields Extractor")
    print("=" * 60)
    print(f"Input file  : {resolved_input}")
    print(f"Output file : {resolved_output}")
    print(f"Rows written: {len(rows):,}")
    if exclusion_counts:
        print(
            "Excluded    : "
            + ", ".join(f"{reason}={count}" for reason, count in sorted(exclusion_counts.items()))
        )
    if inclusion_warnings:
        print(f"Warnings    : {len(inclusion_warnings):,}")
        for warning in inclusion_warnings[:20]:
            print(f"  - {warning}")
    print("=" * 60)

    return resolved_output


def main() -> None:
    args = parse_args()
    process_file(
        args.input,
        args.output,
        include_terminated=args.include_terminated,
    )


if __name__ == "__main__":
    main()
