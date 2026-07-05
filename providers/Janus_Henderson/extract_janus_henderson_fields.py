"""Extract selected Janus Henderson ETF fields from the latest snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
LEGACY_INPUT_DIR = BASE_DIR.parents[1] / "providers" / "Janus Henderson"
REPO_ROOT = BASE_DIR.parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from providers.output_schema import OUTPUT_COLUMNS, infer_aum_currency_from_row

SPACE_PATTERN = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), and Date "
            "from a Janus Henderson ETF snapshot."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Janus Henderson snapshot JSON path. Defaults to the latest janushenderson_etf_export.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output path. Defaults to the same folder as the source janushenderson_etf_export.json file.",
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


def infer_ccy_from_share_class_text(share_class_name: object | None, fallback_title: object | None = None) -> str:
    share_class_text = clean_text(share_class_name)
    if share_class_text:
        token = share_class_text.split(" ", 1)[0].upper().rstrip(".")
        direct_match = re.fullmatch(r"([A-Z]{3})(?:\b|-.+)", token)
        if direct_match:
            return direct_match.group(1)
        leading_match = re.match(r"^([A-Z]{3})(?:\b|[-/])", share_class_text.upper())
        if leading_match:
            return leading_match.group(1)

    title_match = re.search(r"\(([A-Z]{3})\)", clean_text(fallback_title))
    if title_match:
        return title_match.group(1)
    return ""


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
            for path in search_dir.rglob("janushenderson_etf_export.json")
            if search_dir.exists() and path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        searched_locations = ", ".join(str(path) for path in search_dirs)
        raise FileNotFoundError(
            f"No janushenderson_etf_export.json files found in {searched_locations}"
        )
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "janushenderson_selected_fields.csv"


def extract_file_date(input_path: Path) -> str:
    parent_date_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if parent_date_match:
        return datetime.strptime(parent_date_match.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
    return ""


def parse_iso_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def parse_display_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return cleaned


def parse_snapshot(path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(
            f"Unexpected Janus Henderson snapshot in {path}: expected listing_rows to be a list."
        )
    return parse_iso_date(payload.get("captured_at")), rows


def transform_row(source_row: dict[str, object], scrape_date: str) -> dict[str, str]:
    row_date = (
        parse_iso_date(source_row.get("date"))
        or parse_iso_date(source_row.get("data_as_of"))
        or parse_display_date(source_row.get("fund_assets_as_of"))
        or parse_display_date(source_row.get("nav_date"))
        or scrape_date
    )
    ccy = (
        clean_text(source_row.get("ccy")).upper()
        or clean_text(source_row.get("listing_currency")).upper()
        or clean_text(source_row.get("base_currency")).upper()
        or infer_ccy_from_share_class_text(
            source_row.get("share_class_name"),
            source_row.get("page_title") or source_row.get("etf_name"),
        )
    )
    aum_ccy = (
        infer_aum_currency_from_row(source_row)
        or clean_text(source_row.get("base_currency")).upper()
        or clean_text(source_row.get("listing_currency")).upper()
        or ccy
    )
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer")),
        "ISIN": normalize_isin(source_row.get("isin")),
        "CCY": ccy,
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_mn")),
        "AUM CCY": aum_ccy,
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


def write_csv_with_fallback(output_path: Path, rows: list[dict[str, str]]) -> Path:
    try:
        write_csv(output_path, rows)
        return output_path
    except PermissionError:
        fallback_path = output_path.with_name(f"{output_path.stem}_latest{output_path.suffix}")
        write_csv(fallback_path, rows)
        print(
            f"Output file locked, wrote the latest extract to: {fallback_path}"
        )
        return fallback_path


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    captured_at, source_rows = parse_snapshot(resolved_input_path)
    scrape_date = captured_at or extract_file_date(resolved_input_path)
    return dedupe_rows([transform_row(row, scrape_date) for row in source_rows])


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    resolved_output_path = write_csv_with_fallback(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing TER : {sum(1 for row in output_rows if not clean_text(row.get('TER(bps)'))):,}")
    print(f"Missing AUM : {sum(1 for row in output_rows if not clean_text(row.get('AUM(M)'))):,}")
    print(f"Missing AUM CCY : {sum(1 for row in output_rows if not clean_text(row.get('AUM CCY'))):,}")
    print(f"Output file : {resolved_output_path}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
