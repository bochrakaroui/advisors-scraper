"""Extract selected American Century Investments ETF fields from the latest snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from providers.output_schema import OUTPUT_COLUMNS
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from output_schema import OUTPUT_COLUMNS


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
ISSUER = "American Century Investments"

SPACE_PATTERN = re.compile(r"\s+")
JUSTETF_FALLBACK_ISINS = ["IE000K975W13"]

try:
    from scrapers.justetf_profile import build_session as build_justetf_session
    from scrapers.justetf_profile import fetch_profile as fetch_justetf_profile
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    build_justetf_session = None  # type: ignore[assignment]
    fetch_justetf_profile = None  # type: ignore[assignment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), AUM CCY, and Date "
            "from an American Century Investments ETF snapshot."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help=(
            "American Century Investments snapshot JSON path. Defaults to the latest "
            "american_century_investments_etf_export.json."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "CSV output path. Defaults to the same folder as the source "
            "american_century_investments_etf_export.json file."
        ),
    )
    return parser.parse_args()


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None", "null", "nan", "NaN"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in input_dir.rglob("american_century_investments_etf_export.json")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No american_century_investments_etf_export.json files found in {input_dir}"
        )
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "american_century_investments_selected_fields.csv"


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
        return cleaned


def parse_snapshot(path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(
            "Unexpected American Century Investments snapshot in "
            f"{path}: expected listing_rows to be a list."
        )
    return parse_iso_date(payload.get("captured_at")), rows


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    _, rows = parse_snapshot(path)
    return rows


def transform_row(source_row: dict[str, object], scrape_date: str) -> dict[str, str]:
    row_date = (
        parse_iso_date(source_row.get("total_assets_date"))
        or parse_iso_date(source_row.get("nav_date"))
        or parse_iso_date(source_row.get("fund_inception_date"))
        or scrape_date
    )
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer")) or ISSUER,
        "ISIN": normalize_isin(source_row.get("isin")),
        "CCY": clean_text(source_row.get("fund_currency")).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps") or source_row.get("factsheet_ter_bps")),
        "AUM(M)": clean_text(source_row.get("aum_mn")),
        "AUM CCY": clean_text(source_row.get("aum_ccy") or source_row.get("fund_currency")).upper(),
        "Date": row_date,
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(clean_text(row.get(column)).upper() for column in OUTPUT_COLUMNS)
        deduped.setdefault(key, row)
    return list(deduped.values())


def supplement_missing_rows(rows: list[dict[str, str]], scrape_date: str) -> list[dict[str, str]]:
    if build_justetf_session is None or fetch_justetf_profile is None:
        return dedupe_rows(rows)

    present_isins = {normalize_isin(row.get("ISIN")) for row in rows if normalize_isin(row.get("ISIN"))}
    missing_isins = [isin for isin in JUSTETF_FALLBACK_ISINS if isin not in present_isins]
    if not missing_isins:
        return dedupe_rows(rows)

    session = build_justetf_session()
    supplemented_rows = list(rows)
    for isin in missing_isins:
        try:
            profile = fetch_justetf_profile(isin, session=session)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: justETF fallback failed for American Century Investments {isin}: {exc}")
            continue

        if clean_text(profile.get("fetch_status")) not in {"", "ok"}:
            print(
                "WARNING: justETF fallback did not resolve American Century Investments "
                f"{isin}: {clean_text(profile.get('error'))}"
            )
            continue

        supplemented_rows.append(
            {
                "ETF Name": clean_text(profile.get("etf_name")),
                "Issuer": ISSUER,
                "ISIN": normalize_isin(profile.get("isin")) or isin,
                "CCY": clean_text(profile.get("ccy")).upper(),
                "TER(bps)": clean_text(profile.get("ter_bps")),
                "AUM(M)": clean_text(profile.get("aum_mn")),
                "AUM CCY": clean_text(profile.get("aum_ccy")).upper() or clean_text(profile.get("ccy")).upper(),
                "Date": scrape_date,
            }
        )

    return dedupe_rows(supplemented_rows)


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
    output_rows = [transform_row(row, scrape_date) for row in source_rows]
    return supplement_missing_rows(output_rows, scrape_date)


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
