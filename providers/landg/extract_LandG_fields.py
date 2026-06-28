

from __future__ import annotations

import argparse
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

ISSUER = "L&G"

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]

SUPPORTED_SUFFIXES = {".xlsx", ".xls", ".csv"}
ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", re.IGNORECASE)
DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")


# -----------------------------------------------------------------------------
# CLI / paths
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), Date from an L&G export."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="L&G downloaded export file. Defaults to the latest providers/landg/**/landg_etf_export_* file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path. Defaults to landg_selected_fields_<date>.csv next to the input file.",
    )
    parser.add_argument(
        "--provider-dir",
        type=Path,
        help="Provider download folder. Defaults to <project-root>/providers/landg.",
    )
    parser.add_argument(
        "--debug-columns",
        action="store_true",
        help="Print detected input columns and matched output columns.",
    )
    return parser.parse_args()


def project_root() -> Path:
    """
    Return the project root from wherever this file is placed.

    Works when this file is in:
      - providers/landg/extract_LandG_fields.py
      - scrapers/extract_LandG_fields.py
      - project root
    """
    here = Path(__file__).resolve().parent

    for candidate in [here, *here.parents]:
        if (candidate / "providers").is_dir() and (candidate / "scrapers").is_dir():
            return candidate

    # Fallback: if the script is in providers/landg, project root is 2 levels above.
    if here.name.lower() in {"landg", "l&g"} and here.parent.name.lower() == "providers":
        return here.parent.parent

    return here


def default_provider_dir() -> Path:
    root = project_root()

    # Your actual folder is providers/landg.
    landg = root / "providers" / "landg"
    if landg.exists():
        return landg

    # Fallback only if someone used providers/l&g instead.
    landg_alt = root / "providers" / "l&g"
    if landg_alt.exists():
        return landg_alt

    return landg


INPUT_DIR = default_provider_dir()
OUTPUT_DIR = INPUT_DIR


def latest_export(provider_dir: Path) -> Path:
    if not provider_dir.exists():
        raise FileNotFoundError(f"Provider folder not found: {provider_dir}")

    candidates = [
        p
        for p in provider_dir.rglob("landg_etf_export_*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No L&G export found under {provider_dir}. Expected landg_etf_export_*.xlsx/.xls/.csv"
        )

    # Prefer newest modified file; this also works when today's file has a different extension.
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_latest_download(provider_dir: Path) -> Path:
    return latest_export(provider_dir)


def extract_date_from_path(path: Path) -> str:
    # 1) file name: landg_etf_export_2026-06-24.xlsx
    match = DATE_RE.search(path.name)
    if match:
        return match.group(1)

    # 2) parent folder: providers/landg/2026-06-24/
    for part in reversed(path.parts):
        match = DATE_RE.fullmatch(part)
        if match:
            return match.group(1)

    # 3) fallback
    return date.today().isoformat()


def format_display_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value


# -----------------------------------------------------------------------------
# Input reading helpers
# -----------------------------------------------------------------------------

def clean_header(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if text.lower().startswith("unnamed:"):
        return ""
    return text


def normalize(value: object) -> str:
    text = clean_header(value).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def flatten_columns(columns: Iterable[object]) -> list[str]:
    result: list[str] = []
    for col in columns:
        if isinstance(col, tuple):
            parts = [clean_header(x) for x in col if clean_header(x)]
            result.append(" ".join(parts).strip())
        else:
            result.append(clean_header(col))
    return result


def count_isins(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    total = 0
    sample = df.head(500)
    for col in sample.columns:
        total += sample[col].astype(str).str.contains(ISIN_RE, na=False).sum()
    return int(total)


def tidy_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = flatten_columns(df.columns)
    df = df.loc[:, [bool(c) for c in df.columns]]
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.astype(object).where(pd.notna(df), "")
    return df


def read_csv_candidates(path: Path) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for header_row in range(0, 12):
        try:
            df = pd.read_csv(path, dtype=str, header=header_row, sep=None, engine="python")
            df = tidy_frame(df)
            if not df.empty:
                frames.append(df)
        except Exception:
            continue
    return frames


def read_excel_candidates(path: Path) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    xls = pd.ExcelFile(path)
    for sheet in xls.sheet_names:
        for header_row in range(0, 15):
            try:
                df = pd.read_excel(path, sheet_name=sheet, dtype=str, header=header_row)
                df = tidy_frame(df)
                if not df.empty:
                    # Store sheet/header information for debugging without changing output.
                    df.attrs["sheet"] = sheet
                    df.attrs["header_row"] = header_row
                    frames.append(df)
            except Exception:
                continue
    return frames


def read_best_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frames = read_csv_candidates(path)
    elif suffix in {".xlsx", ".xls"}:
        frames = read_excel_candidates(path)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")

    if not frames:
        raise RuntimeError(f"Could not read any table from {path}")

    # Best table = one with most ISIN-looking values, then most rows.
    best = max(frames, key=lambda df: (count_isins(df), len(df), len(df.columns)))
    if count_isins(best) == 0:
        raise RuntimeError(
            "The file was readable, but no ISIN-looking values were found. "
            "Run with --debug-columns and inspect the export format."
        )
    return best


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    return read_best_table(path).to_dict(orient="records")


# -----------------------------------------------------------------------------
# Column matching
# -----------------------------------------------------------------------------

COLUMN_HINTS: dict[str, list[str]] = {
    "name": [
        "etf name",
        "fund name",
        "product name",
        "share class name",
        "fund",
        "name",
    ],
    "isin": ["isin"],
    "ccy": [
        "ccy",
        "currency",
        "trading currency",
        "listing currency",
        "share class currency",
        "dealing currency",
        "base currency",
    ],
    "ter": [
        "ter",
        "ter %",
        "total expense ratio",
        "ongoing charge",
        "ongoing charges",
        "ongoing charges figure",
        "ocf",
        "annual charge",
        "management fee",
    ],
    "aum": [
        "aum",
        "aum m",
        "assets under management",
        "fund size",
        "fund assets",
        "net assets",
        "total assets",
        "total net assets",
    ],
}


def find_column(df: pd.DataFrame, field: str) -> str | None:
    normalized_cols = {col: normalize(col) for col in df.columns}
    hints = COLUMN_HINTS[field]

    # 1) exact normalized match
    for hint in hints:
        nh = normalize(hint)
        for col, ncol in normalized_cols.items():
            if ncol == nh:
                return col

    # 2) contains match, but avoid obvious wrong name columns
    for hint in hints:
        nh = normalize(hint)
        for col, ncol in normalized_cols.items():
            if nh and nh in ncol:
                if field == "name" and any(bad in ncol for bad in ["benchmark", "index", "manager"]):
                    continue
                if field == "aum" and any(bad in ncol for bad in ["nav per", "price", "market price"]):
                    continue
                return col

    # 3) ISIN fallback: pick the column with most ISIN-looking values
    if field == "isin":
        scores = {
            col: int(df[col].astype(str).str.contains(ISIN_RE, na=False).sum())
            for col in df.columns
        }
        best_col, best_score = max(scores.items(), key=lambda item: item[1])
        if best_score > 0:
            return best_col

    return None


# -----------------------------------------------------------------------------
# Value cleaning
# -----------------------------------------------------------------------------

def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "-", "--", "n/a", "na"}:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_isin(value: object | None) -> str:
    text = clean_text(value).upper().replace(" ", "")
    match = ISIN_RE.search(text)
    return match.group(0).upper() if match else ""


def clean_ccy(value: object | None) -> str:
    text = clean_text(value).upper()
    # Keep common ISO currency codes if they appear inside a longer string.
    for code in ["GBP", "GBX", "USD", "EUR", "CHF", "JPY", "AUD", "CAD", "SEK", "NOK", "DKK"]:
        if re.search(rf"\b{code}\b", text):
            return code
    # Otherwise keep a short cleaned token.
    text = re.sub(r"[^A-Z]", "", text)
    return text[:3] if len(text) >= 3 else text


def first_number(text: str) -> float | None:
    text = clean_text(text)
    if not text:
        return None

    # Handle accounting negative numbers: (1,234.5)
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    value = float(match.group(0))
    return -value if negative else value


def ter_to_bps(value: object | None) -> str:
    text = clean_text(value)
    number = first_number(text)
    if number is None:
        return ""

    lower = text.lower()
    if "%" in lower:
        bps = number * 100                       # 0.12% -> 12 bps
    elif abs(number) <= 0.05:
        bps = number * 10_000                    # Excel percent fraction: 0.0012 -> 12 bps
    elif abs(number) <= 5:
        bps = number * 100                       # Percent points: 0.12 -> 12 bps
    else:
        bps = number                             # Already bps

    return f"{bps:.2f}".rstrip("0").rstrip(".")


def aum_to_millions(value: object | None, column_name: str = "") -> str:
    text = clean_text(value)
    number = first_number(text)
    if number is None:
        return ""

    lower = f"{text} {column_name}".lower()

    if any(token in lower for token in ["bn", "billion"]):
        millions = number * 1_000
    elif any(token in lower for token in ["m", "million", "millions"]):
        millions = number
    elif any(token in lower for token in ["k", "thousand"]):
        millions = number / 1_000
    else:
        # If it looks like an absolute currency amount, convert to millions.
        # Otherwise assume the export already reports AUM in millions.
        millions = number / 1_000_000 if abs(number) >= 100_000 else number

    return f"{millions:.2f}".rstrip("0").rstrip(".")


# -----------------------------------------------------------------------------
# Extraction
# -----------------------------------------------------------------------------

def extract_fields(input_path: Path, debug_columns: bool = False) -> pd.DataFrame:
    raw = read_best_table(input_path)
    export_date = extract_date_from_path(input_path)

    cols = {
        "name": find_column(raw, "name"),
        "isin": find_column(raw, "isin"),
        "ccy": find_column(raw, "ccy"),
        "ter": find_column(raw, "ter"),
        "aum": find_column(raw, "aum"),
    }

    if debug_columns:
        print("\nDetected table:")
        if raw.attrs:
            print(f"  sheet      : {raw.attrs.get('sheet', '')}")
            print(f"  header row : {raw.attrs.get('header_row', '')}")
        print("\nInput columns:")
        for c in raw.columns:
            print(f"  - {c}")
        print("\nMatched columns:")
        for key, value in cols.items():
            print(f"  {key:>4}: {value}")
        print()

    if not cols["isin"]:
        raise RuntimeError("Could not detect the ISIN column.")

    out = pd.DataFrame()
    out["ETF Name"] = raw[cols["name"]].map(clean_text) if cols["name"] else ""
    out["Issuer"] = ISSUER
    out["ISIN"] = raw[cols["isin"]].map(clean_isin)
    out["CCY"] = raw[cols["ccy"]].map(clean_ccy) if cols["ccy"] else ""
    out["TER(bps)"] = raw[cols["ter"]].map(ter_to_bps) if cols["ter"] else ""

    if cols["aum"]:
        out["AUM(M)"] = raw[cols["aum"]].map(lambda v: aum_to_millions(v, cols["aum"] or ""))
    else:
        out["AUM(M)"] = ""

    out["Date"] = format_display_date(export_date)

    # Keep only valid ETF listing rows.
    out = out[out["ISIN"].astype(str).str.fullmatch(ISIN_RE, na=False)].copy()
    out = out[OUTPUT_COLUMNS]

    # Preserve multiple listings with the same ISIN/CCY if present, but remove exact duplicate rows.
    out = out.drop_duplicates().reset_index(drop=True)
    return out


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input = input_path or find_latest_download(INPUT_DIR)
    return extract_fields(Path(resolved_input)).to_dict(orient="records")


def main() -> None:
    args = parse_args()

    input_path = args.input
    if input_path is None:
        provider_dir = args.provider_dir or default_provider_dir()
        input_path = latest_export(provider_dir)

    input_path = input_path.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    export_date = extract_date_from_path(input_path)
    output_path = args.output or (input_path.parent / f"landg_selected_fields_{export_date}.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  L&G Fields Extractor")
    print("=" * 60)
    print(f"  Input : {input_path}")
    print(f"  Output: {output_path}")
    print("=" * 60)

    selected = extract_fields(input_path, debug_columns=args.debug_columns)
    selected.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"\n✅ Extracted {len(selected)} rows")
    print(f"📁 Saved: {output_path}")


if __name__ == "__main__":
    main()
