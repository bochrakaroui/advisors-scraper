"""Scrape selected Columbia Threadneedle ETF share classes from the official fund centre."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError as exc:  # pragma: no cover - runtime guidance for local usage
    raise ModuleNotFoundError(
        "beautifulsoup4 is required for the Columbia scraper. "
        "Install it with 'pip install beautifulsoup4'."
    ) from exc


ISSUER = "Columbia Threadneedle Investments"
BASE_URL = "https://www.columbiathreadneedle.com"
INDEX_URL = f"{BASE_URL}/en/gb/institutional/funds-and-prices/"
REQUEST_TIMEOUT_S = 60

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Columbia"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TARGET_ISINS = [
    "IE000953I6Z4",
    "IE000KXM3O48",
    "IE000M07S996",
    "IE000SA19OL0",
    "IE000SWYZ0D5",
    "IE000UL7AOT5",
]

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now()


def build_run_output_dir(base_dir: Path, run_date: str) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "columbia_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    return "" if cleaned in {"", "-", "--", "None", "null", "N/A"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def parse_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    try:
        return Decimal(str(cleaned))
    except (InvalidOperation, ValueError):
        return None


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def amount_to_millions(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def percent_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value * Decimal("10000"), places=2)


def fetch_json(url: str, *, headers: dict[str, str] | None = None, payload: object | None = None) -> Any:
    if payload is None:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT_S, headers=headers)
    else:
        response = SESSION.post(url, json=payload, timeout=REQUEST_TIMEOUT_S, headers=headers)
    response.raise_for_status()
    return response.json()


def discover_fund_center() -> dict[str, str]:
    response = SESSION.get(INDEX_URL, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    fund_center = soup.find("div", id="fundCenter")
    if fund_center is None:
        raise RuntimeError("Could not locate the Columbia fund centre configuration on the landing page.")

    api_url = clean_text(fund_center.get("data-api-url"))
    base_url = clean_text(fund_center.get("data-base-url"))
    configuration_key = clean_text(fund_center.get("data-configuration-key"))

    if not api_url or not configuration_key:
        raise RuntimeError("Columbia fund centre configuration is missing the API path or configuration key.")

    return {
        "api_url": urljoin(BASE_URL, api_url),
        "base_url": urljoin(BASE_URL, base_url) if base_url else "",
        "configuration_key": configuration_key,
    }


def fetch_app_token(api_url: str, configuration_key: str) -> str:
    config_url = f"{api_url.rstrip('/')}/services/getApplicationAppConfig/{configuration_key}"
    payload = fetch_json(config_url)
    token = clean_text(payload.get("applicationConfiguration", {}).get("token"))
    if not token:
        raise RuntimeError("Columbia app config did not return a public API token.")
    return token


def get_property_value(row: dict[str, Any], property_code: str) -> Any:
    return row.get("properties_pub", {}).get(property_code, {}).get("value")


def get_stat_values(row: dict[str, Any], statistic_code: str) -> list[dict[str, Any]]:
    for statistic in row.get("statistics", []):
        if statistic.get("code") == statistic_code:
            values = statistic.get("values", [])
            return values if isinstance(values, list) else []
    return []


def get_first_stat_value(row: dict[str, Any], statistic_code: str) -> dict[str, Any]:
    values = get_stat_values(row, statistic_code)
    return values[0] if values else {}


def extract_ter(row: dict[str, Any]) -> tuple[str, str, str]:
    for item in get_stat_values(row, "fund_charges"):
        if clean_text(item.get("label")) != "Total Expense Ratio (TER)":
            continue
        value = item.get("value_ex_ante")
        date_value = clean_text(item.get("effective_date") or item.get("charges_as_at_date"))
        decimal_value = parse_decimal(value)
        if decimal_value is None:
            return "", "", date_value
        ter = format_decimal(decimal_value * Decimal("100"), places=2)
        ter_bps = percent_to_bps(decimal_value)
        return ter, ter_bps, date_value
    return "", "", ""


def build_listing_row(isin: str, raw_row: dict[str, Any] | None) -> dict[str, Any]:
    if raw_row is None:
        return {
            "issuer": ISSUER,
            "isin": isin,
            "etf_name": "",
            "fund_name": "",
            "share_class_name": "",
            "share_class_code": "",
            "share_class_currency": "",
            "fund_base_currency": "",
            "product_type": "",
            "asset_type": "",
            "benchmark_name": "",
            "distribution_type": "",
            "fund_manager": [],
            "investment_management_company": "",
            "fund_domicile": "",
            "fund_launch_date": "",
            "share_class_launch_date": "",
            "replication_method": "",
            "sfdr_category": "",
            "nav": "",
            "nav_date": "",
            "market_price": "",
            "market_price_date": "",
            "total_assets": "",
            "aum_mn": "",
            "aum_ccy": "",
            "total_assets_date": "",
            "ter": "",
            "ter_bps": "",
            "ter_date": "",
            "listing_details": [],
            "fetch_status": "not_found",
            "raw_api_row": {},
        }

    daily_prices = get_first_stat_value(raw_row, "daily_prices")
    fund_size_nav = get_first_stat_value(raw_row, "fund_size_nav")
    ter, ter_bps, ter_date = extract_ter(raw_row)

    total_assets_value = fund_size_nav.get("fund_size_nav_fund_base")
    aum_ccy = clean_text(
        fund_size_nav.get("share_class_currency")
        or get_property_value(raw_row, "fund_base_currency")
        or get_property_value(raw_row, "share_class_currency")
    )

    return {
        "issuer": ISSUER,
        "isin": normalize_isin(get_property_value(raw_row, "isin") or raw_row.get("clientCode") or isin),
        "etf_name": clean_text(get_property_value(raw_row, "share_class_name") or get_property_value(raw_row, "fund_name")),
        "fund_name": clean_text(get_property_value(raw_row, "fund_name")),
        "share_class_name": clean_text(get_property_value(raw_row, "share_class_name")),
        "share_class_code": clean_text(get_property_value(raw_row, "share_class_code")),
        "share_class_currency": clean_text(get_property_value(raw_row, "share_class_currency")),
        "fund_base_currency": clean_text(get_property_value(raw_row, "fund_base_currency")),
        "product_type": clean_text(get_property_value(raw_row, "product_type")),
        "asset_type": clean_text(get_property_value(raw_row, "asset_type")),
        "benchmark_name": clean_text(get_property_value(raw_row, "benchmark_name")),
        "distribution_type": clean_text(get_property_value(raw_row, "distribution_type")),
        "fund_manager": get_property_value(raw_row, "fund_manager") or [],
        "investment_management_company": clean_text(get_property_value(raw_row, "investment_management_company")),
        "fund_domicile": clean_text(get_property_value(raw_row, "fund_domicile")),
        "fund_launch_date": clean_text(get_property_value(raw_row, "fund_launch_date")),
        "share_class_launch_date": clean_text(get_property_value(raw_row, "share_class_launch_date")),
        "replication_method": clean_text(get_property_value(raw_row, "replication_method")),
        "sfdr_category": clean_text(get_property_value(raw_row, "sfdr_category")),
        "nav": clean_text(daily_prices.get("nav_price")),
        "nav_date": clean_text(daily_prices.get("price_date")),
        "market_price": clean_text(daily_prices.get("market_price")),
        "market_price_date": clean_text(daily_prices.get("market_price_date")),
        "total_assets": clean_text(total_assets_value),
        "aum_mn": amount_to_millions(total_assets_value),
        "aum_ccy": aum_ccy,
        "total_assets_date": clean_text(fund_size_nav.get("fund_size_date") or fund_size_nav.get("effective_date")),
        "ter": ter,
        "ter_bps": ter_bps,
        "ter_date": ter_date,
        "listing_details": get_stat_values(raw_row, "listing_details"),
        "fetch_status": "ok",
        "raw_api_row": raw_row,
    }


def fetch_target_rows(api_url: str, token: str) -> dict[str, dict[str, Any]]:
    search_url = f"{api_url.rstrip('/')}/services/fund/searchEntity"
    payload = {
        "type": "CLSS",
        "search": [{"property": "isin", "values": TARGET_ISINS, "matchtype": "MATCH"}],
        "limit": 50,
        "preserveOriginal": True,
        "translate": False,
        "applyFormats": False,
        "include": {"statistics": {}},
    }
    headers = {"X-KSYS-TOKEN": token}
    response = fetch_json(search_url, headers=headers, payload=payload)
    rows = response.get("values", [])
    return {
        normalize_isin(get_property_value(row, "isin") or row.get("clientCode")): row
        for row in rows
        if isinstance(row, dict)
    }


def build_snapshot() -> dict[str, Any]:
    fund_center = discover_fund_center()
    token = fetch_app_token(fund_center["api_url"], fund_center["configuration_key"])
    rows_by_isin = fetch_target_rows(fund_center["api_url"], token)

    listing_rows = [build_listing_row(isin, rows_by_isin.get(isin)) for isin in TARGET_ISINS]
    status_counts: dict[str, int] = {}
    for row in listing_rows:
        status = clean_text(row.get("fetch_status")) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    matched = [row["isin"] for row in listing_rows if row["fetch_status"] == "ok"]
    missing = [row["isin"] for row in listing_rows if row["fetch_status"] != "ok"]

    return {
        "source": {
            "provider": ISSUER,
            "landing_page_url": INDEX_URL,
            "fund_center_api_url": fund_center["api_url"],
            "service_base_url": fund_center["base_url"],
            "configuration_key": fund_center["configuration_key"],
        },
        "method": (
            "Official Columbia Threadneedle funds-and-prices page discovery of the live Kurtosys fund-centre "
            "configuration, followed by authenticated read-only /fund/searchEntity API extraction for the "
            "explicit target ISIN list with full statistics blocks preserved."
        ),
        "captured_at": timestamp_now().isoformat(),
        "target_isin_count": len(TARGET_ISINS),
        "matched_target_isins": matched,
        "missing_target_isins": missing,
        "status_counts": status_counts,
        "listing_rows": listing_rows,
    }


def download_snapshot(output_path: Path) -> Path:
    snapshot = build_snapshot()
    write_json(output_path, snapshot)
    logging.info("Columbia fetch summary: %s", snapshot.get("status_counts", {}))
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved : %s", output_path)
    return output_path


async def download_columbia_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def main() -> None:
    setup_logging()
    output_path = asyncio.run(download_columbia_file())
    print(f"Saved Columbia snapshot to: {output_path}")


if __name__ == "__main__":
    main()
