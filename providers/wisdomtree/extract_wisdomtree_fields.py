"""Extract the selected ETF fields from the latest downloaded WisdomTree snapshot."""

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
ISSUER = "WisdomTree"
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
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded WisdomTree .json snapshot.")
    parser.add_argument("--input", type=Path, help="Downloaded WisdomTree .json snapshot. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a date folder inside ./wisdomtree.")
    return parser.parse_args()


def build_run_output_dir(base_dir: Path) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    run_date = datetime.now().strftime("%Y-%m-%d")
    output_dir = base_dir / run_date
    suffix = 1
    while output_dir.exists():
        output_dir = base_dir / f"{run_date} ({suffix})"
        suffix += 1
    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name
    return output_dir


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted((path for path in input_dir.rglob("*.json") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .json files found in {input_dir}")
    return candidates[0]


def build_output_path(output_dir: Path) -> Path:
    return build_run_output_dir(output_dir) / "wisdomtree_selected_fields.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def extract_file_date(input_path: Path) -> str:
    match = re.search(r"(\d{8}_\d{6})", input_path.stem)
    if not match:
        parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
        if not parent_date_match:
            return ""
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y 00:00:00")

    timestamp = match.group(1)
    try:
        return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return timestamp


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected WisdomTree snapshot in {path}: expected a list of rows.")
    return rows


def transform_row(source_row: dict[str, str], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": ISSUER,
        "ISIN": clean_text(source_row.get("isin")).upper(),
        "CCY": clean_text(source_row.get("ccy")).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_numeric")),
        "Date": file_date,
    }


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    rows = parse_snapshot_rows(resolved_input_path)
    file_date = extract_file_date(resolved_input_path)
    return [transform_row(row, file_date) for row in rows]


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(OUTPUT_DIR)

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
