"""Extract the selected ETF fields from the latest downloaded J.P. Morgan snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
OUTPUT_DIR = BASE_DIR
ISSUER = "J.P. Morgan Asset Management"
REPO_ROOT = BASE_DIR.parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from providers.output_schema import OUTPUT_COLUMNS

try:
    from scrapers.justetf_profile import build_session as build_justetf_session
    from scrapers.justetf_profile import fetch_profile as fetch_justetf_profile
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    build_justetf_session = None  # type: ignore[assignment]
    fetch_justetf_profile = None  # type: ignore[assignment]


GREEN_BOND_REFERENCE_ISIN = "IE0005FKEK99"
GREEN_BOND_SHARE_CLASSES = {
    "IE0005FKEK99": "JPMorgan Green Social Sustainable Bond Active UCITS ETF USD (acc)",
    "IE000HZSZFP6": "JPMorgan Green Social Sustainable Bond Active UCITS ETF USD (dist)",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the selected ETF fields from a downloaded J.P. Morgan .json snapshot."
    )
    parser.add_argument("--input", type=Path, help="Downloaded J.P. Morgan .json snapshot. Defaults to the latest file.")
    parser.add_argument("--output", type=Path, help="Processed CSV path. Defaults to a dated folder inside ./jpmorgan.")
    return parser.parse_args()


def find_latest_download(input_dir: Path) -> Path:
    candidates = sorted(
        (path for path in input_dir.rglob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No .json files found in {input_dir}")
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "jpmorgan_selected_fields.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected J.P. Morgan snapshot in {path}: expected a list.")
    return payload


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def is_etf_row(row: dict[str, object]) -> bool:
    return clean_text(row.get("categoryCode")) == "ETF" or clean_text(row.get("fundTypeCode")) == "N_ETF"


def format_ter_bps(value: object | None) -> str:
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        try:
            return format_decimal(Decimal(str(value)) * Decimal("100"), places=2)
        except InvalidOperation:
            return ""

    cleaned = clean_text(value).replace("%", "").strip()
    if not cleaned:
        return ""

    if cleaned.lower().endswith("bps"):
        cleaned = cleaned[:-3].strip()
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return format_decimal(Decimal(cleaned), places=2)
        except InvalidOperation:
            return ""

    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return format_decimal(Decimal(cleaned) * Decimal("100"), places=2)
    except InvalidOperation:
        return ""


def normalize_aum_millions(value: object | None) -> str:
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        try:
            return format_decimal(Decimal(str(value)) / Decimal("1000000"), places=2)
        except InvalidOperation:
            return ""

    cleaned = clean_text(value)
    if not cleaned:
        return ""

    cleaned = cleaned.replace("\u00a3", "").replace("$", "").replace("â‚¬", "")
    cleaned = re.sub(r"\b[A-Z]{3}\b", "", cleaned).strip()
    compact = cleaned.lower().replace(" ", "").replace(",", "")

    multiplier = Decimal("0.000001")
    if compact.endswith("bn"):
        multiplier = Decimal("1000")
        compact = compact[:-2]
    elif compact.endswith("b"):
        multiplier = Decimal("1000")
        compact = compact[:-1]
    elif compact.endswith("million"):
        multiplier = Decimal("1")
        compact = compact[:-7]
    elif compact.endswith("mn"):
        multiplier = Decimal("1")
        compact = compact[:-2]
    elif compact.endswith("m"):
        multiplier = Decimal("1")
        compact = compact[:-1]

    if "," in compact and "." not in compact:
        compact = compact.replace(",", ".")

    try:
        amount = Decimal(compact)
    except InvalidOperation:
        return ""

    return format_decimal(amount * multiplier, places=2)


def format_source_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def transform_row(source_row: dict[str, object]) -> dict[str, str]:
    aum_m = normalize_aum_millions(source_row.get("assetsUnderManagement"))
    aum_ccy = clean_text(
        source_row.get("fundValuationCurrency")
        or source_row.get("currencyCode")
        or source_row.get("shareclassCurrencyCode")
    ).upper()
    if not aum_m:
        aum_ccy = ""

    return {
        "ETF Name": clean_text(source_row.get("shareclassName")) or clean_text(source_row.get("displayName")),
        "Issuer": ISSUER,
        "ISIN": clean_text(source_row.get("identifier")).upper(),
        "CCY": clean_text(source_row.get("shareclassCurrencyCode") or source_row.get("currencyCode")).upper(),
        "TER(bps)": format_ter_bps(source_row.get("ongoingCharge")),
        "AUM(M)": aum_m,
        "AUM CCY": aum_ccy,
        "Date": format_source_date(source_row.get("fundValuationDate") or source_row.get("navDate")),
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


def supplement_missing_green_bond_share_classes(
    rows: list[dict[str, str]],
    input_path: Path,
) -> list[dict[str, str]]:
    present_isins = {clean_text(row.get("ISIN")).upper() for row in rows if clean_text(row.get("ISIN"))}
    missing_isins = [isin for isin in GREEN_BOND_SHARE_CLASSES if isin not in present_isins]
    if not missing_isins or build_justetf_session is None or fetch_justetf_profile is None:
        return rows

    try:
        profile = fetch_justetf_profile(GREEN_BOND_REFERENCE_ISIN, session=build_justetf_session())
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: J.P. Morgan Green Bond AUM fallback failed: {exc}")
        return rows

    if clean_text(profile.get("fetch_status")) not in {"", "ok"}:
        return rows

    aum_m = clean_text(profile.get("aum_mn"))
    try:
        has_positive_aum = Decimal(aum_m.replace(",", "")) > 0
    except (InvalidOperation, ValueError):
        has_positive_aum = False
    if not has_positive_aum:
        return rows

    aum_ccy = clean_text(profile.get("aum_ccy")).upper()
    source_date = format_source_date(input_path.parent.name)
    supplemented_rows = list(rows)
    for isin in missing_isins:
        supplemented_rows.append(
            {
                "ETF Name": GREEN_BOND_SHARE_CLASSES[isin],
                "Issuer": ISSUER,
                "ISIN": isin,
                "CCY": "USD",
                "TER(bps)": "32.00",
                "AUM(M)": aum_m,
                "AUM CCY": aum_ccy,
                "Date": source_date,
            }
        )
    return supplemented_rows


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def extract_rows(input_path: Path | None = None) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    rows = parse_snapshot_rows(resolved_input_path)
    output_rows = [transform_row(row) for row in rows if is_etf_row(row)]
    output_rows = dedupe_rows_by_isin(output_rows)
    if resolved_input_path.exists():
        output_rows = supplement_missing_green_bond_share_classes(output_rows, resolved_input_path)
    return dedupe_rows_by_isin(output_rows)


def process_file(input_path: Path | None = None, output_path: Path | None = None) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path)
    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Output file : {resolved_output_path}")
    return resolved_output_path


def main() -> None:
    args = parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    main()
