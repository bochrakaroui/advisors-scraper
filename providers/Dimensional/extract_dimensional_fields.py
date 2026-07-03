"""Extract the selected ETF fields from the latest downloaded Dimensional snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
ISSUER = "Dimensional"
EXCLUDED_ISINS = {"BG9000011163"}

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from a Dimensional .json snapshot."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Downloaded Dimensional .json snapshot. Defaults to the latest dimensional_export.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to dimensional_selected_fields.csv next to the input JSON.",
    )
    return parser.parse_args()


def clean_text(value: object | None) -> str:
    if value is None:
        return ""

    cleaned = (
        str(value)
        .replace("\u00ad", "")
        .replace("\u00a0", " ")
        .replace("Â", "")
        .strip()
    )
    cleaned = re.sub(r"\s+", " ", cleaned)

    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in input_dir.rglob("dimensional_export.json")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        raise FileNotFoundError(f"No dimensional_export.json files found in {input_dir}")

    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "dimensional_selected_fields.csv"


def extract_file_date(input_path: Path) -> str:
    parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if parent_date_match:
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")

    timestamp_match = re.search(r"(\d{8}_\d{6})", input_path.stem)
    if timestamp_match:
        timestamp = timestamp_match.group(1)
        try:
            return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%d/%m/%Y")
        except ValueError:
            return timestamp

    return ""


def normalize_date(raw_value: str, fallback: str = "") -> str:
    cleaned = clean_text(raw_value)
    if not cleaned:
        return fallback

    # Already ISO (YYYY-MM-DD) as produced by the Dimensional scraper.
    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{day}/{month}/{year}"

    if re.fullmatch(r"\d{2}/\d{2}/\d{4}(?: 00:00:00)?", cleaned):
        return cleaned[:10]

    match = re.search(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b", cleaned)
    if match:
        cleaned = match.group(0)

    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    return cleaned or fallback


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])

    if not isinstance(rows, list):
        raise ValueError(f"Unexpected Dimensional snapshot in {path}: expected listing_rows to be a list.")

    return rows


def transform_row(source_row: dict[str, object], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer") or ISSUER),
        "ISIN": clean_text(source_row.get("isin")).upper(),
        "CCY": clean_text(source_row.get("ccy")).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_mn")),
        "AUM CCY": clean_text(source_row.get("aum_ccy")).upper(),
        "Date": normalize_date(
            clean_text(source_row.get("date") or source_row.get("nav_date")),
            fallback=file_date,
        ),
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}

    for row in rows:
        key = tuple(clean_text(row.get(column)).upper() for column in OUTPUT_COLUMNS)
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
    file_date = extract_file_date(resolved_input_path)

    filtered_rows = [
        row
        for row in source_rows
        if clean_text(row.get("isin")).upper() not in EXCLUDED_ISINS
    ]
    output_rows = [transform_row(row, file_date) for row in filtered_rows]
    return dedupe_rows(output_rows)


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing ETF Name: {sum(1 for row in output_rows if not clean_text(row.get('ETF Name'))):,}")
    print(f"Missing ISIN: {sum(1 for row in output_rows if not clean_text(row.get('ISIN'))):,}")
    print(f"Missing CCY: {sum(1 for row in output_rows if not clean_text(row.get('CCY'))):,}")
    print(f"Missing TER(bps): {sum(1 for row in output_rows if not clean_text(row.get('TER(bps)'))):,}")
    print(f"Missing AUM(M): {sum(1 for row in output_rows if not clean_text(row.get('AUM(M)'))):,}")
    print(f"Missing AUM CCY: {sum(1 for row in output_rows if not clean_text(row.get('AUM CCY'))):,}")
    print(f"Missing Date: {sum(1 for row in output_rows if not clean_text(row.get('Date'))):,}")
    print(f"Output file : {resolved_output_path}")

    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
