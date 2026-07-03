"""Download BNP Paribas Asset Management UK ETF data by explicit ISIN list.

Workflow:
  1. Iterate the target UK BNP Paribas ETF ISINs.
  2. Fetch the official BNP Paribas fundsheet JSON for each ISIN.
  3. Save one provider-specific raw snapshot JSON with extracted listing rows.

Output: providers/BNP_Paribas_Asset_Management/<YYYY-MM-DD>/bnpparibas_etf_export.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests


ISSUER = "BNP Paribas Asset Management"
PROFILE_CODE = "IP_UK-FSE"
LANGUAGE_CODE = "ENG"
COUNTRY_CODE = "GBR"
PUBLIC_FACTSHEET_URL_TEMPLATE = "https://www.bnpparibas-am.com/en-gb/fundsheet/{fundsheet_uri}/?tab=overview"
FUNDSHEET_API_URL_TEMPLATE = (
    "https://api.bnpparibas-am.com/push/fundsheet/"
    "{profile}/{language}/{country}/{isin}"
)
REQUEST_DELAY_S = 0.15
REQUEST_TIMEOUT_S = 45
API_CONTEXTS = [
    (PROFILE_CODE, LANGUAGE_CODE, COUNTRY_CODE),
    ("IP_LU-FSE", "ENG", "GBR"),
    ("IP_GLB-FSE", "ENG", "GBR"),
    ("IP_LU-FSE", "ENG", "LUX"),
    ("IP_FR-FSE", "ENG", "GBR"),
]

TARGET_ISINS = [
    "IE0000VX9GN7",
    "IE000130VPV5",
    "IE000629MKR4",
    "IE0006O3TTP9",
    "IE0007YP0PL1",
    "IE0008FB2WZ1",
    "IE0009WYJCP8",
    "IE000ALI2E45",
    "IE000V3AQGW3",
    "IE000WQ5O293",
    "IE000YARBD10",
    "IE000ZME9TM4",
    "LU1291097779",
    "LU1291098827",
    "LU1291099718",
    "LU1291104575",
    "LU1291106356",
    "LU1377382368",
    "LU1547515053",
    "LU1615092217",
    "LU2314312922",
    "LU2533812058",
    "LU2533813023",
    "LU2533813296",
    "LU2616774076",
    "LU2697596745",
    "LU2697597552",
    "LU2742532828",
    "LU2742533636",
    "LU2800573128",
    "LU2823895847",
    "LU2868144093",
    "LU2993390504",
    "LU2993394241",
    "LU2993397939",
    "LU2993401392",
    "LU2993402101",
    "LU3025345516",
    "LU3047998896",
    "LU3125583602",
]

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "BNP_Paribas_Asset_Management"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SPACE_PATTERN = re.compile(r"\s+")
INVISIBLE_ISIN_CHARACTERS = ("\u00A0", "\u2007", "\u202F", "\u200B", "\uFEFF")


def build_run_output_dir(base_dir: Path, run_date: str) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now()


def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "bnpparibas_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "")
    for invisible_character in INVISIBLE_ISIN_CHARACTERS:
        cleaned = cleaned.replace(invisible_character, "")
    cleaned = SPACE_PATTERN.sub(" ", cleaned).strip()
    return "" if cleaned in {"", "-", "--", "- ", " -"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def to_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def amount_to_millions(value: object | None) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def percentage_to_bps(value: object | None) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value * Decimal("100"), places=2)


def decimal_to_string(value: object | None, places: int = 4) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return clean_text(value)
    return format_decimal(decimal_value, places=places)


def get_dict(value: object | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def get_list(value: object | None) -> list[Any]:
    return value if isinstance(value, list) else []


def join_text_list(values: object | None) -> str:
    if not isinstance(values, list):
        return ""
    cleaned_values = [clean_text(value) for value in values if clean_text(value)]
    return "; ".join(cleaned_values)


def build_api_url(isin: str) -> str:
    return build_api_url_for_context(isin, PROFILE_CODE, LANGUAGE_CODE, COUNTRY_CODE)


def build_api_url_for_context(isin: str, profile: str, language: str, country: str) -> str:
    return FUNDSHEET_API_URL_TEMPLATE.format(
        profile=profile,
        language=language,
        country=country,
        isin=isin.lower(),
    )


def build_empty_row(isin: str) -> dict[str, str]:
    return {
        "etf_name": "",
        "issuer": ISSUER,
        "isin": isin,
        "ccy": "",
        "ter_bps": "",
        "aum_mn": "",
        "share_class_aum_mn": "",
        "nav": "",
        "nav_date": "",
        "launch_date": "",
        "creation_date": "",
        "benchmark": "",
        "asset_class": "",
        "sub_asset_class": "",
        "region": "",
        "domicile": "",
        "legal_form": "",
        "management_company": "",
        "fund_type": "",
        "share_class_name": "",
        "share_type": "",
        "distribution_policy": "",
        "bloomberg_code": "",
        "reuters_code": "",
        "inav_bloomberg": "",
        "inav_reuters": "",
        "wkn_code": "",
        "morningstar_rating": "",
        "morningstar_rating_date": "",
        "sfdr_article": "",
        "amf_category": "",
        "risk_indicator": "",
        "recommended_horizon_years": "",
        "registration_countries": "",
        "fund_size_date": "",
        "ter_date": "",
        "reference_date": "",
        "factsheet_url": "",
        "api_url": build_api_url(isin),
        "api_profile": PROFILE_CODE,
        "api_language": LANGUAGE_CODE,
        "api_country": COUNTRY_CODE,
        "objective": "",
        "fetch_status": "pending",
    }


def find_share_entry(payload: dict[str, Any], isin: str) -> dict[str, Any]:
    shares = get_list(get_dict(payload.get("fundshare_selection")).get("shares"))
    for share in shares:
        share_dict = get_dict(share)
        if normalize_isin(share_dict.get("isin_code")) == isin:
            return share_dict
    return {}


def extract_latest_nav(payload: dict[str, Any], currency_code: str) -> tuple[str, str]:
    two_latest_nav = get_dict(get_dict(payload.get("nav")).get("two_latest_nav"))
    nav_items = get_list(two_latest_nav.get(currency_code))
    if not nav_items:
        for value in two_latest_nav.values():
            nav_items = get_list(value)
            if nav_items:
                break

    if not nav_items:
        return "", ""

    latest_nav = get_dict(nav_items[0])
    return decimal_to_string(latest_nav.get("nav"), places=4), clean_text(latest_nav.get("date"))


def extract_share_class_aum(payload: dict[str, Any], currency_code: str) -> str:
    nav_info = get_dict(get_dict(get_dict(payload.get("nav")).get("nav_info")).get(currency_code))
    if not nav_info:
        nav_info_root = get_dict(get_dict(payload.get("nav")).get("nav_info"))
        for value in nav_info_root.values():
            nav_info = get_dict(value)
            if nav_info:
                break
    return amount_to_millions(nav_info.get("share_size"))


def extract_public_factsheet_url(payload: dict[str, Any]) -> str:
    fundsheet_uri = clean_text(payload.get("fundsheet_uri")).strip("/")
    if not fundsheet_uri:
        return ""
    return PUBLIC_FACTSHEET_URL_TEMPLATE.format(fundsheet_uri=fundsheet_uri)


def extract_row_from_payload(payload: dict[str, Any], isin: str) -> dict[str, str]:
    row = build_empty_row(isin)

    header = get_dict(payload.get("header"))
    classification = get_dict(payload.get("classification"))
    fundshare_selection = get_dict(payload.get("fundshare_selection"))
    overview = get_dict(payload.get("overview"))
    overview_key_numbers = get_dict(overview.get("key_numbers"))
    portfolio = get_dict(payload.get("portfolio"))
    fees_timed = get_dict(get_dict(payload.get("fees")).get("fees_timed"))
    risk = get_dict(payload.get("risk"))
    sfdr = get_dict(payload.get("sfdr"))
    share_entry = find_share_entry(payload, isin)
    selected_currency = clean_text(
        fundshare_selection.get("currency")
        or portfolio.get("base_currency_code")
        or fundshare_selection.get("currencies", [""])[0] if isinstance(fundshare_selection.get("currencies"), list) else ""
    )

    real_ongoing_charges = get_dict(fees_timed.get("real_ongoing_charges"))
    registration_countries = join_text_list(overview.get("registration_countries"))
    share_type = clean_text(share_entry.get("share_type"))
    share_class_name = clean_text(share_entry.get("legal_name"))
    nav_value, nav_date = extract_latest_nav(payload, selected_currency)

    row.update(
        {
            "etf_name": clean_text(header.get("legal_name") or header.get("name")),
            "ccy": selected_currency,
            "ter_bps": percentage_to_bps(real_ongoing_charges.get("value")),
            "aum_mn": amount_to_millions(get_dict(overview_key_numbers.get("aum")).get("value")),
            "share_class_aum_mn": extract_share_class_aum(payload, selected_currency),
            "nav": nav_value,
            "nav_date": nav_date,
            "launch_date": clean_text(portfolio.get("launch_date")),
            "creation_date": clean_text(portfolio.get("creation_date")),
            "benchmark": clean_text(portfolio.get("bench")),
            "asset_class": clean_text(classification.get("asset_class") or portfolio.get("asset_class")),
            "sub_asset_class": clean_text(classification.get("sub_asset_class")),
            "region": clean_text(classification.get("region_reporting") or classification.get("region")),
            "domicile": clean_text(portfolio.get("domicile")),
            "legal_form": clean_text(portfolio.get("legal_form_extended") or portfolio.get("legal_form")),
            "management_company": clean_text(portfolio.get("management_company")),
            "fund_type": clean_text(portfolio.get("ucits")),
            "share_class_name": share_class_name,
            "share_type": share_type,
            "distribution_policy": clean_text(get_dict(payload.get("footer")).get("dividend_policy")),
            "bloomberg_code": clean_text(get_dict(fundshare_selection.get("codes")).get("BLOOMBERG")),
            "reuters_code": clean_text(portfolio.get("reuters_code")),
            "inav_bloomberg": clean_text(get_dict(fundshare_selection.get("codes")).get("INAV_BLOOMBERG")),
            "inav_reuters": clean_text(get_dict(fundshare_selection.get("codes")).get("INAV_REUTERS")),
            "wkn_code": clean_text(get_dict(fundshare_selection.get("codes")).get("WKN")),
            "morningstar_rating": clean_text(fundshare_selection.get("morning_star")),
            "morningstar_rating_date": clean_text(get_dict(fundshare_selection.get("morning_star_timed")).get("date")),
            "sfdr_article": clean_text(sfdr.get("article")),
            "amf_category": clean_text(sfdr.get("amf_approach")),
            "risk_indicator": clean_text(risk.get("srri_risk") or risk.get("sri_risk")),
            "recommended_horizon_years": clean_text(overview.get("recommended_investment_horizon")),
            "registration_countries": registration_countries,
            "fund_size_date": clean_text(get_dict(overview_key_numbers.get("aum")).get("market")),
            "ter_date": clean_text(real_ongoing_charges.get("start_date")),
            "reference_date": clean_text(payload.get("reference_date")),
            "factsheet_url": extract_public_factsheet_url(payload),
            "objective": clean_text(get_dict(overview.get("disclaimers")).get("investment_policy")),
            "fetch_status": "ok",
        }
    )

    return row


def fetch_fundsheet_payload(isin: str) -> tuple[dict[str, Any] | None, str, str, str, str, str]:
    normalized_isin = normalize_isin(isin)
    last_status = ""

    for profile, language, country in API_CONTEXTS:
        api_url = build_api_url_for_context(normalized_isin, profile, language, country)
        try:
            response = SESSION.get(api_url, timeout=REQUEST_TIMEOUT_S)
        except Exception as exc:
            last_status = f"error:{type(exc).__name__}"
            continue

        if response.status_code == 404:
            last_status = "http_error:404"
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "?"
            last_status = f"http_error:{status_code}"
            continue

        try:
            payload = response.json()
        except ValueError:
            last_status = "invalid_json"
            continue

        if not isinstance(payload, dict) or not payload:
            last_status = "empty_payload"
            continue

        return payload, api_url, profile, language, country, "ok"

    return None, "", "", "", "", last_status or "not_found"


def fetch_isin_row(isin: str) -> dict[str, str]:
    normalized_isin = normalize_isin(isin)
    row = build_empty_row(normalized_isin)
    payload, api_url, profile, language, country, fetch_status = fetch_fundsheet_payload(normalized_isin)
    if payload is None:
        row["fetch_status"] = fetch_status
        if fetch_status.startswith("http_error:"):
            logging.warning("BNP Paribas ISIN %s returned %s across all API contexts", normalized_isin, fetch_status)
        else:
            logging.warning("Could not fetch BNP Paribas data for %s: %s", normalized_isin, fetch_status)
        return row

    extracted = extract_row_from_payload(payload, normalized_isin)
    extracted["api_url"] = api_url
    extracted["api_profile"] = profile
    extracted["api_language"] = language
    extracted["api_country"] = country
    if not extracted["etf_name"]:
        extracted["fetch_status"] = "missing_name"
    elif (profile, language, country) != (PROFILE_CODE, LANGUAGE_CODE, COUNTRY_CODE):
        logging.info(
            "BNP Paribas ISIN %s resolved via fallback API context %s/%s/%s",
            normalized_isin,
            profile,
            language,
            country,
        )
    return extracted


def build_snapshot(now: datetime) -> dict[str, object]:
    listing_rows: list[dict[str, str]] = []

    logging.info("Loaded %d BNP Paribas target ISINs from the scraper constant list.", len(TARGET_ISINS))
    for index, isin in enumerate(TARGET_ISINS, start=1):
        logging.info("[%d/%d] Fetching %s", index, len(TARGET_ISINS), isin)
        listing_rows.append(fetch_isin_row(isin))
        if index < len(TARGET_ISINS):
            time.sleep(REQUEST_DELAY_S)

    status_counts: dict[str, int] = {}
    for row in listing_rows:
        status = row["fetch_status"]
        status_counts[status] = status_counts.get(status, 0) + 1

    logging.info("BNP Paribas fetch summary: %s", status_counts)

    return {
        "source": {
            "provider": ISSUER,
            "public_site": "https://www.bnpparibas-am.com/en-gb/",
            "api_url_template": FUNDSHEET_API_URL_TEMPLATE,
            "public_factsheet_url_template": PUBLIC_FACTSHEET_URL_TEMPLATE,
            "default_profile": PROFILE_CODE,
            "default_language": LANGUAGE_CODE,
            "default_country": COUNTRY_CODE,
            "api_contexts": [
                {"profile": profile, "language": language, "country": country}
                for profile, language, country in API_CONTEXTS
            ],
        },
        "method": "Explicit BNP ISIN list + official BNP Paribas UK fundsheet API per ISIN",
        "captured_at": now.isoformat(),
        "target_isins": TARGET_ISINS,
        "listing_rows": listing_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now = timestamp_now()
    snapshot = build_snapshot(now)
    listing_rows = snapshot.get("listing_rows", [])
    if isinstance(listing_rows, list) and not any(
        isinstance(row, dict) and clean_text(row.get("fetch_status")) == "ok"
        for row in listing_rows
    ):
        raise ConnectionError(
            "BNP Paribas download produced no successful API responses. "
            "The network may be unavailable or the site may be blocking requests."
        )
    write_json(destination, snapshot)
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved: %s", destination)


async def download_bnpparibas_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("listing_rows", [])


def main() -> None:
    output_path = build_output_path(timestamp_now())
    download_snapshot(output_path)


if __name__ == "__main__":
    main()
