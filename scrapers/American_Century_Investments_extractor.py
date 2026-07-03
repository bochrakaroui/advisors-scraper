"""Download Avantis UCITS ETF data for American Century Investments.

Workflow:
  1. Open the live Avantis UCITS ETF landing page with Playwright using a
     German non-US location cookie, because the legacy public URL now redirects.
  2. Discover the current UCITS ETF product pages from that landing page.
  3. Visit each product page, keep only the six requested ISINs, and extract the
     live product facts from the hydrated page content.
  4. Open each official English factsheet PDF and extract supplementary fields
     for verification and raw-document coverage.
  5. Save one provider-specific raw snapshot JSON.

Output:
    providers/American_Century_Investments/<YYYY-MM-DD>/
        american_century_investments_etf_export.json
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from pypdf import PdfReader

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
except ImportError as exc:  # pragma: no cover - environment guard
    raise RuntimeError(
        "playwright is required for the American Century Investments scraper. "
        "Install it with 'pip install playwright' and run "
        "'python -m playwright install chromium'."
    ) from exc


ISSUER = "American Century Investments"
BRAND = "Avantis Investors"
BASE_URL = "https://www.avantisinvestors.com"
INDEX_URL = f"{BASE_URL}/ucitsetf/de-de/"
REQUEST_TIMEOUT_S = 120
PLAYWRIGHT_TIMEOUT_MS = 120000
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

TARGET_ISINS = [
    "IE0003R87OG3",
    "IE000K975W13",
    "IE000OW54ZX1",
    "IE000RJECXS5",
    "IE000SDFJUU0",
    "IE000T62NEO6",
]

DEFAULT_DETAIL_URLS = [
    f"{BASE_URL}/ucitsetf/de-de/avantis-america-equity-ucits-etf/",
    f"{BASE_URL}/ucitsetf/de-de/avantis-emerging-markets-equity-ucits-etf/",
    f"{BASE_URL}/ucitsetf/de-de/avantis-europe-equity-ucits-etf/",
    f"{BASE_URL}/ucitsetf/de-de/avantis-global-equity-ucits-etf/",
    f"{BASE_URL}/ucitsetf/de-de/avantis-global-small-cap-value-ucits-etf/",
    f"{BASE_URL}/ucitsetf/de-de/avantis-pacific-equity-ucits-etf/",
]

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "American_Century_Investments"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9,de;q=0.8",
    "Accept": "application/pdf,application/json,text/html,*/*",
}

PDF_SESSION = requests.Session()
PDF_SESSION.headers.update(HEADERS)

INTERNAL_SPACE_RE = re.compile(r"\s+")
ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
AMOUNT_WITH_SUFFIX_RE = re.compile(r"([€$£])?\s*([\d.,]+)\s*([KMB])?\b", re.IGNORECASE)

LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "nav": re.compile(r"NAV\s+([^\n]+)"),
    "nav_date": re.compile(r"Kurse vom\s+(\d{4}/\d{2}/\d{2})"),
    "fund_inception_date": re.compile(r"Fondsauflagedatum\s+(\d{4}/\d{2}/\d{2})"),
    "share_class_inception_date": re.compile(r"Auflagedatum Anteilsklasse\s+(\d{4}/\d{2}/\d{2})"),
    "fund_currency": re.compile(r"Fondsw\u00e4hrung\s+([A-Z]{3})"),
    "asset_class": re.compile(r"Anlageklasse\s+([^\n]+)"),
    "distribution_policy": re.compile(r"Ertragsverwendung\s+([^\n]+)"),
    "benchmark": re.compile(r"Benchmark\s+([^\n]+)"),
    "number_of_holdings": re.compile(
        r"Anzahl der Positionen\s+([^\n]+?)\s+Stand\s+(\d{4}/\d{2}/\d{2})",
        re.DOTALL,
    ),
    "total_assets": re.compile(
        r"Gesamtverm\u00f6gen\s+([^\n]+?)\s+Stand\s+(\d{4}/\d{2}/\d{2})",
        re.DOTALL,
    ),
    "share_class_assets": re.compile(
        r"Verm\u00f6gen Anteilsklasse\s+([^\n]+?)\s+Stand\s+(\d{4}/\d{2}/\d{2})",
        re.DOTALL,
    ),
    "shares_outstanding": re.compile(
        r"Umlaufende Anteile\s+([^\n]+?)\s+Stand\s+(\d{4}/\d{2}/\d{2})",
        re.DOTALL,
    ),
    "registered_countries": re.compile(r"Registrierte L\u00e4nder\s+([^\n]+)"),
    "administrator_custodian": re.compile(r"Verwaltung/Verwahrstelle\s+([^\n]+)"),
    "investment_manager": re.compile(r"Fondsmanager\s+([^\n]+)"),
    "priips_risk_indicator": re.compile(
        r"PRIIPs KID - Risikoindikator\*\s+([^\n]+?)\s+as of\s+(\d{4}/\d{2}/\d{2})",
        re.DOTALL,
    ),
    "kiid_risk_indicator": re.compile(
        r"KIID - Risikoindikator\*\s+([^\n]+?)\s+as of\s+(\d{4}/\d{2}/\d{2})",
        re.DOTALL,
    ),
    "isin": re.compile(r"ISIN\s+([A-Z0-9]{12})"),
    "product_structure": re.compile(r"Produktstruktur\s+([^\n]+)"),
    "investment_method": re.compile(r"Methodik\s+([^\n]+)"),
    "domicile": re.compile(r"Domizil\s+([^\n]+)"),
    "fiscal_year_end": re.compile(r"Gesch\u00e4ftsjahresende\s+([^\n]+)"),
    "sfdr_classification": re.compile(r"SFDR-Klassifizierung\s+([^\n]+)"),
    "fund_legal_structure": re.compile(r"Rechtsform des Fonds\s+([^\n]+)"),
    "uk_reporting_status": re.compile(r"UK-Meldestatus\s+([^\n]+)"),
    "ocf_percent": re.compile(r"Laufende Kosten \(OCF\)\s+([0-9.]+%)"),
}

FACTSHEET_PATTERNS: dict[str, re.Pattern[str]] = {
    "factsheet_marketing_date": re.compile(r"Marketing Communication as of\s+(\d{2}/\d{2}/\d{4})"),
    "factsheet_total_aum": re.compile(r"TOTAL AUM\s+([^\n]+)"),
    "factsheet_base_currency": re.compile(r"FUND BASE CURRENCY\s+([A-Z]{3})"),
    "factsheet_use_of_income": re.compile(r"USE OF INCOME\s+([^\n]+)"),
    "factsheet_ongoing_charges_percent": re.compile(r"ON-GOING CHARGES FEE p\.a\.\s+([0-9.]+%)"),
    "factsheet_product_structure": re.compile(r"PRODUCT STRUCTURE\s+([^\n]+)"),
    "factsheet_methodology": re.compile(r"METHODOLOGY\s+([^\n]+)"),
    "factsheet_domicile": re.compile(r"DOMICILE\s+([^\n]+)"),
    "factsheet_fund_legal_structure": re.compile(r"FUND LEGAL STRUCTURE\s+([^\n]+)"),
    "factsheet_uk_reporting_status": re.compile(r"UK REPORTING STATUS\s+([^\n]+)"),
}

CURRENCY_SYMBOL_MAP = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
}


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "american_century_investments_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = INTERNAL_SPACE_RE.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "None", "null", "nan", "NaN"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def normalize_url(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    absolute = urljoin(BASE_URL, cleaned)
    split_url = urlsplit(absolute)
    normalized_path = re.sub(r"/{2,}", "/", split_url.path)
    return urlunsplit((split_url.scheme.lower(), split_url.netloc.lower(), normalized_path, split_url.query, ""))


def parse_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    normalized = re.sub(r"[^\d.,-]", "", cleaned)
    if not normalized:
        return None
    if "," in normalized and "." in normalized:
        if normalized.rfind(".") > normalized.rfind(","):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized and "." not in normalized:
        comma_parts = normalized.split(",")
        if len(comma_parts) > 2 or all(len(part) == 3 for part in comma_parts[1:]):
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


def percentage_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value * Decimal("100"), places=2)


def extract_currency_from_amount(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for symbol, code in CURRENCY_SYMBOL_MAP.items():
        if symbol in cleaned:
            return code
    code_match = re.match(r"^([A-Z]{3})\b", cleaned)
    return code_match.group(1) if code_match else ""


def amount_text_to_millions(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    match = AMOUNT_WITH_SUFFIX_RE.search(cleaned)
    if not match:
        decimal_value = parse_decimal(cleaned)
        return format_decimal(decimal_value / Decimal("1000000"), places=2) if decimal_value is not None else ""

    number_text = match.group(2)
    suffix = (match.group(3) or "").upper()
    decimal_value = parse_decimal(number_text)
    if decimal_value is None:
        return ""

    if suffix == "B":
        decimal_value *= Decimal("1000")
    elif suffix == "M":
        decimal_value *= Decimal("1")
    elif suffix == "K":
        decimal_value *= Decimal("0.001")
    else:
        decimal_value /= Decimal("1000000")

    return format_decimal(decimal_value, places=2)


def extract_first_match(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
    match = pattern.search(text)
    if not match:
        return ()
    return tuple(clean_text(group) for group in match.groups())


def parse_detail_text(text: str) -> dict[str, str]:
    row: dict[str, str] = {}

    nav_match = extract_first_match(LABEL_PATTERNS["nav"], text)
    if nav_match:
        row["nav"] = nav_match[0]

    nav_date_match = extract_first_match(LABEL_PATTERNS["nav_date"], text)
    if nav_date_match:
        row["nav_date"] = nav_date_match[0]

    for key in (
        "fund_inception_date",
        "share_class_inception_date",
        "fund_currency",
        "asset_class",
        "distribution_policy",
        "benchmark",
        "registered_countries",
        "administrator_custodian",
        "investment_manager",
        "isin",
        "product_structure",
        "investment_method",
        "domicile",
        "fiscal_year_end",
        "sfdr_classification",
        "fund_legal_structure",
        "uk_reporting_status",
        "ocf_percent",
    ):
        match = extract_first_match(LABEL_PATTERNS[key], text)
        if match:
            row[key] = match[0]

    for key in ("number_of_holdings", "total_assets", "share_class_assets", "shares_outstanding"):
        match = extract_first_match(LABEL_PATTERNS[key], text)
        if match:
            row[key] = match[0]
            if len(match) > 1:
                row[f"{key}_date"] = match[1]

    for key in ("priips_risk_indicator", "kiid_risk_indicator"):
        match = extract_first_match(LABEL_PATTERNS[key], text)
        if match:
            row[key] = match[0]
            if len(match) > 1:
                row[f"{key}_date"] = match[1]

    aum_currency = extract_currency_from_amount(row.get("total_assets"))
    if aum_currency:
        row["aum_ccy"] = aum_currency
    if row.get("total_assets"):
        row["aum_mn"] = amount_text_to_millions(row.get("total_assets"))
    if row.get("share_class_assets"):
        row["share_class_aum_mn"] = amount_text_to_millions(row.get("share_class_assets"))
    if row.get("ocf_percent"):
        row["ter_bps"] = percentage_to_bps(row.get("ocf_percent"))

    if row.get("fund_currency") and not row.get("aum_ccy"):
        row["aum_ccy"] = row["fund_currency"]

    return row


def extract_factsheet_links(links: list[dict[str, str]]) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for item in links:
        text = clean_text(item.get("text"))
        href = normalize_url(item.get("href"))
        if not text or not href:
            continue
        if text == "Fact Sheet - EN":
            extracted["factsheet_en_url"] = href
        elif text == "Fact Sheet - DE":
            extracted["factsheet_de_url"] = href
    return extracted


def fetch_factsheet_text(url: str) -> str:
    response = PDF_SESSION.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_factsheet_text(text: str) -> dict[str, str]:
    row: dict[str, str] = {}
    for key, pattern in FACTSHEET_PATTERNS.items():
        match = extract_first_match(pattern, text)
        if match:
            row[key] = match[0]

    all_isins = sorted(set(ISIN_RE.findall(text)))
    if all_isins:
        row["factsheet_isin"] = all_isins[0]

    total_aum_raw = row.get("factsheet_total_aum", "")
    if total_aum_raw:
        row["factsheet_total_aum_mn"] = amount_text_to_millions(total_aum_raw)
        row["factsheet_total_aum_ccy"] = extract_currency_from_amount(total_aum_raw)

    charges = row.get("factsheet_ongoing_charges_percent", "")
    if charges:
        row["factsheet_ter_bps"] = percentage_to_bps(charges)

    return row


async def fetch_page_payload(context: BrowserContext, url: str) -> tuple[str, str, list[dict[str, str]]]:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        title = await page.title()
        text = await page.locator("body").inner_text()
        links = await page.locator("a[href]").evaluate_all(
            """
            elements => elements.map(element => ({
                text: (element.innerText || '').trim(),
                href: element.href || element.getAttribute('href') || ''
            }))
            """
        )
        normalized_links = [
            {"text": clean_text(item.get("text")), "href": normalize_url(item.get("href"))}
            for item in links
        ]
        return title, text, normalized_links
    finally:
        await page.close()


async def discover_detail_urls(context: BrowserContext) -> list[str]:
    _title, _text, links = await fetch_page_payload(context, INDEX_URL)
    discovered_urls: list[str] = []
    seen_urls: set[str] = set()

    for item in links:
        href = normalize_url(item.get("href"))
        if "/ucitsetf/de-de/avantis-" not in href or not href.endswith("/"):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        discovered_urls.append(href)

    return discovered_urls or DEFAULT_DETAIL_URLS


def build_empty_row(detail_url: str) -> dict[str, str]:
    return {
        "issuer": ISSUER,
        "brand": BRAND,
        "detail_url": detail_url,
        "etf_name": "",
        "isin": "",
        "fund_currency": "",
        "asset_class": "",
        "distribution_policy": "",
        "benchmark": "",
        "number_of_holdings": "",
        "number_of_holdings_date": "",
        "total_assets": "",
        "total_assets_date": "",
        "share_class_assets": "",
        "share_class_assets_date": "",
        "shares_outstanding": "",
        "shares_outstanding_date": "",
        "registered_countries": "",
        "administrator_custodian": "",
        "investment_manager": "",
        "priips_risk_indicator": "",
        "priips_risk_indicator_date": "",
        "kiid_risk_indicator": "",
        "kiid_risk_indicator_date": "",
        "product_structure": "",
        "investment_method": "",
        "domicile": "",
        "fiscal_year_end": "",
        "sfdr_classification": "",
        "fund_legal_structure": "",
        "uk_reporting_status": "",
        "ocf_percent": "",
        "ter_bps": "",
        "aum_mn": "",
        "aum_ccy": "",
        "share_class_aum_mn": "",
        "nav": "",
        "nav_date": "",
        "fund_inception_date": "",
        "share_class_inception_date": "",
        "factsheet_en_url": "",
        "factsheet_de_url": "",
        "factsheet_marketing_date": "",
        "factsheet_total_aum": "",
        "factsheet_total_aum_mn": "",
        "factsheet_total_aum_ccy": "",
        "factsheet_base_currency": "",
        "factsheet_use_of_income": "",
        "factsheet_ongoing_charges_percent": "",
        "factsheet_ter_bps": "",
        "factsheet_product_structure": "",
        "factsheet_methodology": "",
        "factsheet_domicile": "",
        "factsheet_fund_legal_structure": "",
        "factsheet_uk_reporting_status": "",
        "factsheet_isin": "",
        "factsheet_fetch_status": "pending",
        "fetch_status": "pending",
    }


async def fetch_detail_row(context: BrowserContext, detail_url: str) -> dict[str, str]:
    row = build_empty_row(detail_url)

    try:
        title, text, links = await fetch_page_payload(context, detail_url)
    except Exception as exc:  # noqa: BLE001
        row["fetch_status"] = f"page_error:{type(exc).__name__}"
        logging.warning("Could not load Avantis detail page %s: %s", detail_url, exc)
        return row

    row["etf_name"] = clean_text(title)
    row.update(parse_detail_text(text))
    row.update(extract_factsheet_links(links))

    if row.get("factsheet_en_url"):
        try:
            factsheet_text = await asyncio.to_thread(fetch_factsheet_text, row["factsheet_en_url"])
            row.update(parse_factsheet_text(factsheet_text))
            row["factsheet_fetch_status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            row["factsheet_fetch_status"] = f"error:{type(exc).__name__}"
            logging.warning("Could not fetch Avantis factsheet %s: %s", row['factsheet_en_url'], exc)
    else:
        row["factsheet_fetch_status"] = "missing_url"

    row["isin"] = normalize_isin(row.get("isin") or row.get("factsheet_isin"))
    row["fund_currency"] = clean_text(row.get("fund_currency") or row.get("factsheet_base_currency")).upper()
    row["aum_ccy"] = clean_text(row.get("aum_ccy") or row.get("fund_currency") or row.get("factsheet_total_aum_ccy")).upper()

    if row.get("etf_name") and row.get("isin"):
        row["fetch_status"] = "ok"
    elif row.get("etf_name"):
        row["fetch_status"] = "missing_isin"
    else:
        row["fetch_status"] = "missing_name"

    return row


async def build_snapshot(now: datetime) -> dict[str, object]:
    logging.info("Opening Avantis UCITS landing page: %s", INDEX_URL)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(extra_http_headers=HEADERS)
        await context.add_cookies(
            [
                {"name": "countrySelection", "value": "de", "domain": "www.avantisinvestors.com", "path": "/"},
                {"name": "country", "value": "de", "domain": "www.avantisinvestors.com", "path": "/"},
            ]
        )

        try:
            detail_urls = await discover_detail_urls(context)
            logging.info("Discovered %d Avantis UCITS product pages.", len(detail_urls))

            listing_rows: list[dict[str, str]] = []
            seen_isins: set[str] = set()
            target_isins = set(TARGET_ISINS)

            for index, detail_url in enumerate(detail_urls, start=1):
                logging.info("[%d/%d] Fetching %s", index, len(detail_urls), detail_url)
                row = await fetch_detail_row(context, detail_url)
                isin = normalize_isin(row.get("isin"))
                if not isin or isin not in target_isins:
                    continue
                if isin in seen_isins:
                    continue
                seen_isins.add(isin)
                listing_rows.append(row)
        finally:
            await browser.close()

    status_counts: dict[str, int] = {}
    factsheet_status_counts: dict[str, int] = {}
    for row in listing_rows:
        fetch_status = row.get("fetch_status", "unknown")
        factsheet_status = row.get("factsheet_fetch_status", "unknown")
        status_counts[fetch_status] = status_counts.get(fetch_status, 0) + 1
        factsheet_status_counts[factsheet_status] = factsheet_status_counts.get(factsheet_status, 0) + 1

    found_target_isins = sorted(normalize_isin(row.get("isin")) for row in listing_rows if row.get("isin"))
    missing_target_isins = sorted(set(TARGET_ISINS) - set(found_target_isins))

    logging.info("American Century / Avantis page fetch summary: %s", status_counts)
    logging.info("American Century / Avantis factsheet fetch summary: %s", factsheet_status_counts)

    return {
        "source": {
            "provider": ISSUER,
            "brand": BRAND,
            "legacy_requested_url": f"{BASE_URL}/av/ucitsetf-funds/",
            "active_index_url": INDEX_URL,
            "factsheet_language": "EN",
            "location_cookie_country": "de",
        },
        "method": (
            "Playwright discovery over the live Avantis Germany UCITS ETF site "
            "+ hydrated product-page fact extraction + official English factsheet PDF parsing; "
            "AUM uses the live product-page Total Assets figure as requested."
        ),
        "captured_at": now.isoformat(),
        "target_isins": TARGET_ISINS,
        "found_target_isins": found_target_isins,
        "missing_target_isins": missing_target_isins,
        "listing_rows": listing_rows,
    }


async def download_american_century_investments_file() -> Path:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)
    snapshot = await build_snapshot(now)
    listing_rows = snapshot.get("listing_rows", [])

    if not isinstance(listing_rows, list) or not any(
        isinstance(row, dict) and clean_text(row.get("fetch_status")) == "ok"
        for row in listing_rows
    ):
        raise ConnectionError(
            "American Century Investments / Avantis download produced no successful product page rows. "
            "The network may be unavailable or the site may have changed."
        )

    write_json(output_path, snapshot)
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved: %s", output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def main() -> None:
    asyncio.run(download_american_century_investments_file())


if __name__ == "__main__":
    main()
