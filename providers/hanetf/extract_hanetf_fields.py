"""
HANetf Fields Extractor
=======================

Reads the latest raw HANetf file from:

    providers/hanetf/YYYY-MM-DD/hanetf_raw_YYYY-MM-DD.csv

Creates:

    providers/hanetf/YYYY-MM-DD/hanetf_selected_fields.csv

Output columns:
    ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), AUM CCY, Date
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

ISSUER = "HANetf"

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
ISO_CURRENCY_PREFIX_RE = re.compile(r"^([A-Z]{3})(?=[\d\s,.(+-])")
ISO_CURRENCY_TOKEN_RE = re.compile(r"\b([A-Z]{3})\b")

CCY_SYMBOL_MAP = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
}

KNOWN_CURRENCY_CODES = {
    "AED", "AUD", "BRL", "CAD", "CHF", "CNH", "CNY", "CZK", "DKK", "EUR", "GBP",
    "HKD", "HUF", "ILS", "INR", "JPY", "KRW", "KZT", "MXN", "NOK", "NZD", "PLN",
    "QAR", "RUB", "SAR", "SEK", "SGD", "TRY", "TWD", "USD", "ZAR",
}

KNOWN_ISIN_CURRENCY_OVERRIDES = {
    # Confirmed from HANetf product details: base currency and net assets are shown in CAD.
    "IE000P1G9TM6": "CAD",
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from HANetf raw scraped file."
    )

    parser.add_argument(
        "--input",
        type=Path,
        help="Path to HANetf raw CSV file. Defaults to latest hanetf_raw_*.csv under providers/hanetf.",
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
            for path in input_dir.rglob("hanetf_raw_*.csv")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        raise FileNotFoundError(
            f"No HANetf raw CSV file found under {input_dir}. "
            f"Run scrapers/hanetf_extractor.py first."
        )

    print(f"Auto-selected latest input: {candidates[0]}")
    return candidates[0]


def find_latest_download(input_dir: Path) -> Path:
    return find_latest_input(input_dir)


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "hanetf_selected_fields.csv"


# ---------------------------------------------------------------------------
# Cleaners
# ---------------------------------------------------------------------------

def clean_text(value: object | None) -> str:
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    text = str(value).strip()

    if text in {"", "-", "—", "–", "nan", "NaN", "None"}:
        return ""

    return text


def normalize_column_name(value: object | None) -> str:
    text = clean_text(value).lower()
    text = text.replace("\n", " ")
    text = text.replace("_", " ")
    text = text.replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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


def extract_ccy_from_aum(value: object | None) -> str:
    cleaned = clean_text(value)

    if not cleaned:
        return ""

    symbol_currency = CCY_SYMBOL_MAP.get(cleaned[0], "")
    if symbol_currency:
        return symbol_currency

    upper_cleaned = cleaned.upper()
    prefix_match = ISO_CURRENCY_PREFIX_RE.match(upper_cleaned)
    if prefix_match and prefix_match.group(1) in KNOWN_CURRENCY_CODES:
        return prefix_match.group(1)

    token_match = ISO_CURRENCY_TOKEN_RE.search(upper_cleaned)
    if token_match and token_match.group(1) in KNOWN_CURRENCY_CODES:
        return token_match.group(1)

    return ""


def infer_ccy_from_name(value: object | None) -> str:
    cleaned = clean_text(value).upper()
    if not cleaned:
        return ""

    for token_match in ISO_CURRENCY_TOKEN_RE.finditer(cleaned):
        if token_match.group(1) in KNOWN_CURRENCY_CODES:
            return token_match.group(1)

    return ""


def format_aum_m(value: object | None) -> str:
    """
    Converts HANetf raw AUM to millions.

    Examples:
        $123.45m   -> 123.45
        £1.2bn     -> 1200.00
        123450000  -> 123.45
        123.45     -> 123.45
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
    Converts TER to basis points.

    Examples:
        0.49%   -> 49.00
        0.49    -> 49.00
        0.0049  -> 49.00
        49      -> 49.00
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
    cleaned = clean_text(value)

    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")

    if cleaned:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(cleaned[:10], fmt).strftime("%d/%m/%Y")
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
# Column matching
# ---------------------------------------------------------------------------

def find_column(
    df: pd.DataFrame,
    aliases: list[str],
    *,
    required: bool = True,
) -> str | None:
    normalized_columns = {
        normalize_column_name(column): column
        for column in df.columns
    }

    for alias in aliases:
        alias_norm = normalize_column_name(alias)

        if alias_norm in normalized_columns:
            return normalized_columns[alias_norm]

    for norm_column, original_column in normalized_columns.items():
        for alias in aliases:
            alias_norm = normalize_column_name(alias)

            if alias_norm in norm_column:
                return original_column

    if required:
        raise ValueError(
            f"Could not find required column. Tried aliases: {aliases}. "
            f"Available columns: {list(df.columns)}"
        )

    return None


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def read_input_file(input_path: Path) -> pd.DataFrame:
    suffix = input_path.suffix.lower()

    if suffix == ".csv":
        return pd.read_csv(input_path, dtype=str, encoding="utf-8-sig")

    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(input_path, dtype=str)

    raise ValueError(f"Unsupported input file type: {input_path.suffix}")


def parse_snapshot_rows(input_path: Path) -> list[dict[str, str]]:
    return read_input_file(input_path).to_dict(orient="records")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input = input_path.resolve() if input_path else find_latest_input(INPUT_DIR)
    df = read_input_file(resolved_input)

    name_col = find_column(
        df,
        ["Name", "ETF Name", "Fund Name", "Product Name"],
    )

    isin_col = find_column(
        df,
        ["ISIN"],
    )

    issuer_col = find_column(
        df,
        ["Issuer"],
        required=False,
    )

    aum_col = find_column(
        df,
        ["AUM", "AUM(M)", "AUM Base", "AUM (Base)", "Assets Under Management", "Fund Size"],
        required=False,
    )

    aum_raw_col = find_column(
        df,
        ["AUM Raw", "AUM Source", "Net Assets of Fund", "Assets Under Management Raw"],
        required=False,
    )

    ccy_col = find_column(
        df,
        ["CCY", "Currency", "Trading Currency", "Listing Currency", "Base Currency"],
        required=False,
    )

    ter_col = find_column(
        df,
        ["TER", "TER(bps)", "Total Expense Ratio", "Ongoing Charge", "OCF"],
        required=False,
    )

    date_col = find_column(
        df,
        ["Date", "As Of", "As of Date", "Data Date"],
        required=False,
    )

    rows: list[dict[str, str]] = []

    for _, source_row in df.iterrows():
        name = clean_text(source_row.get(name_col, ""))
        isin = clean_text(source_row.get(isin_col, "")).upper()

        if not name:
            continue

        if not is_valid_isin(isin):
            continue

        issuer_raw = source_row.get(issuer_col, "") if issuer_col else ""
        aum_raw = source_row.get(aum_col, "") if aum_col else ""
        aum_raw_text = source_row.get(aum_raw_col, "") if aum_raw_col else aum_raw
        ccy_raw = source_row.get(ccy_col, "") if ccy_col else ""
        ter_raw = source_row.get(ter_col, "") if ter_col else ""
        date_raw = source_row.get(date_col, "") if date_col else ""
        ccy_override = KNOWN_ISIN_CURRENCY_OVERRIDES.get(isin, "")
        inferred_ccy = (
            clean_text(ccy_raw).upper()
            or extract_ccy_from_aum(aum_raw_text)
            or ccy_override
            or infer_ccy_from_name(name)
        )

        record = {
            "ETF Name": name,
            "Issuer": clean_text(issuer_raw) or ISSUER,
            "ISIN": isin,
            "CCY": inferred_ccy,
            "TER(bps)": format_ter_bps(ter_raw),
            "AUM(M)": format_aum_m(aum_raw),
            "AUM CCY": extract_ccy_from_aum(aum_raw_text) or ccy_override or inferred_ccy,
            "Date": normalize_date(date_raw, resolved_input),
        }

        rows.append(record)

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
    print("HANetf Fields Extractor")
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
