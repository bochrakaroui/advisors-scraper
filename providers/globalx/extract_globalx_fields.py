"""Extract the selected ETF fields from the latest downloaded Global X snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
ISSUER = "Global X ETFs"
REPO_ROOT = BASE_DIR.parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

try:
    from scrapers.justetf_profile import build_session as build_justetf_session
    from scrapers.justetf_profile import fetch_profile as fetch_justetf_profile
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    build_justetf_session = None  # type: ignore[assignment]
    fetch_justetf_profile = None  # type: ignore[assignment]

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

JUSTETF_FALLBACK_ISINS = [
    "IE000544AJM3",
    "IE000MS9DTS9",
    "IE000YICM5P9",
    "IE00BLCHJ641",
    "IE00BLCHJC08",
    "IE00BLCHK052",
    "IE00BLR6Q650",
    "IE00BLR6QC17",
    "IE00BM8R0H36",
    "IE00BMH5YS76",
]
ALLOW_NON_OFFICIAL_ROW_SUPPLEMENT_ENV_VAR = "GLOBALX_ALLOW_NON_OFFICIAL_ROW_SUPPLEMENT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from a Global X .json snapshot."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Downloaded Global X .json snapshot. Defaults to the latest globalx_etf_export.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to globalx_selected_fields.csv next to the input JSON.",
    )
    return parser.parse_args()


def non_official_row_supplement_allowed() -> bool:
    return os.environ.get(ALLOW_NON_OFFICIAL_ROW_SUPPLEMENT_ENV_VAR, "").strip().lower() not in {"0", "false", "no", "off"}


def clean_text(value: object | None) -> str:
    if value is None:
        return ""

    cleaned = (
        str(value)
        .replace("\u00ad", "")
        .replace("\u00a0", " ")
        .replace("Â", "")
        .strip()
    )
    cleaned = re.sub(r"\s+", " ", cleaned)

    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def canonicalize_fund_name(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"^Global X\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\s+(?:USD|EUR|GBP|CHF)\s+(?:Distributing|Accumulating)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+Dist\s+(?:USD|EUR|GBP|CHF)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(?:Distributing|Accumulating)\b.*$", "", cleaned, flags=re.IGNORECASE)
    return clean_text(cleaned).casefold()


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in input_dir.rglob("globalx_etf_export.json")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        raise FileNotFoundError(f"No globalx_etf_export.json files found in {input_dir}")

    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "globalx_selected_fields.csv"


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


def normalize_date(raw_value: str, fallback: str = "") -> str:
    cleaned = clean_text(raw_value)
    if not cleaned:
        return fallback

    if re.fullmatch(r"\d{2}/\d{2}/\d{4}(?: 00:00:00)?", cleaned):
        return cleaned[:10]

    match = re.search(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b", cleaned)
    if match:
        cleaned = match.group(0)

    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    return cleaned or fallback


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])

    if not isinstance(rows, list):
        raise ValueError(f"Unexpected Global X snapshot in {path}: expected rows to be a list.")

    return rows


def transform_row(source_row: dict[str, object], file_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer") or ISSUER),
        "ISIN": clean_text(source_row.get("isin")).upper(),
        "CCY": clean_text(
            source_row.get("ccy")
            or source_row.get("aum_currency")
        ).upper(),
        "TER(bps)": clean_text(source_row.get("ter_bps")),
        "AUM(M)": clean_text(
            source_row.get("aum_m")
            or source_row.get("aum_numeric")
        ),
        "AUM CCY": clean_text(source_row.get("aum_currency") or source_row.get("ccy")).upper(),
        "Date": normalize_date(
            clean_text(source_row.get("date") or source_row.get("as_of_date")),
            fallback=file_date,
        ),
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}

    for row in rows:
        key = tuple(clean_text(row.get(column)).upper() for column in OUTPUT_COLUMNS)
        deduped.setdefault(key, row)

    return list(deduped.values())


def supplement_missing_rows(rows: list[dict[str, str]], file_date: str) -> list[dict[str, str]]:
    if not non_official_row_supplement_allowed():
        return dedupe_rows(rows)
    if build_justetf_session is None or fetch_justetf_profile is None:
        return dedupe_rows(rows)

    official_rows_by_name: dict[str, dict[str, str]] = {}
    for row in rows:
        canonical_name = canonicalize_fund_name(row.get("ETF Name"))
        if canonical_name and clean_text(row.get("AUM(M)")) and canonical_name not in official_rows_by_name:
            official_rows_by_name[canonical_name] = row

    present_isins = {clean_text(row.get("ISIN")).upper() for row in rows if clean_text(row.get("ISIN"))}
    missing_isins = [isin for isin in JUSTETF_FALLBACK_ISINS if isin not in present_isins]
    if not missing_isins:
        return dedupe_rows(rows)

    session = build_justetf_session()
    supplemented_rows = list(rows)
    for isin in missing_isins:
        try:
            profile = fetch_justetf_profile(isin, session=session)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: justETF fallback failed for Global X {isin}: {exc}")
            continue

        if clean_text(profile.get("fetch_status")) not in {"", "ok"}:
            print(f"WARNING: justETF fallback did not resolve Global X {isin}: {clean_text(profile.get('error'))}")
            continue

        etf_name = clean_text(profile.get("etf_name"))
        ccy = clean_text(profile.get("ccy")).upper()
        ter_bps = clean_text(profile.get("ter_bps"))
        fallback_aum = clean_text(profile.get("aum_mn"))
        fallback_aum_ccy = clean_text(profile.get("aum_ccy")).upper()

        sibling_official_row = official_rows_by_name.get(canonicalize_fund_name(etf_name))
        supplemented_rows.append(
            {
                "ETF Name": etf_name,
                "Issuer": ISSUER,
                "ISIN": clean_text(profile.get("isin")).upper() or isin,
                "CCY": ccy,
                "TER(bps)": ter_bps,
                "AUM(M)": clean_text(
                    sibling_official_row.get("AUM(M)") if sibling_official_row else fallback_aum
                ),
                "AUM CCY": clean_text(
                    sibling_official_row.get("AUM CCY") if sibling_official_row else fallback_aum_ccy
                ).upper(),
                "Date": clean_text(sibling_official_row.get("Date") if sibling_official_row else file_date),
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
    source_rows = parse_snapshot_rows(resolved_input_path)
    file_date = extract_file_date(resolved_input_path)

    output_rows = [transform_row(row, file_date) for row in source_rows]
    return supplement_missing_rows(output_rows, file_date)


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing ETF Name: {sum(1 for row in output_rows if not clean_text(row.get('ETF Name'))):,}")
    print(f"Missing ISIN: {sum(1 for row in output_rows if not clean_text(row.get('ISIN'))):,}")
    print(f"Missing CCY: {sum(1 for row in output_rows if not clean_text(row.get('CCY'))):,}")
    print(f"Missing TER(bps): {sum(1 for row in output_rows if not clean_text(row.get('TER(bps)'))):,}")
    print(f"Missing AUM(M): {sum(1 for row in output_rows if not clean_text(row.get('AUM(M)'))):,}")
    print(f"Missing Date: {sum(1 for row in output_rows if not clean_text(row.get('Date'))):,}")
    print(f"Output file : {resolved_output_path}")

    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
