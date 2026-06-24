"""
Palmer Square Fields Extractor
==============================

Reads the latest raw Palmer Square file from:

    providers/palmersquare/YYYY-MM-DD/palmersquare_raw_YYYY-MM-DD.csv

Creates:

    providers/palmersquare/YYYY-MM-DD/palmersquare_selected_fields.csv

Output columns:
    ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), Date
"""

from __future__ import annotations

import argparse
import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR

ISSUER = "Palmer Square"

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

CCY_SYMBOL_MAP = {
    "€": "EUR",
    "$": "USD",
    "£": "GBP",
}

CCY_TEXT_MAP = {
    "EUR": "EUR",
    "EURO": "EUR",
    "USD": "USD",
    "US DOLLAR": "USD",
    "GBP": "GBP",
    "GBX": "GBp",
    "GBp": "GBp",
}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from Palmer Square raw scraped CSV."
    )

    parser.add_argument(
        "--input",
        type=Path,
        help="Path to palmersquare_raw_*.csv. Defaults to latest raw file under providers/palmersquare.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to same folder as raw input.",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# PATH HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def find_latest_input(input_dir: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in input_dir.rglob("palmersquare_raw_*.csv")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        raise FileNotFoundError(
            f"No Palmer Square raw CSV file found under {input_dir}. "
            f"Run scrapers/palmersquare_extractor.py first."
        )

    return candidates[0]


def find_latest_download(input_dir: Path) -> Path:
    return find_latest_input(input_dir)


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "palmersquare_selected_fields.csv"


# ─────────────────────────────────────────────────────────────────────────────
# CLEANERS / FORMATTERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(value: object | None) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)

    if text in {"", "-", "—", "–", "None", "nan", "NaN"}:
        return ""

    return text


def normalize_header(value: object | None) -> str:
    text = clean_text(value).lower()
    text = text.replace("\n", " ")
    text = text.replace("_", " ")
    text = text.replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_valid_isin(value: object | None) -> bool:
    isin = clean_text(value).upper()
    return bool(ISIN_RE.fullmatch(isin))


def extract_ccy(value: object | None) -> str:
    text = clean_text(value)

    if not text:
        return ""

    first_char = text[0]

    if first_char in CCY_SYMBOL_MAP:
        return CCY_SYMBOL_MAP[first_char]

    upper_text = text.upper()

    for key, ccy in CCY_TEXT_MAP.items():
        if key.upper() in upper_text:
            return ccy

    return ""


def decimal_from_value(value: object | None) -> Decimal | None:
    """
    Extracts a Decimal from values like:
        €52,680,265
        52.68m
        0.25%
        52,680,265
    """

    raw = clean_text(value)

    if not raw:
        return None

    cleaned = raw.replace("%", "").strip()
    cleaned = re.sub(r"[^\d.,-]", "", cleaned)

    if not cleaned:
        return None

    # Case: 52,680,265 => thousands commas
    if "," in cleaned and "." not in cleaned:
        parts = cleaned.split(",")

        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    # Case: 52,680,265.12 => thousands commas + decimal dot
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantizer = Decimal("1." + "0" * places)
    return format(value.quantize(quantizer, rounding=ROUND_HALF_UP), f".{places}f")


def format_aum_m(value: object | None) -> str:
    """
    Converts AUM to millions.

    Examples:
        €52,680,265  -> 52.68
        €52.68m      -> 52.68
        €1.2bn       -> 1200.00
        52680265     -> 52.68
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
    """
    Converts Ongoing Charges / TER to basis points.

    Examples:
        0.25%  -> 25.00
        0.25   -> 25.00
        0.0025 -> 25.00
        25     -> 25.00
    """

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
    text = clean_text(value)

    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")

    if text:
        text = text.replace("AS OF", "").replace("As of", "").replace("as of", "").strip()

        for fmt in (
            "%d/%m/%Y",
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%d-%m-%Y",
            "%b %d, %Y",
            "%B %d, %Y",
        ):
            try:
                return datetime.strptime(text[:20], fmt).strftime("%d/%m/%Y")
            except ValueError:
                continue

        return text

    for part in [fallback_path.parent.name, fallback_path.name]:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", part)

        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                pass

    return datetime.now().strftime("%d/%m/%Y")


# ─────────────────────────────────────────────────────────────────────────────
# CSV READER / COLUMN MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def read_csv(input_path: Path) -> list[dict[str, str]]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def parse_snapshot_rows(input_path: Path) -> list[dict[str, str]]:
    return read_csv(input_path)


def find_column(
    headers: list[str],
    aliases: list[str],
    *,
    required: bool = True,
) -> str | None:
    normalized = {
        normalize_header(header): header
        for header in headers
    }

    for alias in aliases:
        alias_norm = normalize_header(alias)

        if alias_norm in normalized:
            return normalized[alias_norm]

    for norm_header, original_header in normalized.items():
        for alias in aliases:
            alias_norm = normalize_header(alias)

            if alias_norm in norm_header:
                return original_header

    if required:
        raise ValueError(
            f"Could not find required column. Tried aliases: {aliases}. "
            f"Available columns: {headers}"
        )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    source_rows = read_csv(resolved_input)

    if not source_rows:
        return []

    headers = list(source_rows[0].keys())

    name_col = find_column(
        headers,
        ["ETF Name", "Name", "Fund Name", "Product Name"],
    )

    issuer_col = find_column(
        headers,
        ["Issuer"],
        required=False,
    )

    isin_col = find_column(
        headers,
        ["ISIN", "ISIN Code"],
    )

    aum_col = find_column(
        headers,
        ["AUM", "Total Net Assets", "Net Assets", "Assets Under Management"],
        required=False,
    )

    ter_col = find_column(
        headers,
        ["Ongoing Charges", "Ongoing Charge", "TER", "OCF", "Total Expense Ratio"],
        required=False,
    )

    date_col = find_column(
        headers,
        ["Date", "As Of", "As of Date", "AUM Date"],
        required=False,
    )

    rows: list[dict[str, str]] = []

    for source_row in source_rows:
        name = clean_text(source_row.get(name_col, ""))
        isin = clean_text(source_row.get(isin_col, "")).upper()

        if not name:
            continue

        if not is_valid_isin(isin):
            continue

        issuer_raw = source_row.get(issuer_col, "") if issuer_col else ""
        aum_raw = source_row.get(aum_col, "") if aum_col else ""
        ter_raw = source_row.get(ter_col, "") if ter_col else ""
        date_raw = source_row.get(date_col, "") if date_col else ""

        record = {
            "ETF Name": name,
            "Issuer": clean_text(issuer_raw) or ISSUER,
            "ISIN": isin,
            "CCY": extract_ccy(aum_raw),
            "TER(bps)": format_ter_bps(ter_raw),
            "AUM(M)": format_aum_m(aum_raw),
            "Date": normalize_date(date_raw, resolved_input),
        }

        rows.append(record)

    # Deduplicate exact final rows
    unique_rows: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()

    for row in rows:
        key = tuple(row[column] for column in OUTPUT_COLUMNS)

        if key in seen:
            continue

        seen.add(key)
        unique_rows.append(row)

    return unique_rows


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
        output_path.resolve()
        if output_path
        else build_output_path(resolved_input)
    )

    rows = extract_rows(resolved_input)
    write_csv(resolved_output, rows)

    print("=" * 60)
    print("Palmer Square Fields Extractor")
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
