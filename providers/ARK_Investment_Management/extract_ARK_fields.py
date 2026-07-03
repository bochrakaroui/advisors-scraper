"""Extract standardised ETF fields from a downloaded ARK Invest Europe XLSX snapshot.

Source columns produced by ark_extractor.py:
    FUND NAME | FUND URL | BASE CODE | ISIN | SFDR CLASSIFICATION |
    TER PCT   | AUM USD  | CCY       | FACTSHEET URL | SCRAPE DATE

    NOTE: older snapshots (before CCY column was added) only have AUM USD.
    The extractor handles both layouts — CCY is inferred from the AUM column
    header name ("AUM USD" → "USD") when no explicit CCY column is present.

Output columns:
    ETF Name | Issuer | ISIN | CCY | TER(bps) | AUM(M) | Date

Output path (same folder as the scraper):
    providers/ARK_Investment_Management/{date}/ark_selected_fields.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import datetime
from pathlib import Path

import openpyxl

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR           = Path(__file__).resolve().parent
INPUT_DIR          = BASE_DIR.parents[1] / "providers" / "ARK_Investment_Management"
LEGACY_INPUT_DIR   = BASE_DIR.parents[1] / "providers" / "ark"
PROVIDER_FOLDER    = "ARK_Investment_Management"
ISSUER             = "ARK Invest Europe"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
DEFAULT_CCY        = "USD"

OUTPUT_COLUMNS = ["ETF Name", "Issuer", "ISIN", "CCY", "TER(bps)", "AUM(M)", "Date"]

TER_PCT_TO_BPS      = 100
AUM_RAW_TO_MILLIONS = 1_000_000
KNOWN_CURRENCY_CODES = {
    "AED", "AUD", "BRL", "CAD", "CHF", "CNH", "CNY", "CZK", "DKK", "EUR", "GBP",
    "HKD", "HUF", "ILS", "INR", "JPY", "KRW", "KZT", "MXN", "NOK", "NZD", "PLN",
    "QAR", "RUB", "SAR", "SEK", "SGD", "TRY", "TWD", "USD", "ZAR",
}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract standardised ETF fields from an ARK XLSX snapshot."
    )
    p.add_argument("--input",  type=Path,
                   help="Path to ark_etfs_YYYY-MM-DD.xlsx. Defaults to latest found.")
    p.add_argument("--output", type=Path,
                   help="Output CSV path. Defaults to same folder as the XLSX.")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT FOLDER  –  same providers/ARK_Investment_Management/{date}/ as scraper
# ─────────────────────────────────────────────────────────────────────────────

def build_run_output_dir(base_dir: Path) -> Path:
    """
    Reuses ETF_PIPELINE_RUN_FOLDER as a run-folder name when present.
    Otherwise creates providers/ARK_Investment_Management/{today}/.
    """
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        output_dir = base_dir / datetime.now().strftime("%Y-%m-%d")
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def canonical_output_dir_for_snapshot(snapshot_path: Path) -> Path:
    """
    Keep outputs in providers/ARK_Investment_Management/{date}/ even if the
    source snapshot came from an older nested folder layout.
    """
    date_folder = snapshot_path.parent.name
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_folder):
        return INPUT_DIR / date_folder
    return snapshot_path.parent


# ─────────────────────────────────────────────────────────────────────────────
# INPUT DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

def find_latest_snapshot(base_dir: Path) -> Path:
    candidates = sorted(
        (p for p in base_dir.rglob("ark_etfs_*.xlsx") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No ark_etfs_*.xlsx found under {base_dir}.\n"
            "Run ark_extractor.py first."
        )
    return candidates[0]


def find_latest_snapshot_any() -> Path:
    for candidate_dir in (INPUT_DIR, LEGACY_INPUT_DIR):
        try:
            return find_latest_snapshot(candidate_dir)
        except FileNotFoundError:
            continue
    raise FileNotFoundError(
        f"No ark_etfs_*.xlsx found under {INPUT_DIR} or {LEGACY_INPUT_DIR}.\n"
        "Run ark_extractor.py first."
    )


def find_latest_download(input_dir: Path) -> Path:
    try:
        return find_latest_snapshot(input_dir)
    except FileNotFoundError:
        if input_dir.resolve() == INPUT_DIR.resolve():
            return find_latest_snapshot_any()
        raise


# ─────────────────────────────────────────────────────────────────────────────
# TEXT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)
                     .replace("\u00ad", "").replace("\u00a0", " ").strip())
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def extract_file_date(path: Path) -> str:
    for source in (path.stem, path.parent.name):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", source)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                pass
    return ""


def parse_ccy_from_col_name(col_name: str) -> str:
    """
    Infer CCY from the AUM column header when no explicit CCY column exists.
    'AUM USD'    -> 'USD'
    'AUM EUR'    -> 'EUR'
    'AUM (GBP)'  -> 'GBP'
    Falls back to DEFAULT_CCY.

    Skips tokens that are column-name keywords rather than ISO-4217 codes.
    """
    _SKIP = {"AUM", "ETF", "NAV", "TER", "CCY", "BPS", "PCT", "MNT", "MN"}
    for m in re.finditer(r"\b([A-Z]{3})\b", col_name.upper()):
        token = m.group(1)
        if token not in _SKIP:
            return token
    return DEFAULT_CCY


def normalize_ccy_value(raw_ccy: object | None, aum_col_name: str) -> str:
    value = clean_text(raw_ccy).upper()
    if value in KNOWN_CURRENCY_CODES:
        return value

    inferred_from_header = parse_ccy_from_col_name(aum_col_name) if aum_col_name else DEFAULT_CCY
    return inferred_from_header


# ─────────────────────────────────────────────────────────────────────────────
# XLSX LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_snapshot(path: Path) -> tuple[str, list[dict[str, str]]]:
    """
    Returns (scrape_date_dd_mm_yyyy, list_of_normalised_row_dicts).

    Handles two column layouts:
      New  (with CCY): … | AUM USD | CCY | FACTSHEET URL | SCRAPE DATE
      Old  (no CCY)  : … | AUM USD |     | FACTSHEET URL | SCRAPE DATE
    CCY is back-filled from the AUM column name for old-format files.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not all_rows:
        raise ValueError(f"Empty workbook: {path}")

    # Normalise header: 'FUND NAME' → 'fund_name', 'AUM USD' → 'aum_usd'
    raw_header = [str(h).strip() if h is not None else f"col_{i}"
                  for i, h in enumerate(all_rows[0])]
    norm_header = [re.sub(r"\s+", "_", h.lower()) for h in raw_header]

    # Detect whether a CCY column is present
    has_ccy_col = "ccy" in norm_header

    # Infer CCY from the AUM column name when it is absent or when the CCY
    # column is populated with a placeholder like "AUM".
    aum_col = next((h for h in raw_header if "AUM" in h.upper()), "AUM USD")
    inferred_ccy = parse_ccy_from_col_name(aum_col)

    data_rows: list[dict[str, str]] = []
    for raw_row in all_rows[1:]:
        if not any(cell is not None and str(cell).strip() for cell in raw_row):
            continue
        row = {norm_header[i]: clean_text(cell) for i, cell in enumerate(raw_row)}
        if not has_ccy_col:
            row["ccy"] = inferred_ccy
        else:
            row["ccy"] = normalize_ccy_value(row.get("ccy"), aum_col)
        data_rows.append(row)

    # Scrape date: read from first data row, fall back to file name
    scrape_date = ""
    if data_rows:
        raw_date = data_rows[0].get("scrape_date", "")
        if raw_date:
            try:
                scrape_date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                scrape_date = raw_date
    if not scrape_date:
        scrape_date = extract_file_date(path)

    return scrape_date, data_rows


# ─────────────────────────────────────────────────────────────────────────────
# FIELD TRANSFORMS
# ─────────────────────────────────────────────────────────────────────────────

def ter_pct_to_bps(raw: str) -> str:
    """'0.75' (%) → '75' (bps)."""
    try:
        return str(round(float(raw) * TER_PCT_TO_BPS))
    except (ValueError, TypeError):
        return ""


def aum_to_millions(raw: str) -> str:
    """'319747100' → '319.7471' (4 dp, stripped trailing zeros)."""
    try:
        val = float(raw.replace(",", "")) / AUM_RAW_TO_MILLIONS
        return f"{val:.4f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return ""


def transform_row(source: dict[str, str], scrape_date: str) -> dict[str, str]:
    return {
        "ETF Name" : clean_text(source.get("fund_name")),
        "Issuer"   : ISSUER,
        "ISIN"     : clean_text(source.get("isin")).upper(),
        "CCY"      : clean_text(source.get("ccy")).upper() or DEFAULT_CCY,
        "TER(bps)" : ter_pct_to_bps(source.get("ter_pct", "")),
        "AUM(M)"   : aum_to_millions(source.get("aum_usd", "")),
        "Date"     : scrape_date,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for row in rows:
        seen.setdefault(row.get("ISIN", "").upper(), row)
    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# CSV WRITER
# ─────────────────────────────────────────────────────────────────────────────

def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_csv_with_fallback(output_path: Path, rows: list[dict[str, str]]) -> Path:
    try:
        write_csv(output_path, rows)
        return output_path
    except PermissionError:
        fallback = output_path.with_name(f"{output_path.stem}_updated{output_path.suffix}")
        write_csv(fallback, rows)
        print(f"  ⚠ Output locked — wrote to: {fallback}")
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API  (importable by the orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved = input_path.resolve() if input_path else find_latest_snapshot_any()
    scrape_date, source_rows = load_snapshot(resolved)
    return dedupe_rows([transform_row(r, scrape_date) for r in source_rows])


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    _, rows = load_snapshot(path)
    return rows


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_snapshot_any()
    scrape_date, source_rows = load_snapshot(resolved_input)

    output_rows = dedupe_rows([transform_row(r, scrape_date) for r in source_rows])

    if output_path:
        resolved_output = output_path.resolve()
    else:
        # Always use the shared provider run-folder so both scripts write
        # to the same directory (providers/ARK_Investment_Management/{date}/).
        # Never use resolved_input.parent — it may be a read-only uploads dir.
        resolved_output = canonical_output_dir_for_snapshot(resolved_input) / "ark_selected_fields.csv"

    resolved_output = write_csv_with_fallback(resolved_output, output_rows)

    print(f"  Source file  : {resolved_input}")
    print(f"  Rows written : {len(output_rows):,}")
    print(f"  Unique ISINs : {len({r['ISIN'] for r in output_rows if r.get('ISIN')}):,}")
    print(f"  Missing ISIN : {sum(1 for r in output_rows if not r.get('ISIN')):,}")
    print(f"  Missing CCY  : {sum(1 for r in output_rows if not r.get('CCY')):,}")
    print(f"  Missing TER  : {sum(1 for r in output_rows if not r.get('TER(bps)')):,}")
    print(f"  Missing AUM  : {sum(1 for r in output_rows if not r.get('AUM(M)')):,}")
    print(f"  Output file  : {resolved_output}")

    return resolved_output


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
