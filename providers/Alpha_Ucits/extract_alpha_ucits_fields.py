"""Extract selected Alpha UCITS ETF fields from the latest scraper CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
ISSUER = "Alpha UCITS"
RAW_FILENAME = "alpha_ucits_etf_export.json"
FALLBACK_FILENAME = "alpha_ucits_selected_fields.csv"

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

SPACE_PATTERN = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), AUM CCY, and Date from Alpha UCITS ETF snapshot."
    )
    parser.add_argument("--input", type=Path, help="Alpha UCITS raw snapshot path. Defaults to latest.")
    parser.add_argument("--output", type=Path, help="Output CSV path. Defaults to the same folder as the input.")
    return parser.parse_args()


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None", "null"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def normalize_ccy(value: object | None) -> str:
    cleaned = clean_text(value).upper()
    return cleaned if re.fullmatch(r"[A-Z]{3}", cleaned) else ""


def normalize_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return cleaned


def find_latest_download(input_dir: Path) -> Path:
    for filename in (RAW_FILENAME, FALLBACK_FILENAME):
        candidates = sorted(
            (path for path in input_dir.rglob(filename) if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    raise FileNotFoundError(f"No {RAW_FILENAME} or {FALLBACK_FILENAME} files found in {input_dir}")


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / FALLBACK_FILENAME


def parse_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("listing_rows", [])
        return rows if isinstance(rows, list) else []
    return parse_csv_rows(path)


def transform_row(source_row: dict[str, object]) -> dict[str, str] | None:
    etf_name = clean_text(source_row.get("ETF Name") or source_row.get("fund_name"))
    if not etf_name:
        return None
    return {
        "ETF Name": etf_name,
        "Issuer": clean_text(source_row.get("Issuer")) or ISSUER,
        "ISIN": normalize_isin(source_row.get("ISIN")),
        "CCY": normalize_ccy(source_row.get("CCY")),
        "TER(bps)": clean_text(source_row.get("TER(bps)")),
        "AUM(M)": clean_text(source_row.get("AUM(M)")),
        "AUM CCY": normalize_ccy(source_row.get("AUM CCY"))
        or ("EUR" if clean_text(source_row.get("Fund total net assets (EUR)")) else ""),
        "Date": normalize_date(source_row.get("Date") or source_row.get("launch_date")),
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(column, "") for column in OUTPUT_COLUMNS)
        deduped.setdefault(key, row)
    return list(deduped.values())


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    source_rows = parse_snapshot_rows(resolved_input)
    return dedupe_rows([row for row in (transform_row(source_row) for source_row in source_rows) if row is not None])


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return output_path
    except PermissionError:
        fallback_path = output_path.with_name(f"{output_path.stem}_latest{output_path.suffix}")
        with fallback_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Primary output was locked; wrote fallback file instead: {fallback_path}")
        return fallback_path


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output = output_path.resolve() if output_path else build_output_path(resolved_input)
    rows = extract_rows(resolved_input)
    written_output = write_csv(resolved_output, rows)
    print(f"Source file : {resolved_input}")
    print(f"Rows written: {len(rows):,}")
    print(f"Output file : {written_output}")
    return written_output


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
