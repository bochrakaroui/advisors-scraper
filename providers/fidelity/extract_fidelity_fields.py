"""Extract the selected ETF fields from the latest downloaded Fidelity snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
ISSUER = "Fidelity International"
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
    parser = argparse.ArgumentParser(
        description="Extract the selected ETF fields from a downloaded Fidelity .json snapshot."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Downloaded Fidelity .json snapshot. Defaults to the latest fidelity_etf_export.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Processed CSV path. Defaults to a date folder inside ./fidelity.",
    )
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
        (
            path
            for path in input_dir.rglob("fidelity_etf_export.json")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No fidelity_etf_export.json files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "fidelity_selected_fields.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = (
        str(value)
        .replace("\u00ad", "")
        .replace("\u00a0", " ")
        .strip()
    )
    cleaned = re.sub(r"\s+", " ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


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


def normalize_date(raw_value: object | None, fallback: str = "") -> str:
    cleaned = clean_text(raw_value)
    if not cleaned:
        return fallback

    for fmt in ("%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y 00:00:00", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.strftime("%d/%m/%Y")
        except ValueError:
            continue

    return cleaned


def parse_captured_at(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    normalized = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return normalize_date(cleaned)

    return parsed.strftime("%d/%m/%Y")


def parse_snapshot(path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    captured_at = parse_captured_at(payload.get("captured_at"))
    rows = payload.get("listing_rows", [])

    if not isinstance(rows, list):
        raise ValueError(f"Unexpected Fidelity snapshot in {path}: expected listing_rows to be a list.")

    return captured_at, rows


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    _, rows = parse_snapshot(path)
    return rows


def transform_row(source_row: dict[str, object], scrape_timestamp: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer") or ISSUER),
        "ISIN": clean_text(source_row.get("isin")).upper(),
        "CCY": clean_text(source_row.get("ccy")).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_mn")),
        "Date": scrape_timestamp or normalize_date(source_row.get("date")),
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


def write_csv_with_fallback(output_path: Path, rows: list[dict[str, str]]) -> Path:
    try:
        write_csv(output_path, rows)
        return output_path
    except PermissionError:
        fallback_path = output_path.with_name(f"{output_path.stem}_updated{output_path.suffix}")
        write_csv(fallback_path, rows)
        print(f"Output file is locked, wrote updated data to: {fallback_path}")
        return fallback_path


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    captured_at, source_rows = parse_snapshot(resolved_input_path)
    scrape_timestamp = captured_at or extract_file_date(resolved_input_path)

    output_rows = [transform_row(row, scrape_timestamp) for row in source_rows]
    return dedupe_rows(output_rows)


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    resolved_output_path = write_csv_with_fallback(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing ISIN: {sum(1 for row in output_rows if not clean_text(row.get('ISIN'))):,}")
    print(f"Missing CCY: {sum(1 for row in output_rows if not clean_text(row.get('CCY'))):,}")
    print(f"Missing TER(bps): {sum(1 for row in output_rows if not clean_text(row.get('TER(bps)'))):,}")
    print(f"Missing AUM(M): {sum(1 for row in output_rows if not clean_text(row.get('AUM(M)'))):,}")
    print(f"Output file : {resolved_output_path}")

    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
