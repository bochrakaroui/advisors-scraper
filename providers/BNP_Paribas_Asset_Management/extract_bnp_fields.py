"""Extract selected BNP Paribas ETF fields from the latest snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
LEGACY_INPUT_DIR = BASE_DIR.parents[1] / "providers" / "BNP Paribas Asset Management"
OUTPUT_COLUMNS = [
    "ISIN",
    "ETF Name",
    "Issuer",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract ISIN, ETF name, issuer, CCY, TER and AUM(M) from a BNP Paribas snapshot."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="BNP Paribas snapshot JSON path. Defaults to the latest bnpparibas_etf_export.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output path. Defaults to the same folder as the source bnpparibas_etf_export.json file.",
    )
    return parser.parse_args()


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


def snapshot_has_meaningful_rows(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    rows = payload.get("listing_rows", [])
    if not isinstance(rows, list):
        return False

    for row in rows:
        if not isinstance(row, dict):
            continue
        if clean_text(row.get("fetch_status")) == "ok":
            return True
        if any(
            clean_text(row.get(field))
            for field in ("etf_name", "ccy", "ter_bps", "aum_mn")
        ):
            return True
    return False


def find_latest_download(input_dir: Path) -> Path:
    search_dirs = [input_dir]
    if input_dir.resolve() == INPUT_DIR.resolve() and LEGACY_INPUT_DIR.resolve() not in {
        path.resolve() for path in search_dirs
    }:
        search_dirs.append(LEGACY_INPUT_DIR)

    candidates = sorted(
        (
            path
            for search_dir in search_dirs
            for path in search_dir.rglob("bnpparibas_etf_export.json")
            if search_dir.exists() and path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if snapshot_has_meaningful_rows(candidate):
            return candidate

    searched_locations = ", ".join(str(path) for path in search_dirs)
    if candidates:
        raise FileNotFoundError(
            "No valid bnpparibas_etf_export.json snapshot with meaningful rows was found in "
            f"{searched_locations}"
        )
    raise FileNotFoundError(f"No bnpparibas_etf_export.json files found in {searched_locations}")


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "bnpparibas_selected_fields.csv"


def extract_file_date(input_path: Path) -> str:
    parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if parent_date_match:
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
    return ""


def parse_captured_at(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.strftime("%d/%m/%Y")


def parse_snapshot(path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    captured_at = parse_captured_at(payload.get("captured_at"))
    rows = payload.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected BNP snapshot in {path}: expected listing_rows to be a list.")
    return captured_at, rows


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    _, rows = parse_snapshot(path)
    return rows


def transform_row(source_row: dict[str, object], scrape_date: str) -> dict[str, str]:
    return {
        "ISIN": clean_text(source_row.get("isin")).upper(),
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer")),
        "CCY": clean_text(source_row.get("ccy")).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_mn")),
        "Date": scrape_date,
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
    return dedupe_rows([transform_row(row, scrape_date) for row in source_rows])


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing ISIN : {sum(1 for row in output_rows if not clean_text(row.get('ISIN'))):,}")
    print(f"Missing CCY  : {sum(1 for row in output_rows if not clean_text(row.get('CCY'))):,}")
    print(f"Missing TER  : {sum(1 for row in output_rows if not clean_text(row.get('TER(bps)'))):,}")
    print(f"Missing AUM  : {sum(1 for row in output_rows if not clean_text(row.get('AUM(M)'))):,}")
    print(f"Output file : {resolved_output_path}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
