"""Extract selected ETF fields from the latest M&G snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]

SPACE_PATTERN = re.compile(r"\s+")
EXCLUDED_ISINS = {
    "LU0249326488",
    "LU0259322260",
    "LU1750178011",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), and Date "
            "from an M&G ETF snapshot."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="M&G snapshot JSON path. Defaults to the latest mg_etf_export.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output path. Defaults to the same folder as the source mg_etf_export.json file.",
    )
    return parser.parse_args()


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (path for path in input_dir.rglob("mg_etf_export.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No mg_etf_export.json files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "mg_selected_fields.csv"


def extract_file_date(input_path: Path) -> str:
    parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if parent_date_match:
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
    return ""


def parse_iso_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def parse_snapshot(path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected M&G snapshot in {path}: expected listing_rows to be a list.")
    return parse_iso_date(payload.get("captured_at")), rows


def transform_row(source_row: dict[str, object], scrape_date: str) -> dict[str, str]:
    row_date = (
        parse_iso_date(source_row.get("date"))
        or parse_iso_date(source_row.get("nav_date"))
        or scrape_date
    )
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer")),
        "ISIN": normalize_isin(source_row.get("isin")),
        "CCY": clean_text(source_row.get("ccy")).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_mn")),
        "Date": row_date,
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(column, "") for column in OUTPUT_COLUMNS)
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
    captured_at, source_rows = parse_snapshot(resolved_input_path)
    scrape_date = captured_at or extract_file_date(resolved_input_path)
    filtered_rows = [
        row
        for row in source_rows
        if normalize_isin(row.get("isin")) not in EXCLUDED_ISINS
    ]
    return dedupe_rows([transform_row(row, scrape_date) for row in filtered_rows])


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing TER : {sum(1 for row in output_rows if not clean_text(row.get('TER(bps)'))):,}")
    print(f"Missing AUM : {sum(1 for row in output_rows if not clean_text(row.get('AUM(M)'))):,}")
    print(f"Output file : {resolved_output_path}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
