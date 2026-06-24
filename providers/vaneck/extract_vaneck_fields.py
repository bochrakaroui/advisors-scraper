"""Extract the selected ETF fields from the latest downloaded VanEck file.

The VanEck "Download Excel" export is an HTML table saved as .xls.
Structure (single <table id="export-excel-table">):
  - <thead> with one <tr> of <th> columns
  - <tbody> with one <tr> per fund; text values inside <a> anchors in every cell

Source columns used
-------------------
Ticker | ISIN | Name | Asset Class | Category | Income Treatment |
TER | Inception Date | SFDR Classification | Total Net Assets

Output columns (matching the iShares pipeline schema)
------------------------------------------------------
ETF Name | Issuer | ISIN | CCY | TER(bps) | AUM(M) | Date
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

# Fixed issuer for every row – VanEck is the sole provider in this file.
ISSUER = "VanEck"

# Column names as they appear in the HTML <th> elements.
SOURCE_COLUMNS = {
    "ticker": "Ticker",
    "isin": "ISIN",
    "name": "Name",
    "asset_class": "Asset Class",
    "category": "Category",
    "income_treatment": "Income Treatment",
    "ter": "TER",
    "inception_date": "Inception Date",
    "sfdr": "SFDR Classification",
    "aum": "Total Net Assets",
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

# AUM suffix multipliers (values are already expressed in M/B).
_AUM_MULTIPLIERS: dict[str, Decimal] = {
    "B": Decimal("1000"),   # convert billions → millions
    "M": Decimal("1"),      # already in millions
    "K": Decimal("0.001"),  # thousands → millions (edge case)
}

# Currency symbols that may prefix AUM values.
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

class _TableParser(HTMLParser):
    """Minimal SAX-style parser for the single HTML table in the VanEck export."""

    def __init__(self) -> None:
        super().__init__()
        self._headers: list[str] = []
        self._rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: str = ""
        self._in_cell: bool = False
        self._in_header: bool = False
        self._in_head: bool = False

    # ------------------------------------------------------------------
    # HTMLParser callbacks
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "thead":
            self._in_head = True
        elif tag == "tbody":
            self._in_head = False
        elif tag == "tr":
            self._current_row = []
        elif tag in ("th", "td"):
            self._in_cell = True
            self._in_header = tag == "th"
            self._current_cell = ""

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td"):
            text = self._current_cell.strip()
            if self._in_header and self._current_row is not None:
                self._current_row.append(text)
            elif not self._in_header and self._current_row is not None:
                self._current_row.append(text)
            self._in_cell = False
            self._in_header = False
            self._current_cell = ""
        elif tag == "tr":
            if self._current_row is not None:
                if self._in_head and self._current_row:
                    # Header row: strip any nested <th> artefacts from the
                    # malformed thead produced by the VanEck exporter.
                    self._headers = [h.strip() for h in self._current_row if h.strip()]
                elif not self._in_head and self._current_row:
                    self._rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell += data

    # ------------------------------------------------------------------
    # Public result accessors
    # ------------------------------------------------------------------

    @property
    def headers(self) -> list[str]:
        return self._headers

    @property
    def rows(self) -> list[list[str]]:
        return self._rows


def parse_html_table(path: Path) -> tuple[list[str], list[list[str]]]:
    """Return (headers, data_rows) from the VanEck HTML-as-XLS file."""
    content = path.read_text(encoding="utf-8", errors="replace")
    parser = _TableParser()
    parser.feed(content)

    if not parser.headers:
        raise ValueError(f"No table headers found in {path}")
    if not parser.rows:
        raise ValueError(f"No data rows found in {path}")

    return parser.headers, parser.rows


# ---------------------------------------------------------------------------
# Text / number helpers  (same conventions as extract_ishares_fields.py)
# ---------------------------------------------------------------------------

def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = value.strip()
    return "" if cleaned in {"", "-", "- ", " -"} else cleaned


def format_decimal(value: str | None, places: int = 2) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return cleaned
    quantized = dec.quantize(Decimal("1." + "0" * places), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def format_ter(value: str | None) -> str:
    """Convert a percentage TER string ('0.40%') to basis points ('40.00')."""
    cleaned = clean_text(value).replace("%", "").strip()
    if not cleaned:
        return ""
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return format_decimal(str(Decimal(cleaned) * Decimal("100")), places=2)
    except InvalidOperation:
        return cleaned


def parse_aum(value: str | None) -> tuple[str, str]:
    """Return (aum_in_millions_str, currency_code) from a value like '$637.4M' or '€1.4B'.

    Unknown / unparseable values return ('', '').
    """
    cleaned = clean_text(value)
    if not cleaned:
        return "", ""

    # Detect currency symbol
    ccy = ""
    if cleaned[0] in _CURRENCY_SYMBOLS:
        ccy = _CURRENCY_SYMBOLS[cleaned[0]]
        cleaned = cleaned[1:]

    # Detect suffix multiplier (last character)
    multiplier = Decimal("1")
    if cleaned and cleaned[-1].upper() in _AUM_MULTIPLIERS:
        multiplier = _AUM_MULTIPLIERS[cleaned[-1].upper()]
        cleaned = cleaned[:-1]

    try:
        raw = Decimal(cleaned) * multiplier
        return format_decimal(str(raw), places=2), ccy
    except InvalidOperation:
        return cleaned, ccy


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def extract_file_date(input_path: Path) -> str:
    """Infer the snapshot date from the filename or parent folder name."""
    # Pattern: YYYYMMDD_HHMMSS in the stem
    match = re.search(r"(\d{8}_\d{6})", input_path.stem)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").strftime(
                "%d/%m/%Y %H:%M:%S"
            )
        except ValueError:
            pass

    # Pattern: YYYY-MM-DD in the parent folder name
    parent_match = re.match(r"(\d{4}-\d{2}-\d{2})", input_path.parent.name)
    if parent_match:
        return datetime.strptime(parent_match.group(1), "%Y-%m-%d").strftime(
            "%d/%m/%Y 00:00:00"
        )

    return ""


# ---------------------------------------------------------------------------
# Row transformation
# ---------------------------------------------------------------------------

def transform_row(
    source: dict[str, str],
    file_date: str,
) -> dict[str, str]:
    aum_m, _ccy = parse_aum(source.get(SOURCE_COLUMNS["aum"]))

    return {
        "ETF Name": clean_text(source.get(SOURCE_COLUMNS["name"])),
        "Issuer": ISSUER,
        "ISIN": clean_text(source.get(SOURCE_COLUMNS["isin"])).upper(),
        "CCY": _ccy,
        "TER(bps)": format_ter(source.get(SOURCE_COLUMNS["ter"])),
        "AUM(M)": aum_m,
        "Date": file_date,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (p for p in input_dir.rglob("*.xls") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No .xls files found in {input_dir}")
    return candidates[0]


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    headers, raw_rows = parse_html_table(path)
    rows: list[dict[str, str]] = []

    for raw_row in raw_rows:
        rows.append(
            {
                headers[i]: raw_row[i] if i < len(raw_row) else ""
                for i in range(len(headers))
            }
        )

    return rows


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


def build_output_path(output_dir: Path) -> Path:
    dated_output_dir = build_run_output_dir(output_dir)
    return dated_output_dir / "vaneck_selected_fields.csv"


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Public API  (mirrors extract_ishares_fields.py)
# ---------------------------------------------------------------------------

def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    headers, raw_rows = parse_html_table(resolved)

    missing = [col for col in SOURCE_COLUMNS.values() if col not in headers]
    if missing:
        raise ValueError(f"Missing expected source columns: {', '.join(missing)}")

    file_date = extract_file_date(resolved)

    result: list[dict[str, str]] = []
    for raw_row in raw_rows:
        # Zip to header names; pad with empty strings if a row is short.
        source = {
            headers[i]: raw_row[i] if i < len(raw_row) else ""
            for i in range(len(headers))
        }
        name = clean_text(source.get(SOURCE_COLUMNS["name"], ""))
        if not name:
            continue  # skip blank / footer rows
        result.append(transform_row(source, file_date))

    return result


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    resolved_input = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output = (
        output_path.resolve() if output_path else build_output_path(OUTPUT_DIR)
    )

    output_rows = extract_rows(resolved_input)
    write_csv(resolved_output, output_rows)

    print(f"Source file : {resolved_input}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {resolved_output}")
    return resolved_output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract selected ETF fields from a downloaded VanEck .xls file."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Downloaded VanEck .xls file. Defaults to the latest file found.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Processed CSV path. Defaults to a date folder inside ./vaneck.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
