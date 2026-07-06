"""Extract selected Goldman Sachs ETF fields from the latest snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR
LEGACY_INPUT_DIR = BASE_DIR.parents[1] / "providers" / "Goldman Sachs"
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
    "Date",
]

FUNDS_SERVICE_URL = "https://am.gs.com/services/funds"
REQUEST_TIMEOUT_S = 45
TER_QUERY = """
query getFundsDetail($fundDetailRequest: FundDetailRequest) {
  fundsDetail(fundDetailRequest: $fundDetailRequest) {
    feeAndExpenseAsOfDate
    feesAndExpenses {
      label
      asAtDate
      value
      suffix
    }
  }
}
""".strip()
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Content-Type": "application/json",
    "Referer": (
        "https://am.gs.com/en-gb/institutions/funds"
        "?locale=en-gb&audience=institutions&eft=true&sf=funds&filters=funds%7CETF"
    ),
}

SPACE_PATTERN = re.compile(r"\s+")
TER_LABEL_PRIORITY = ("netExpensesRatio", "managementFees")
JUSTETF_FALLBACK_ISINS = [
    "IE00BJ5CMD00",
    "IE0003MKK4H3",
    "IE000HPBRE54",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract ETF Name, Issuer, ISIN, CCY, TER(bps), AUM(M), and Date "
            "from a Goldman Sachs ETF snapshot."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Goldman Sachs snapshot JSON path. Defaults to the latest goldmansachs_etf_export.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output path. Defaults to the same folder as the source goldmansachs_etf_export.json file.",
    )
    parser.add_argument(
        "--no-live-ter",
        action="store_true",
        help="Skip live TER lookups and only use TER values already present in the snapshot.",
    )
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


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
            for path in search_dir.rglob("goldmansachs_etf_export.json")
            if search_dir.exists() and path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        searched_locations = ", ".join(str(path) for path in search_dirs)
        raise FileNotFoundError(
            f"No goldmansachs_etf_export.json files found in {searched_locations}"
        )
    return candidates[0]


def build_output_path(input_path: Path) -> Path:
    return input_path.parent / "goldmansachs_selected_fields.csv"


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
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.strftime("%d/%m/%Y")


def parse_snapshot(path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    if not isinstance(rows, list):
        raise ValueError(
            f"Unexpected Goldman Sachs snapshot in {path}: expected listing_rows to be a list."
        )
    return parse_iso_date(payload.get("captured_at")), rows


def to_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value).replace(",", "").replace("%", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def percent_to_bps(value: object | None) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return ""
    bps = decimal_value * Decimal("100")
    quantized = bps.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return str(int(quantized))


def build_ter_lookup_payload(pv_number: str, share_class_id: str) -> dict[str, Any]:
    return {
        "operationName": "getFundsDetail",
        "variables": {
            "fundDetailRequest": {
                "country": "gb",
                "language": "en",
                "audience": "institutions",
                "pvNumber": pv_number,
                "shareClassId": share_class_id,
            }
        },
        "query": TER_QUERY,
    }


def extract_ter_bps_from_detail(detail_payload: dict[str, Any]) -> str:
    entries = detail_payload.get("feesAndExpenses")
    if not isinstance(entries, list):
        return ""

    by_label: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = clean_text(entry.get("label"))
        if label:
            by_label[label] = entry

    for label in TER_LABEL_PRIORITY:
        if label in by_label:
            return percent_to_bps(by_label[label].get("value"))
    return ""


def fetch_live_ter_bps(
    session: requests.Session,
    pv_number: str,
    share_class_id: str,
) -> str:
    if not pv_number or not share_class_id:
        return ""

    response = session.post(
        FUNDS_SERVICE_URL,
        json=build_ter_lookup_payload(pv_number, share_class_id),
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()
    if "errors" in payload:
        raise RuntimeError(f"Goldman Sachs funds service returned errors: {payload['errors']}")

    detail_payload = payload.get("data", {}).get("fundsDetail")
    if not isinstance(detail_payload, dict):
        return ""
    return extract_ter_bps_from_detail(detail_payload)


def build_live_ter_map(
    source_rows: list[dict[str, object]],
    use_live_ter: bool,
) -> dict[tuple[str, str], str]:
    ter_map: dict[tuple[str, str], str] = {}
    if not use_live_ter:
        return ter_map

    keys_to_fetch: list[tuple[str, str]] = []
    for row in source_rows:
        pv_number = clean_text(row.get("pv_number"))
        isin = normalize_isin(row.get("isin"))
        if pv_number and isin and (pv_number, isin) not in ter_map:
            keys_to_fetch.append((pv_number, isin))
            ter_map[(pv_number, isin)] = ""

    if not keys_to_fetch:
        return ter_map

    session = requests.Session()
    session.headers.update(HEADERS)

    logging.info("Fetching live Goldman Sachs TER values for %d share classes", len(keys_to_fetch))
    for index, (pv_number, isin) in enumerate(keys_to_fetch, start=1):
        try:
            ter_map[(pv_number, isin)] = fetch_live_ter_bps(session, pv_number, isin)
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "TER lookup failed for PV %s / ISIN %s (%d/%d): %s",
                pv_number,
                isin,
                index,
                len(keys_to_fetch),
                exc,
            )
    return ter_map


def transform_row(
    source_row: dict[str, object],
    scrape_date: str,
    ter_map: dict[tuple[str, str], str],
) -> dict[str, str]:
    pv_number = clean_text(source_row.get("pv_number"))
    isin = normalize_isin(source_row.get("isin"))
    ter_bps = clean_text(source_row.get("ter_bps")) or ter_map.get((pv_number, isin), "")
    row_date = scrape_date or parse_iso_date(source_row.get("aum_date")) or parse_iso_date(source_row.get("nav_date"))

    return {
        "ETF Name": clean_text(source_row.get("etf_name")),
        "Issuer": clean_text(source_row.get("issuer")),
        "ISIN": isin,
        "CCY": clean_text(source_row.get("ccy")).upper(),
        "TER(bps)": ter_bps,
        "AUM(M)": clean_text(source_row.get("aum_mn")),
        "Date": row_date,
    }


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(column, "") for column in OUTPUT_COLUMNS)
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
            logging.warning("justETF fallback failed for Goldman Sachs %s: %s", isin, exc)
            continue

        if clean_text(profile.get("fetch_status")) not in {"", "ok"}:
            logging.warning(
                "justETF fallback did not resolve Goldman Sachs %s: %s",
                isin,
                clean_text(profile.get("error")),
            )
            continue

        supplemented_rows.append(
            {
                "ETF Name": clean_text(profile.get("etf_name")),
                "Issuer": "Goldman Sachs Asset Management",
                "ISIN": normalize_isin(profile.get("isin")) or isin,
                "CCY": clean_text(profile.get("ccy")).upper(),
                "TER(bps)": clean_text(profile.get("ter_bps")),
                "AUM(M)": clean_text(profile.get("aum_mn")),
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


def extract_rows(
    input_path: Path | None = None,
    use_live_ter: bool = True,
) -> list[dict[str, str]]:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    captured_at, source_rows = parse_snapshot(resolved_input_path)
    scrape_date = captured_at or extract_file_date(resolved_input_path)
    ter_map = build_live_ter_map(source_rows, use_live_ter=use_live_ter)
    output_rows = [transform_row(row, scrape_date, ter_map) for row in source_rows]
    return supplement_missing_rows(output_rows, scrape_date)


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def process_file(
    input_path: Path | None = None,
    output_path: Path | None = None,
    use_live_ter: bool = True,
) -> Path:
    resolved_input_path = input_path.resolve() if input_path else find_latest_download(INPUT_DIR)
    resolved_output_path = output_path.resolve() if output_path else build_output_path(resolved_input_path)

    output_rows = extract_rows(resolved_input_path, use_live_ter=use_live_ter)
    write_csv(resolved_output_path, output_rows)

    print(f"Source file : {resolved_input_path}")
    print(f"Rows written: {len(output_rows):,}")
    print(f"Unique ISINs: {len({row['ISIN'] for row in output_rows if row.get('ISIN')}):,}")
    print(f"Missing ISIN : {sum(1 for row in output_rows if not clean_text(row.get('ISIN'))):,}")
    print(f"Missing CCY  : {sum(1 for row in output_rows if not clean_text(row.get('CCY'))):,}")
    print(f"Missing TER  : {sum(1 for row in output_rows if not clean_text(row.get('TER(bps)'))):,}")
    print(f"Missing AUM  : {sum(1 for row in output_rows if not clean_text(row.get('AUM(M)'))):,}")
    print(f"Output file : {resolved_output_path}")
    return resolved_output_path


def main() -> None:
    setup_logging()
    args = parse_args()
    process_file(args.input, args.output, use_live_ter=not args.no_live_ter)


if __name__ == "__main__":
    main()
