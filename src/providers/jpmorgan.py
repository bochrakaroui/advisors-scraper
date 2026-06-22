"""Collect J.P. Morgan Asset Management UK ETF listings from the official fund explorer."""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import requests


PAGE_URL = "https://am.jpmorgan.com/gb/en/asset-management/per/products/fund-explorer/etf"
API_URL = (
    "https://am.jpmorgan.com/FundsMarketingHandler/fund-explorer"
    "?country=gb&role=per&userLoggedIn=false&language=en&fundType=etf"
)
ISSUER = "J.P. Morgan Asset Management"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "jpmorgan"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

OUTPUT_COLUMNS = ["etf_listing_name", "issuer", "isin", "ccy", "ter", "aum_m"]
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": PAGE_URL,
    "Accept": "application/json, text/plain, */*",
}


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def timestamp_now() -> datetime:
    return datetime.now()


def build_raw_path(now: datetime) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    return RAW_DIR / f"jpmorgan_etfs_raw_{now.strftime('%Y%m%d_%H%M%S')}.json"


def build_processed_path(now: datetime) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    primary = PROCESSED_DIR / f"jpmorgan_etfs_{now.strftime('%Y%m%d')}.csv"
    if not primary.exists():
        return primary
    return PROCESSED_DIR / f"jpmorgan_etfs_{now.strftime('%Y%m%d_%H%M%S')}.csv"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def is_valid_isin(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", value))


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    text = format(quantized, f".{places}f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def normalize_ter(value: object | None) -> str:
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        try:
            return format_decimal(Decimal(str(value)), places=4)
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
            return format_decimal(Decimal(cleaned) / Decimal("100"), places=4)
        except InvalidOperation:
            return ""

    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return format_decimal(Decimal(cleaned), places=4)
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

    cleaned = cleaned.replace("\u00a3", "").replace("$", "").replace("€", "")
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


def fetch_listing_payload() -> list[dict[str, object]]:
    response = requests.get(API_URL, headers=REQUEST_HEADERS, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Unexpected J.P. Morgan API payload: expected a list of ETF rows.")
    return payload


def save_raw_payload(path: Path, payload: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def is_etf_row(row: dict[str, object]) -> bool:
    return clean_text(row.get("categoryCode")) == "ETF" or clean_text(row.get("fundTypeCode")) == "N_ETF"


def transform_row(row: dict[str, object]) -> dict[str, str]:
    return {
        "etf_listing_name": clean_text(row.get("shareclassName")) or clean_text(row.get("displayName")),
        "issuer": ISSUER,
        "isin": clean_text(row.get("identifier")).upper(),
        "ccy": clean_text(row.get("shareclassCurrencyCode") or row.get("currencyCode")).upper(),
        "ter": normalize_ter(row.get("ongoingCharge")),
        "aum_m": normalize_aum_millions(row.get("assetsUnderManagement")),
    }


def validate_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    valid_rows: list[dict[str, str]] = []

    for row in rows:
        if not row["etf_listing_name"]:
            logging.warning("Skipping row with empty etf_listing_name")
            continue
        if row["issuer"] != ISSUER:
            logging.warning("Skipping %s because issuer is invalid: %r", row["etf_listing_name"], row["issuer"])
            continue
        if not is_valid_isin(row["isin"]):
            logging.warning("Skipping %s because ISIN is invalid: %r", row["etf_listing_name"], row["isin"])
            continue
        if row["ter"]:
            try:
                Decimal(row["ter"])
            except InvalidOperation:
                logging.warning("Clearing non-numeric TER for %s: %r", row["etf_listing_name"], row["ter"])
                row["ter"] = ""
        if row["aum_m"]:
            try:
                Decimal(row["aum_m"])
            except InvalidOperation:
                logging.warning("Clearing non-numeric AUM for %s: %r", row["etf_listing_name"], row["aum_m"])
                row["aum_m"] = ""

        valid_rows.append(row)

    return valid_rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def log_summary(rows: list[dict[str, str]], output_path: Path) -> None:
    valid_isins = sum(1 for row in rows if is_valid_isin(row["isin"]))
    missing_ccy = sum(1 for row in rows if not row["ccy"])
    missing_ter = sum(1 for row in rows if not row["ter"])
    missing_aum = sum(1 for row in rows if not row["aum_m"])

    logging.info("Official URL used: %s", PAGE_URL)
    logging.info("Data method used: official API/JSON endpoint")
    logging.info("ETF rows extracted: %s", len(rows))
    logging.info("Valid ISINs: %s", valid_isins)
    logging.info("Missing CCY values: %s", missing_ccy)
    logging.info("Missing TER values: %s", missing_ter)
    logging.info("Missing AUM values: %s", missing_aum)
    logging.info("Final CSV path: %s", output_path)


def main() -> None:
    setup_logging()
    now = timestamp_now()
    raw_path = build_raw_path(now)
    processed_path = build_processed_path(now)

    payload = fetch_listing_payload()
    save_raw_payload(raw_path, payload)

    etf_rows = [row for row in payload if is_etf_row(row)]
    output_rows = validate_rows([transform_row(row) for row in etf_rows])
    write_csv(processed_path, output_rows)

    logging.info("Raw JSON saved: %s", raw_path)
    log_summary(output_rows, processed_path)


if __name__ == "__main__":
    main()
