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
    "AUM CCY",
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
    else:
        run_date = datetime.now().strftime("%Y-%m-%d")
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir

def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted((path for path in input_dir.rglob("*.json") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No .json files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "wisdomtree_selected_fields.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def normalize_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return cleaned


def extract_file_date(input_path: Path) -> str:
    match = re.search(r"(\d{8}_\d{6})", input_path.stem)
    if not match:
        parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
        if not parent_date_match:
            return ""
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")

    timestamp = match.group(1)
    try:
        return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%d/%m/%Y")
    except ValueError:
        return timestamp


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected WisdomTree snapshot in {path}: expected a list of rows.")
    return rows


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def is_official_wisdomtree_url(value: object | None) -> bool:
    cleaned = clean_text(value).lower()
    return cleaned.startswith("https://www.wisdomtree.eu/en-gb/etfs/")


def load_historical_selected_rows(target_isins: set[str], current_input_path: Path) -> list[dict[str, str]]:
    if not target_isins:
        return []

    current_raw_path = current_input_path.resolve()
    historical_rows_by_isin: dict[str, list[dict[str, str]]] = {}

    for raw_path in sorted(
        INPUT_DIR.rglob("wisdomtree_etf_export.json"),
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    ):
        if raw_path.resolve() == current_raw_path:
            continue
        try:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        rows = payload.get("rows", [])
        if not isinstance(rows, list):
            continue

        eligible_isins = {
            isin
            for isin in target_isins
            if isin not in historical_rows_by_isin
            and any(
                normalize_isin(row.get("isin")) == isin
                and is_official_wisdomtree_url(row.get("product_url") or row.get("source_url"))
                for row in rows
                if isinstance(row, dict)
            )
        }
        if not eligible_isins:
            continue

        selected_path = raw_path.parent / "wisdomtree_selected_fields.csv"
        if not selected_path.exists():
            continue

        try:
            with selected_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                grouped_rows: dict[str, list[dict[str, str]]] = {}
                for row in reader:
                    isin = normalize_isin(row.get("ISIN"))
                    if isin not in eligible_isins:
                        continue
                    grouped_rows.setdefault(isin, []).append(
                        {key: clean_text(value) for key, value in row.items()}
                    )
        except Exception:
            continue

        for isin, grouped in grouped_rows.items():
            if isin not in historical_rows_by_isin and grouped:
                historical_rows_by_isin[isin] = grouped

        if target_isins.issubset(historical_rows_by_isin):
            break

    ordered_rows: list[dict[str, str]] = []
    for isin in sorted(historical_rows_by_isin):
        ordered_rows.extend(historical_rows_by_isin[isin])
    return ordered_rows


def transform_row(source_row: dict[str, str], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": ISSUER,
        "ISIN": clean_text(source_row.get("isin")).upper(),
        "CCY": clean_text(source_row.get("ccy") or source_row.get("base_currency")).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_numeric") or source_row.get("aum_m")),
        "AUM CCY": clean_text(source_row.get("aum_currency")).upper(),
        "Date": normalize_date(source_row.get("as_of_date")),
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(clean_text(row.get(column)) for column in OUTPUT_COLUMNS)
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
    rows = parse_snapshot_rows(resolved_input_path)
    file_date = extract_file_date(resolved_input_path)

    rows_by_isin: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        isin = normalize_isin(row.get("isin"))
        if isin:
            rows_by_isin.setdefault(isin, []).append(row)

    replacement_isins = {
        isin
        for isin, grouped_rows in rows_by_isin.items()
        if grouped_rows
        and not any(
            is_official_wisdomtree_url(row.get("product_url") or row.get("source_url"))
            for row in grouped_rows
        )
        and any("justetf.com" in clean_text(row.get("product_url") or row.get("source_url")).lower() for row in grouped_rows)
    }

    output_rows = [
        transform_row(row, file_date)
        for row in rows
        if normalize_isin(row.get("isin")) not in replacement_isins
    ]
    output_rows.extend(load_historical_selected_rows(replacement_isins, resolved_input_path))
    return dedupe_rows(output_rows)


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
