"""Extract selected Pacer ETFs fields from the latest scraper snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
ISSUER = "Pacer ETFs"
SOURCE_FILENAME = "pacer_etfs_export.json"

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
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), and Date from Pacer ETF snapshot JSON."
    )
    parser.add_argument("--input", type=Path, help="Pacer snapshot JSON path. Defaults to the latest export.")
    parser.add_argument("--output", type=Path, help="Output CSV path. Defaults to the same folder as the input.")
    return parser.parse_args()


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None", "null"} else cleaned


def normalize_isin(value: object | None) -> str:
    cleaned = clean_text(value).upper().replace(" ", "")
    return cleaned if ISIN_RE.fullmatch(cleaned) else ""


def normalize_ccy(value: object | None) -> str:
    cleaned = clean_text(value).upper()
    return cleaned if re.fullmatch(r"[A-Z]{3}", cleaned) else ""


def normalize_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (path for path in input_dir.rglob(SOURCE_FILENAME) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No {SOURCE_FILENAME} files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "pacer_etfs_selected_fields.csv"


def parse_snapshot(path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected Pacer snapshot in {path}: expected listing_rows to be a list.")
    return normalize_date(payload.get("captured_at")), rows


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def extract_file_date(input_path: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if not match:
        return ""
    return datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")


def transform_row(source_row: dict[str, object], fallback_date: str) -> dict[str, str] | None:
    isin = normalize_isin(source_row.get("isin"))
    if not isin:
        return None

    etf_name = clean_text(source_row.get("etf_name") or source_row.get("listing_name"))
    if not etf_name:
        return None

    row_date = (
        normalize_date(source_row.get("rate_date"))
        or normalize_date(source_row.get("fund_details_as_of"))
        or normalize_date(source_row.get("inception_date"))
        or fallback_date
    )

    return {
        "ETF Name": etf_name,
        "Issuer": clean_text(source_row.get("issuer")) or ISSUER,
        "ISIN": isin,
        "CCY": normalize_ccy(source_row.get("ccy") or source_row.get("aum_currency")),
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


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    captured_at, source_rows = parse_snapshot(resolved_input)
    fallback_date = captured_at or extract_file_date(resolved_input)
    transformed = [transform_row(source_row, fallback_date) for source_row in source_rows]
    return dedupe_rows([row for row in transformed if row is not None])


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output = output_path.resolve() if output_path else build_output_path(resolved_input)
    rows = extract_rows(resolved_input)
    write_csv(resolved_output, rows)
    print(f"Source file : {resolved_input}")
    print(f"Rows written: {len(rows):,}")
    print(f"Output file : {resolved_output}")
    return resolved_output


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
