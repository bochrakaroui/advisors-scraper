"""Scrape official abrdn UCITS ETF products and export selected fields CSV."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


ISSUER = "abrdn"
BASE_URL = "https://www.aberdeeninvestments.com"
VIEW_ALL_FUNDS_URL = f"{BASE_URL}/en-ch/investor/funds/view-all-funds"
OVERVIEW_API_URL = f"{BASE_URL}/api/gateway/funds/overview"
KEY_INFO_API_URL = f"{BASE_URL}/api/gateway/funds/fundDetailsKeyInformation"

LANGUAGE = "en-CH"
SITE = "Investor"
COUNTRY_CODE = "CHE"
INVESTOR_TYPE = "4,1"
JURISDICTION = "Live"
LITERATURE_AUTHORIZATION = "1"
TAKE = 100
REQUEST_TIMEOUT_S = 60
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "abrdn"
RAW_FILENAME = "abrdn_etf_export.json"

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SPACE_RE = re.compile(r"\s+")
NON_VALUE_STRINGS = {"", "-", "--", "none", "null", "n/a", "nan"}
ISIN_RE = re.compile(r"^[A-Z0-9]{12}$")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now(UTC)


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / RAW_FILENAME


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_RE.sub(" ", cleaned)
    return "" if cleaned.lower() in NON_VALUE_STRINGS else cleaned


def normalize_isin(value: object | None) -> str:
    cleaned = clean_text(value).upper().replace(" ", "")
    return cleaned if ISIN_RE.fullmatch(cleaned) else ""


def normalize_ccy(value: object | None) -> str:
    cleaned = clean_text(value).upper()
    return cleaned if re.fullmatch(r"[A-Z]{3}", cleaned) else ""


def infer_ccy_from_share_class(share_class_name: object | None) -> str:
    match = re.match(r"^([A-Z]{3})\b", clean_text(share_class_name).upper())
    return match.group(1) if match else ""


def parse_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None

    normalized = re.sub(r"[^0-9,.\-+]", "", cleaned)
    if not normalized:
        return None

    if "," in normalized and "." in normalized:
        if normalized.rfind(".") > normalized.rfind(","):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized and "." not in normalized:
        parts = normalized.split(",")
        if len(parts) > 2 or all(len(part) == 3 for part in parts[1:]):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(",", ".")

    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def decimal_to_string(value: Decimal, places: int = 2) -> str:
    rendered = format_decimal(value, places)
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def percent_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return decimal_to_string(decimal_value * Decimal("100"), places=2)


def amount_to_millions(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def parse_iso_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    candidates = [
        cleaned,
        cleaned.replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).strftime("%d/%m/%Y")
        except ValueError:
            continue

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


def slugify(value: str) -> str:
    slug = clean_text(value).lower()
    slug = slug.replace("&", " and ")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def build_detail_url(fund_name: str, isin: str) -> str:
    return f"{VIEW_ALL_FUNDS_URL}/{slugify(fund_name)}-{normalize_isin(isin).lower()}"


def country_investors_payload(*, include_literature_authorization: bool) -> dict[str, str]:
    payload = {
        "countryCode": COUNTRY_CODE,
        "investorType": INVESTOR_TYPE,
        "jurisdiction": JURISDICTION,
    }
    if include_literature_authorization:
        payload["literatureAuthorization"] = LITERATURE_AUTHORIZATION
    return payload


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_json(session: requests.Session, url: str, payload: dict[str, Any], referer: str) -> Any:
    response = session.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": referer,
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


def fetch_text(session: requests.Session, url: str, referer: str = VIEW_ALL_FUNDS_URL) -> str:
    response = session.get(
        url,
        headers={"Referer": referer},
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.text


def parse_next_data(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    next_data_node = soup.find("script", id="__NEXT_DATA__")
    if next_data_node is None or not next_data_node.string:
        raise ValueError("Embedded __NEXT_DATA__ payload not found on official fund page.")
    payload = json.loads(next_data_node.string)
    return payload.get("props", {}).get("pageProps", {})


def discover_overview_funds(session: requests.Session) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    skip = 0

    while True:
        payload = {
            "countryInvestors": country_investors_payload(include_literature_authorization=True),
            "searchQuery": {"id": "", "name": "", "isin": ""},
            "language": LANGUAGE,
            "site": SITE,
            "skip": skip,
            "take": TAKE,
            "filters": [],
            "tab": "overview",
        }
        data = fetch_json(session, OVERVIEW_API_URL, payload, f"{VIEW_ALL_FUNDS_URL}?page=1")
        batch = data.get("content", {}).get("overview", []) if isinstance(data, dict) else []
        if not isinstance(batch, list) or not batch:
            break
        results.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < TAKE:
            break
        skip += TAKE

    return results


def is_ucits_etf_candidate(fund_item: dict[str, Any]) -> bool:
    fund_name = clean_text(fund_item.get("name")).upper()
    if "UCITS ETF" in fund_name:
        return True

    for share_class in fund_item.get("shareclasses", []):
        if not isinstance(share_class, dict):
            continue
        share_class_name = clean_text(share_class.get("shareclassName")).upper()
        factsheet_title = clean_text((share_class.get("factsheet") or {}).get("title")).upper()
        if "ETF" in share_class_name or "UCITS ETF" in factsheet_title:
            return True
    return False


def discover_etf_candidates(session: requests.Session) -> list[dict[str, Any]]:
    overview_funds = discover_overview_funds(session)
    candidates: list[dict[str, Any]] = []
    seen_isins: set[str] = set()

    for fund_item in overview_funds:
        if not is_ucits_etf_candidate(fund_item):
            continue

        fund_name = clean_text(fund_item.get("name"))
        fund_id = clean_text(fund_item.get("id"))
        share_classes = fund_item.get("shareclasses", [])
        if not fund_name or not fund_id or not isinstance(share_classes, list):
            continue

        for share_class in share_classes:
            if not isinstance(share_class, dict):
                continue
            isin = normalize_isin(share_class.get("isin"))
            if not isin or isin in seen_isins:
                continue

            share_class_name = clean_text(share_class.get("shareclassName"))
            if "ETF" not in fund_name.upper() and "ETF" not in share_class_name.upper():
                continue

            seen_isins.add(isin)
            candidates.append(
                {
                    "fund_id": fund_id,
                    "fund_name": fund_name,
                    "share_class_id": clean_text(share_class.get("shareclassID")),
                    "share_class_name": share_class_name,
                    "isin": isin,
                    "asset_class": clean_text(fund_item.get("assetClass")),
                    "sfdr_classification": clean_text(fund_item.get("sfdrClassification")),
                    "correlation_fund_range_id": clean_text(fund_item.get("correlationFundRangeId")),
                    "factsheet_url": clean_text((share_class.get("factsheet") or {}).get("documentURI")),
                    "detail_url": build_detail_url(fund_name, isin),
                }
            )

    candidates.sort(key=lambda item: (item["fund_name"], item["isin"]))
    return candidates


def discover_detail_urls_with_playwright(target_isins: set[str]) -> dict[str, str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logging.warning("Playwright is not installed; skipping browser fallback for abrdn detail URL discovery.")
        return {}

    async def _run() -> dict[str, str]:
        detail_url_map: dict[str, str] = {}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            page = await context.new_page()
            try:
                await page.goto(f"{VIEW_ALL_FUNDS_URL}?page=1", wait_until="networkidle", timeout=120000)
                hrefs = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href*="/funds/view-all-funds/"]'))
                    .map(anchor => anchor.href || "")
                    .filter(Boolean)"""
                )
            finally:
                await page.close()
                await context.close()
                await browser.close()

        for href in hrefs:
            match = re.search(r"-([a-z0-9]{12})$", href, re.IGNORECASE)
            if not match:
                continue
            isin = match.group(1).upper()
            if isin in target_isins:
                detail_url_map[isin] = href
        return detail_url_map

    return asyncio.run(_run())


def fetch_detail_page_context(
    session: requests.Session,
    candidate: dict[str, Any],
    playwright_url_map: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    detail_url = clean_text(candidate.get("detail_url"))
    html_error: Exception | None = None

    for candidate_url in [detail_url, clean_text((playwright_url_map or {}).get(candidate["isin"]))]:
        if not candidate_url:
            continue
        try:
            html = fetch_text(session, candidate_url, VIEW_ALL_FUNDS_URL)
            page_props = parse_next_data(html)
            return page_props, candidate_url
        except Exception as exc:  # noqa: BLE001
            html_error = exc

    if html_error is not None:
        raise html_error
    raise RuntimeError("No official detail page URL could be resolved.")


def fetch_key_information(
    session: requests.Session,
    fund_details_data: dict[str, Any],
    detail_url: str,
) -> dict[str, Any]:
    payload = {
        "countryCode": COUNTRY_CODE,
        "language": LANGUAGE,
        "shareClass": clean_text((fund_details_data.get("selectedShareclass") or {}).get("id")),
        "assetClassId": clean_text(fund_details_data.get("assetClassId")),
        "fund": clean_text(fund_details_data.get("id")),
        "correlationFundRangeId": clean_text(fund_details_data.get("correlationFundRangeID")),
        "fundRangeName": clean_text(fund_details_data.get("fundRangeName")),
        "fundName": clean_text(fund_details_data.get("name")),
        "fundNameId": clean_text(fund_details_data.get("fundNameId")),
        "shareClassNameId": clean_text(fund_details_data.get("shareClassNameId")),
        "literatureAuthorizations": [LITERATURE_AUTHORIZATION],
        "sfdrClassificationId": clean_text(fund_details_data.get("sfdrArticleClassificationId")),
        "countryInvestors": country_investors_payload(include_literature_authorization=False),
        "site": SITE,
    }
    data = fetch_json(session, KEY_INFO_API_URL, payload, detail_url)
    return data.get("content", {}) if isinstance(data, dict) else {}


def build_output_row(
    candidate: dict[str, Any],
    detail_page_props: dict[str, Any] | None,
    detail_url: str,
    key_info: dict[str, Any] | None,
    today: datetime,
) -> dict[str, str]:
    fund_details_data = (
        detail_page_props.get("pageData", {}).get("fundDetailsData", {})
        if isinstance(detail_page_props, dict)
        else {}
    )
    share_class_data = (key_info or {}).get("shareClass", {}) if isinstance(key_info, dict) else {}
    fund_data = (key_info or {}).get("fund", {}) if isinstance(key_info, dict) else {}
    selected_share_class = fund_details_data.get("selectedShareclass", {})
    first_price = ((selected_share_class or {}).get("prices") or [{}])[0]

    page_title = clean_text((detail_page_props or {}).get("pageData", {}).get("pageTitle"))
    etf_name = clean_text(page_title.replace("| Aberdeen", "")) or clean_text(fund_details_data.get("name")) or candidate["fund_name"]

    isin = (
        normalize_isin(share_class_data.get("isin"))
        or normalize_isin((selected_share_class or {}).get("isin"))
        or candidate["isin"]
    )
    ccy = (
        normalize_ccy(share_class_data.get("shareClassCurrency"))
        or normalize_ccy(share_class_data.get("baseCurrency"))
        or normalize_ccy((first_price or {}).get("priceCurrencyCode"))
        or normalize_ccy(fund_details_data.get("fundSizeCurrency"))
        or infer_ccy_from_share_class(candidate.get("share_class_name"))
    )
    ter_bps = percent_to_bps(share_class_data.get("totalExpenseRatio"))

    aum_m = (
        amount_to_millions(share_class_data.get("netAssets"))
        or amount_to_millions(fund_data.get("fundSize"))
        or amount_to_millions(fund_details_data.get("fundSizeValue"))
    )
    date = (
        parse_iso_date(fund_details_data.get("fundSizeValueDate"))
        or parse_iso_date((first_price or {}).get("currentAsAt"))
        or today.strftime("%d/%m/%Y")
    )

    return {
        "ETF Name": etf_name,
        "Issuer": ISSUER,
        "ISIN": isin,
        "CCY": ccy,
        "TER(bps)": ter_bps,
        "AUM(M)": aum_m,
        "Date": date,
        "_detail_url": detail_url,
        "_factsheet_url": clean_text(candidate.get("factsheet_url")),
    }


def count_missing(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for column in OUTPUT_COLUMNS:
        counts[column] = sum(1 for row in rows if not clean_text(row.get(column)))
    return counts


def write_json(output_path: Path, payload: object) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def scrape_abrdn_etfs() -> Path:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)
    session = build_session()

    overview_candidates = discover_etf_candidates(session)
    logging.info("Discovered %d official abrdn UCITS ETF fund page(s).", len(overview_candidates))

    rows: list[dict[str, str]] = []
    failed_detail_urls: set[str] = set()
    unresolved_isins: set[str] = set()
    playwright_url_map: dict[str, str] = {}

    for candidate in overview_candidates:
        detail_page_props: dict[str, Any] | None = None
        key_info: dict[str, Any] | None = None
        resolved_detail_url = candidate["detail_url"]

        try:
            detail_page_props, resolved_detail_url = fetch_detail_page_context(session, candidate)
        except Exception as exc:  # noqa: BLE001
            failed_detail_urls.add(candidate["detail_url"])
            unresolved_isins.add(candidate["isin"])
            logging.warning(
                "Official abrdn fund page fetch failed for %s (%s): %s",
                candidate["fund_name"],
                candidate["isin"],
                exc,
            )

        if unresolved_isins and not playwright_url_map:
            logging.info("Trying Playwright fallback to discover unresolved official abrdn fund page URLs.")
            playwright_url_map = discover_detail_urls_with_playwright(unresolved_isins)

        if detail_page_props is None and candidate["isin"] in playwright_url_map:
            try:
                detail_page_props, resolved_detail_url = fetch_detail_page_context(
                    session,
                    candidate,
                    playwright_url_map=playwright_url_map,
                )
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "Playwright fallback detail page fetch still failed for %s (%s): %s",
                    candidate["fund_name"],
                    candidate["isin"],
                    exc,
                )

        fund_details_data = (
            detail_page_props.get("pageData", {}).get("fundDetailsData", {})
            if isinstance(detail_page_props, dict)
            else {}
        )
        if clean_text(fund_details_data.get("fundRangeName")) and clean_text(fund_details_data.get("fundRangeName")) != "ETF (UCITS)":
            logging.info(
                "Skipping non-ETF product returned from official page %s (%s): %s",
                candidate["fund_name"],
                candidate["isin"],
                clean_text(fund_details_data.get("fundRangeName")),
            )
            continue

        if detail_page_props is not None:
            try:
                key_info = fetch_key_information(session, fund_details_data, resolved_detail_url)
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "Official abrdn key information fetch failed for %s (%s): %s",
                    candidate["fund_name"],
                    candidate["isin"],
                    exc,
                )

        row = build_output_row(candidate, detail_page_props, resolved_detail_url, key_info, now)
        rows.append(row)

    rows.sort(key=lambda row: (row.get("ETF Name", ""), row.get("ISIN", "")))
    missing_counts = count_missing(rows)

    logging.info("Extracted %d abrdn UCITS ETF row(s).", len(rows))
    for field_name in OUTPUT_COLUMNS:
        logging.info("Missing %-9s: %d", field_name, missing_counts[field_name])

    write_json(
        output_path,
        {
            "captured_at": now.isoformat(),
            "provider": ISSUER,
            "overview_candidate_count": len(overview_candidates),
            "row_count": len(rows),
            "listing_rows": rows,
        },
    )

    print(f"Fund pages discovered : {len(overview_candidates):,}")
    print(f"ETF rows extracted    : {len(rows):,}")
    print(f"Output file           : {output_path}")
    print("Missing field counts  :")
    for field_name in OUTPUT_COLUMNS:
        print(f"  {field_name}: {missing_counts[field_name]:,}")

    return output_path


def main() -> None:
    scrape_abrdn_etfs()


if __name__ == "__main__":
    main()
