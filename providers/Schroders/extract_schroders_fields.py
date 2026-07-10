"""Extract the selected ETF fields from the latest Schroders raw JSON snapshot.

Mirrors extract_ishares_fields.py: finds the most recently scraped raw file,
pulls out the same standard set of columns, and writes a CSV next to the
source file inside its dated `providers/Schroders/<date>/` folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
REPO_ROOT = BASE_DIR.parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

try:
    from scrapers.justetf_profile import build_session as build_justetf_session
    from scrapers.justetf_profile import fetch_profile as fetch_justetf_profile
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    build_justetf_session = None  # type: ignore[assignment]
    fetch_justetf_profile = None  # type: ignore[assignment]

RAW_FILENAME = "schroders_etf_export.json"

SOURCE_FIELDS = {
    "fund_name": "etf_name",
    "issuer": "issuer",
    "isin": "isin",
    "currency": "ccy",
    "ter": "ter_bps",
    "aum": "aum_m",
}

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]

JUSTETF_FALLBACK_ISINS = [
    "IE000BNLRWE6",
    "IE000FGFJT15",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract the selected ETF fields from a downloaded Schroders JSON snapshot.")
    parser.add_argument("--input", type=Path, help="Raw Schroders JSON file. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a CSV alongside the input file.")
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="Keep rows whose extraction_method is 'failed'. By default these are dropped.",
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
        (path for path in input_dir.rglob(RAW_FILENAME) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No {RAW_FILENAME} files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "schroders_selected_fields.csv"


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    cleaned = str(value).strip()
    return "" if cleaned in {"", "-", "- ", " -", "None"} else cleaned


def format_decimal(value: Any, places: int = 2) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    try:
        decimal_value = Decimal(cleaned)
    except InvalidOperation:
        return cleaned

    quantized = decimal_value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def extract_file_date(input_path: Path) -> str:
    """Date column mirrors extract_ishares_fields.py: derived from the dated
    run folder (providers/Schroders/<date>/...), not from any in-row field,
    so it's consistent regardless of what each ISIN's own date string looked
    like (DOM-fallback rows can carry differently formatted date strings)."""
    parent_date_match = input_path.parent.name
    try:
        return datetime.strptime(parent_date_match, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


def load_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if rows is None:
        raise ValueError(f"No 'rows' key found in {path}")
    return rows


def parse_snapshot_rows(path: Path) -> list[dict[str, Any]]:
    return load_rows(path)


def transform_row(source_row: dict[str, Any], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_FIELDS["fund_name"])),
        "Issuer": clean_text(source_row.get(SOURCE_FIELDS["issuer"])),
        "ISIN": clean_text(source_row.get(SOURCE_FIELDS["isin"])).upper(),
        "CCY": clean_text(source_row.get(SOURCE_FIELDS["currency"])).upper(),
        "TER(bps)": format_decimal(source_row.get(SOURCE_FIELDS["ter"])),
        "AUM(M)": format_decimal(source_row.get(SOURCE_FIELDS["aum"])),
        "Date": file_date,
    }


def filter_rows(rows: list[dict[str, Any]], include_failed: bool) -> list[dict[str, Any]]:
    if include_failed:
        return rows
    return [row for row in rows if row.get("extraction_method") != "failed"]


def source_row_has_positive_aum(row: dict[str, Any]) -> bool:
    cleaned = clean_text(row.get(SOURCE_FIELDS["aum"])).replace(",", "")
    if not cleaned:
        return False
    try:
        return Decimal(cleaned) > 0
    except (InvalidOperation, ValueError):
        return False


def supplement_from_latest_successful_snapshot(
    rows: list[dict[str, str]],
    input_path: Path,
    target_isins: set[str],
) -> list[dict[str, str]]:
    present_isins = {clean_text(row.get("ISIN")).upper() for row in rows if clean_text(row.get("ISIN"))}
    unresolved_isins = {isin for isin in target_isins if isin and isin not in present_isins}
    if not unresolved_isins:
        return rows

    try:
        current_run_date = datetime.strptime(input_path.parent.name, "%Y-%m-%d")
    except ValueError:
        current_run_date = datetime.max

    candidates = sorted(
        (
            path
            for path in INPUT_DIR.rglob(RAW_FILENAME)
            if path.is_file() and path.resolve() != input_path.resolve()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    recovered_rows: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        try:
            candidate_run_date = datetime.strptime(candidate.parent.name, "%Y-%m-%d")
        except ValueError:
            continue
        if candidate_run_date >= current_run_date:
            continue

        for source_row in load_rows(candidate):
            isin = clean_text(source_row.get(SOURCE_FIELDS["isin"])).upper()
            if (
                isin not in unresolved_isins
                or isin in recovered_rows
                or source_row.get("extraction_method") == "failed"
                or not source_row_has_positive_aum(source_row)
            ):
                continue
            recovered_rows[isin] = transform_row(source_row, extract_file_date(candidate))
        if unresolved_isins <= set(recovered_rows):
            break

    if recovered_rows:
        print(
            "Recovered latest successful official Schroders AUM for: "
            + ", ".join(sorted(recovered_rows))
        )
    return rows + [recovered_rows[isin] for isin in sorted(recovered_rows)]


def supplement_missing_rows(rows: list[dict[str, str]], file_date: str) -> list[dict[str, str]]:
    if build_justetf_session is None or fetch_justetf_profile is None:
        return rows

    present_isins = {clean_text(row.get("ISIN")).upper() for row in rows if clean_text(row.get("ISIN"))}
    missing_isins = [isin for isin in JUSTETF_FALLBACK_ISINS if isin not in present_isins]
    if not missing_isins:
        return rows

    session = build_justetf_session()
    supplemented_rows = list(rows)
    for isin in missing_isins:
        try:
            profile = fetch_justetf_profile(isin, session=session)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: justETF fallback failed for Schroders {isin}: {exc}")
            continue

        if clean_text(profile.get("fetch_status")) not in {"", "ok"}:
            print(f"WARNING: justETF fallback did not resolve Schroders {isin}: {clean_text(profile.get('error'))}")
            continue

        supplemented_rows.append(
            {
                "ETF Name": clean_text(profile.get("etf_name")),
                "Issuer": "Schroders",
                "ISIN": clean_text(profile.get("isin")).upper() or isin,
                "CCY": clean_text(profile.get("ccy")).upper(),
                "TER(bps)": format_decimal(profile.get("ter_bps")),
                "AUM(M)": format_decimal(profile.get("aum_mn")),
                "Date": file_date,
            }
        )

    return supplemented_rows


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def extract_rows(input_path: Path | None = None, *, include_failed: bool = False) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    raw_rows = load_rows(resolved_input_path)
    filtered_rows = filter_rows(raw_rows, include_failed=include_failed)
    file_date = extract_file_date(resolved_input_path)
    output_rows = [transform_row(row, file_date) for row in filtered_rows]
    failed_isins = {
        clean_text(row.get(SOURCE_FIELDS["isin"])).upper()
        for row in raw_rows
        if row.get("extraction_method") == "failed" and clean_text(row.get(SOURCE_FIELDS["isin"]))
    }
    output_rows = supplement_from_latest_successful_snapshot(output_rows, resolved_input_path, failed_isins)
    return supplement_missing_rows(output_rows, file_date)


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
    *,
    include_failed: bool = False,
) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = (
        output_path.resolve() if output_path else build_output_path(resolved_input_path)
    )

    output_rows = extract_rows(resolved_input_path, include_failed=include_failed)

    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {resolved_output_path}")
    print(f"Filter      : {'All rows (incl. failed)' if include_failed else 'Successful extractions only'}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output, include_failed=args.include_failed)


if __name__ == "__main__":
    main()
