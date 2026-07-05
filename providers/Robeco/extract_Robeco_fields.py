"""Extract selected ETF fields from the latest downloaded Robeco file.

Source columns (confirmed from robeco_etf_export.xlsx):
    col 2  "Share Class Name"    → ETF Name
    col 3  "ISIN"                → ISIN
    col 14 "Share Class Currency"→ CCY
    col 21 "Ongoing Charges"     → TER  (e.g. "0.25%"  → 25.00 bps)
    col 16 "Share Class Size"    → AUM  (e.g. "635,373,873" → 635.37 M)
    col 18 "Inception Date"      → used to derive Date when folder name is absent

Output columns (same contract as Amundi extractor):
    ETF Name | Issuer | ISIN | CCY | TER(bps) | AUM(M) | Date
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zipfile import ZipFile


# ---------------------------------------------------------------------------
# Paths  –  output lands in the same folder as the source XLSX
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
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

XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
JUSTETF_FALLBACK_ISINS = ["IE00063T9YS5"]

# Exact header strings as they appear in row 1 of robeco_etf_export.xlsx
SOURCE_COLUMNS = {
    "fund_name": "Share Class Name",
    "isin":      "ISIN",
    "currency":  "Share Class Currency",
    "ter":       "Ongoing Charges",
    "aum":       "Share Class Size",
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from a downloaded Robeco .xlsx file."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Robeco .xlsx file path. Defaults to the latest file found under the script directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to robeco_selected_fields.csv next to the source file.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (p for p in input_dir.rglob("robeco_etf_export.xlsx") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        # Fallback: any xlsx in the tree
        candidates = sorted(
            (p for p in input_dir.rglob("*.xlsx") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    if not candidates:
        raise FileNotFoundError(f"No .xlsx files found under {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    """CSV goes into the same folder as the source XLSX."""
    return input_path.parent / "robeco_selected_fields.csv"


# ---------------------------------------------------------------------------
# Text / number helpers
# ---------------------------------------------------------------------------
def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -"} else cleaned


def format_decimal(value: str | Decimal | None, places: int = 2) -> str:
    if isinstance(value, Decimal):
        decimal_value = value
    else:
        cleaned = clean_text(value)
        if not cleaned:
            return ""
        try:
            decimal_value = Decimal(cleaned)
        except InvalidOperation:
            return cleaned
    quantized = decimal_value.quantize(
        Decimal("1." + "0" * places), rounding=ROUND_HALF_UP
    )
    return format(quantized, f".{places}f")


def format_ter(value: str | None) -> str:
    """Convert "0.25%" → "25.00" (basis points, 2 d.p.)."""
    cleaned = clean_text(value).replace("%", "").strip()
    if not cleaned:
        return ""
    # Handle comma-as-decimal-separator locales
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return format_decimal(str(Decimal(cleaned) * Decimal("100")), places=2)
    except InvalidOperation:
        return cleaned


def format_aum_millions(value: str | None) -> str:
    """Convert "635,373,713" (raw units) → "635.37" (millions, 2 d.p.)."""
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    # Strip thousands separators and any trailing currency labels
    numeric = re.sub(r"[^\d.]", "", cleaned.replace(",", ""))
    if not numeric:
        return cleaned
    try:
        return format_decimal(str(Decimal(numeric) / Decimal("1_000_000")), places=2)
    except InvalidOperation:
        return cleaned


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def extract_run_date(input_path: Path) -> str:
    """
    Derive the scrape/run date (dd/mm/yyyy) in this order of priority:
      1. ETF_PIPELINE_RUN_FOLDER env var  (set by the scraper, e.g. "2025-06-24")
      2. Parent folder name matching YYYY-MM-DD
      3. File mtime as last resort
    """
    run_folder = os.environ.get(RUN_FOLDER_ENV_VAR, "")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", run_folder)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")

    m = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")

    return datetime.fromtimestamp(input_path.stat().st_mtime).strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Low-level XLSX reader  (same approach as Amundi extractor)
# ---------------------------------------------------------------------------
def load_shared_strings(workbook: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.findall(".//a:t", XLSX_NS))
        for item in root.findall("a:si", XLSX_NS)
    ]


def extract_column_letters(cell_ref: str) -> str:
    m = re.match(r"[A-Z]+", cell_ref)
    return m.group(0) if m else ""


def parse_sheet_row(row: ET.Element, shared_strings: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for cell in row.findall("a:c", XLSX_NS):
        col = extract_column_letters(cell.attrib.get("r", ""))
        if not col:
            continue
        cell_type = cell.attrib.get("t")
        raw = cell.find("a:v", XLSX_NS)
        if cell_type == "s" and raw is not None and raw.text is not None:
            value = shared_strings[int(raw.text)]
        elif cell_type == "inlineStr":
            value = "".join(n.text or "" for n in cell.findall(".//a:t", XLSX_NS))
        else:
            value = "" if raw is None or raw.text is None else raw.text
        values[col] = value
    return values


def parse_xlsx_rows(path: Path) -> list[dict[str, str]]:
    """Return a list of dicts keyed by the header strings from row 1."""
    with ZipFile(path) as wb:
        shared_strings = load_shared_strings(wb)
        sheet_root = ET.fromstring(wb.read("xl/worksheets/sheet1.xml"))

    header_by_col: dict[str, str] = {}
    rows: list[dict[str, str]] = []

    for row in sheet_root.findall(".//a:sheetData/a:row", XLSX_NS):
        row_num = int(row.attrib.get("r", "0"))
        values_by_col = parse_sheet_row(row, shared_strings)

        if row_num == 1:
            # Row 1 is the header row
            header_by_col = {
                col: clean_text(val)
                for col, val in values_by_col.items()
                if clean_text(val)
            }
            continue

        if not header_by_col:
            continue

        record = {
            header_by_col[col]: val
            for col, val in values_by_col.items()
            if col in header_by_col
        }
        # Only keep rows that have a fund name
        if clean_text(record.get(SOURCE_COLUMNS["fund_name"])):
            rows.append(record)

    return rows


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def transform_row(source_row: dict[str, str], run_date: str) -> dict[str, str]:
    return {
        "ETF Name": clean_text(source_row.get(SOURCE_COLUMNS["fund_name"])),
        "Issuer":   "Robeco",
        "ISIN":     clean_text(source_row.get(SOURCE_COLUMNS["isin"])).upper(),
        "CCY":      clean_text(source_row.get(SOURCE_COLUMNS["currency"])).upper(),
        "TER(bps)": format_ter(source_row.get(SOURCE_COLUMNS["ter"])),
        "AUM(M)":   format_aum_millions(source_row.get(SOURCE_COLUMNS["aum"])),
        "Date":     run_date,
    }


def dedupe_rows_by_isin(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[str, dict[str, str]] = {}
    ordered_rows: list[dict[str, str]] = []
    for row in rows:
        isin = clean_text(row.get("ISIN")).upper()
        if isin and isin in deduped:
            continue
        if isin:
            deduped[isin] = row
        ordered_rows.append(row)
    return ordered_rows


def supplement_missing_rows(rows: list[dict[str, str]], run_date: str) -> list[dict[str, str]]:
    if build_justetf_session is None or fetch_justetf_profile is None:
        return dedupe_rows_by_isin(rows)

    present_isins = {clean_text(row.get("ISIN")).upper() for row in rows if clean_text(row.get("ISIN"))}
    missing_isins = [isin for isin in JUSTETF_FALLBACK_ISINS if isin not in present_isins]
    if not missing_isins:
        return dedupe_rows_by_isin(rows)

    session = build_justetf_session()
    supplemented_rows = list(rows)
    for isin in missing_isins:
        try:
            profile = fetch_justetf_profile(isin, session=session)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: justETF fallback failed for Robeco {isin}: {exc}")
            continue

        if clean_text(profile.get("fetch_status")) not in {"", "ok"}:
            print(f"WARNING: justETF fallback did not resolve Robeco {isin}: {clean_text(profile.get('error'))}")
            continue

        supplemented_rows.append(
            {
                "ETF Name": clean_text(profile.get("etf_name")),
                "Issuer": "Robeco",
                "ISIN": clean_text(profile.get("isin")).upper() or isin,
                "CCY": clean_text(profile.get("ccy")).upper(),
                "TER(bps)": clean_text(profile.get("ter_bps")),
                "AUM(M)": clean_text(profile.get("aum_mn")),
                "Date": run_date,
            }
        )

    return dedupe_rows_by_isin(supplemented_rows)


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------
def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Public API  (matches Amundi extractor's interface)
# ---------------------------------------------------------------------------
def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    source_rows = parse_xlsx_rows(resolved)
    run_date = extract_run_date(resolved)
    output_rows = [transform_row(r, run_date) for r in source_rows]
    return supplement_missing_rows(output_rows, run_date)


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    return parse_xlsx_rows(path)


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output = output_path.resolve() if output_path else build_output_path(resolved_input)

    output_rows = extract_rows(resolved_input)
    write_csv(resolved_output, output_rows)

    print(f"Source file : {resolved_input}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {resolved_output}")
    return resolved_output


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
