from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

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

# --- TUNE ME: Waystone hosts ETFs from many different issuers rather than
# being an issuer itself, and the scraper's "issuer" field currently comes
# through empty. Map known leading words/phrases in the fund name to the
# real issuer. Anything not listed falls back to the fund name's first word,
# title-cased, so unseen issuers still get *something* rather than a blank.
ISSUER_ALIASES: dict[str, str] = {
    "wahed": "Wahed",
    "calamos": "Calamos",
    "muzinich": "Muzinich",
    "eldridge": "Eldridge",
    "bellevue": "Bellevue",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the selected ETF fields from the latest Waystone scraper JSON."
    )
    parser.add_argument("--input", type=Path, help="Waystone scraper JSON file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a date folder inside ./Waystone.")
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
        (path for path in input_dir.rglob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No .json files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    # Keep the output alongside the source snapshot (same dated run folder)
    # rather than always minting a new date, so re-runs against an older
    # snapshot don't get filed under today's date.
    return input_path.parent / "waystone_selected_fields.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "N/A"} else cleaned


def format_decimal(value: object | None, places: int = 2) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    cleaned = cleaned.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    try:
        decimal_value = Decimal(cleaned)
    except InvalidOperation:
        return ""
    quantized = decimal_value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def guess_issuer(fund_name: str) -> str:
    lowered = fund_name.casefold()
    for prefix, issuer in ISSUER_ALIASES.items():
        if lowered.startswith(prefix):
            return issuer
    first_word = fund_name.split(" ", 1)[0] if fund_name else ""
    return first_word.title()


def format_date(value: str | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    # Source is already DD/MM/YYYY (e.g. "30/06/2026"); pass through as-is,
    # but fall back to re-formatting if some other shape shows up.
    try:
        datetime.strptime(cleaned, "%d/%m/%Y")
        return cleaned
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return cleaned


def load_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", payload.get("listing_rows", []))
    return rows


def transform_row(source_row: dict) -> dict[str, str] | None:
    if source_row.get("extraction_method") == "failed":
        return None

    fund_name = clean_text(source_row.get("etf_name"))
    isin = clean_text(source_row.get("isin")).upper()
    if not fund_name or not isin:
        return None

    issuer = clean_text(source_row.get("provider")) or "Waystone"

    return {
        "ETF Name": fund_name,
        "Issuer": issuer,
        "ISIN": isin,
        "CCY": clean_text(source_row.get("ccy") or source_row.get("aum_currency")).upper(),
        "TER(bps)": format_decimal(source_row.get("ter_bps")),
        "AUM(M)": format_decimal(source_row.get("aum_m") or source_row.get("aum_numeric")),
        "AUM CCY": clean_text(source_row.get("aum_currency")).upper(),
        "Date": format_date(source_row.get("as_of_date") or source_row.get("date")),
    }


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    source_rows = load_rows(resolved_input_path)
    output_rows = [transform_row(row) for row in source_rows]
    return [row for row in output_rows if row is not None]


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
