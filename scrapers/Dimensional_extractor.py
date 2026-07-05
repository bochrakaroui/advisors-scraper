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

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ModuleNotFoundError:  # pragma: no cover - optional PDF enrichment
    PdfReader = None  # type: ignore[assignment]


DIMENSIONAL_ISSUER = "Dimensional"
EXPAT_ISSUER = "Expat Asset Management"

DIMENSIONAL_LISTING_URL = "https://www.dimensional.com/gb-en/funds?ft=ucitsEtf"
DIMENSIONAL_FUND_CENTER_API_URL = "https://etf.dimensional.com/public/v2/fundcenter?allowMorningstarFixedIncome=true"
DIMENSIONAL_FUND_DETAIL_API_URL = "https://etf.dimensional.com/public/v2/fundcenter/funddetail?allowMorningstarFixedIncome=true"
DIMENSIONAL_PORTFOLIO_DETAILS_API_URL = (
    "https://www.dimensional.com/investment-api/portfolio-details?allowMorningstarFixedIncome=true"
)
DIMENSIONAL_FALLBACK_PROFILE_URL = "https://www.justetf.com/en/etf-profile.html?isin={isin}"
EXPAT_FUND_URL = "https://expat.bg/en/funds/ExpatBulgariaSOFIX?utm_source=chatgpt.com"
DIMENSIONAL_SELECTED_COUNTRY = "GB"
DIMENSIONAL_REGION_CODE = "emea"
DIMENSIONAL_COUNTRY_CODE = "gb"
DIMENSIONAL_LANGUAGE_CODE = "en"

REQUEST_TIMEOUT_S = 60

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Dimensional"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

DIMENSIONAL_TARGET_ISINS = [
    "IE0002YHUWS3",
    "IE000EGGFVG6",
    "IE000S67ID55",
    "IE000XKK4AV2",
]
EXPAT_TARGET_ISINS = ["BG9000011163"]
TARGET_ISINS = DIMENSIONAL_TARGET_ISINS + EXPAT_TARGET_ISINS

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SPACE_PATTERN = re.compile(r"\s+")
DATE_PATTERN = re.compile(r"(\d{1,2}\.\d{1,2}\.\d{4})")


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "dimensional_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None", "null", "N/A"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def parse_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    normalized = cleaned.replace("\u202f", " ").replace("\u2009", " ")
    normalized = normalized.replace(" ", "")
    normalized = normalized.replace("EUR", "").replace("USD", "").replace("GBP", "").replace("BGN", "")
    normalized = normalized.replace("p.a.", "").replace("%", "").strip()
    normalized = re.sub(r"[^\d,.-]", "", normalized)
    if not normalized:
        return None
    if "," in normalized and "." in normalized:
        if normalized.rfind(".") > normalized.rfind(","):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def parse_percent_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value * Decimal("100"), places=2)


def parse_amount_to_millions(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def parse_nav_value(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return clean_text(value)
    return format_decimal(decimal_value, places=4)


def parse_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in (
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def fetch_html(url: str) -> str:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.text


def fetch_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_payload: object | None = None,
) -> Any:
    response = SESSION.request(method=method.upper(), url=url, headers=headers, json=json_payload, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.json()


def fetch_pdf_text(url: str) -> str:
    if PdfReader is None:
        return ""
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def build_missing_row(isin: str, issuer: str) -> dict[str, Any]:
    return {
        "etf_name": "",
        "issuer": issuer,
        "isin": isin,
        "ccy": "",
        "ter_bps": "",
        "aum_mn": "",
        "aum_ccy": "",
        "date": "",
        "nav": "",
        "nav_date": "",
        "distribution_type": "",
        "fund_type": "UCITS ETF",
        "fund_domicile": "",
        "index_name": "",
        "index_ticker": "",
        "primary_ticker": "",
        "share_class": "",
        "management_company": "",
        "product_range": "",
        "objective": "",
        "factsheet_url": "",
        "product_page_url": "",
        "kiid_url": "",
        "launch_date": "",
        "investment_manager": "",
        "administrator_and_custodian": "",
        "bbg": "",
        "reuters": "",
        "lipper": "",
        "fetch_status": "not_found",
    }


def derive_share_class_and_distribution(name: str) -> tuple[str, str]:
    match = re.search(
        r"(?:(?P<share>[A-Z]{3}\s*\((?P<dist1>Acc|Dist)\))|(?P<short>\((?P<dist2>Acc\.?|Dist\.?)\)))\s*$",
        clean_text(name),
        flags=re.IGNORECASE,
    )
    if not match:
        return "", ""
    share_class = clean_text(match.group("share") or match.group("short"))
    distribution_token = clean_text(match.group("dist1") or match.group("dist2")).lower().replace(".", "")
    distribution = "Accumulating" if distribution_token == "acc" else "Distributing"
    return share_class, distribution


def soup_text_by_testid(soup: BeautifulSoup, testid: str) -> str:
    node = soup.select_one(f'[data-testid="{testid}"]')
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def parse_dimensional_fallback_row(isin: str, fallback_reason: str) -> dict[str, Any]:
    row = build_missing_row(isin, DIMENSIONAL_ISSUER)
    row["product_range"] = "Dimensional UCITS ETFs"
    row["fund_type"] = "UCITS ETF"
    row["product_page_url"] = DIMENSIONAL_FALLBACK_PROFILE_URL.format(isin=isin)
    row["source_kind"] = "fallback_public_profile"
    row["official_listing_url"] = DIMENSIONAL_LISTING_URL
    row["fallback_reason"] = fallback_reason or "official_api_unavailable"

    soup = BeautifulSoup(fetch_html(row["product_page_url"]), "html.parser")
    page_title = clean_text(soup.title.string if soup.title else "")
    if isin not in page_title and isin not in soup.get_text(" ", strip=True):
        row["fetch_status"] = "not_found"
        return row

    etf_name = soup_text_by_testid(soup, "etf-profile-header_etf-name")
    share_class, distribution = derive_share_class_and_distribution(etf_name)
    ter_text = soup_text_by_testid(soup, "tl_etf-basics_value_ter")
    fund_size_text = soup_text_by_testid(soup, "etf-profile-header_fund-size-value-wrapper")
    launch_date = parse_date(soup_text_by_testid(soup, "etf-profile-header_inception-date-value"))

    row.update(
        {
            "etf_name": etf_name,
            "isin": normalize_isin(soup_text_by_testid(soup, "etf-profile-header_isin-value")) or isin,
            "ccy": clean_text(soup_text_by_testid(soup, "tl_etf-basics_value_fund-currency")).upper(),
            "ter_bps": parse_percent_to_bps(ter_text),
            "aum_mn": parse_amount_to_millions(fund_size_text),
            "aum_ccy": "EUR" if ("EUR" in fund_size_text and parse_amount_to_millions(fund_size_text)) else "",
            "date": timestamp_now().strftime("%Y-%m-%d"),
            "distribution_type": distribution or soup_text_by_testid(soup, "etf-profile-header_distribution-policy-value"),
            "fund_domicile": soup_text_by_testid(soup, "tl_etf-basics_value_domicile-country"),
            "primary_ticker": soup_text_by_testid(soup, "etf-profile-header_identifier-value-ticker"),
            "share_class": share_class,
            "management_company": soup_text_by_testid(soup, "tl_etf-basics_value_fund-provider"),
            "objective": soup_text_by_testid(soup, "tl_etf-basics_value_strategy-risk"),
            "launch_date": launch_date,
            "investment_manager": DIMENSIONAL_ISSUER,
            "bbg": soup_text_by_testid(soup, "etf-profile-header_identifier-value-wkn"),
            "index_name": "",
            "nav": "",
            "nav_date": "",
            "replication": soup_text_by_testid(soup, "etf-profile-header_replication-value"),
            "replication_method": soup_text_by_testid(soup, "tl_etf-basics_value_replication-method"),
            "raw_profile_title": page_title,
        }
    )
    row["fetch_status"] = "ok"
    return row


def clean_html_text(value: object | None) -> str:
    if value is None:
        return ""
    return clean_text(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True))


def extract_identifier_value(meta: dict[str, Any], slug: str) -> str:
    for identifier in meta.get("identifiers", []) or []:
        if clean_text(identifier.get("slug")).lower() == slug.lower():
            return clean_text(identifier.get("value"))
    primary_identifier = meta.get("primaryIdentifier") or {}
    if clean_text(primary_identifier.get("slug")).lower() == slug.lower():
        return clean_text(primary_identifier.get("value"))
    return ""


def extract_dimensional_fee_display(fees: list[dict[str, Any]], slug: str) -> str:
    for fee in fees or []:
        if clean_text(fee.get("slug")).lower() != slug.lower():
            continue
        fee_value = fee.get("value") or {}
        return clean_text(fee_value.get("display"))
    return ""


def extract_latest_dimensional_nav(prices: list[dict[str, Any]]) -> tuple[str, str]:
    for price in prices or []:
        nav = price.get("nav") or {}
        date_value = price.get("date") or {}
        nav_display = clean_text(nav.get("display"))
        nav_date = clean_text(date_value.get("value"))
        if nav_display and nav_date:
            return parse_nav_value(nav_display), parse_date(nav_date)
    return "", ""


def slugify_segment(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^\w]+", "-", clean_text(value).lower())).strip("-")


def build_dimensional_product_url(portfolio_number: int | str, marketing_name: str) -> str:
    slug = slugify_segment(marketing_name)
    return f"https://www.dimensional.com/gb-en/funds/{portfolio_number}/{slug}"


def select_dimensional_document_url(documents: list[dict[str, Any]], preferred_labels: tuple[str, ...]) -> str:
    normalized_labels = tuple(label.lower() for label in preferred_labels)
    for document in documents or []:
        content_type = clean_text(document.get("ContentType")).lower()
        if any(label in content_type for label in normalized_labels):
            return clean_text(document.get("Url"))
    return ""


def fetch_dimensional_portfolios() -> dict[str, dict[str, Any]]:
    payload = fetch_json(
        DIMENSIONAL_FUND_CENTER_API_URL,
        headers={"Accept": "application/json", "x-selected-country": DIMENSIONAL_SELECTED_COUNTRY},
    )
    rows_by_isin: dict[str, dict[str, Any]] = {}
    for portfolio in payload.get("data", {}).get("portfolios", []) or []:
        meta = portfolio.get("meta") or {}
        if not bool(meta.get("isDfaUcitsEtf")):
            continue
        isin = normalize_isin(extract_identifier_value(meta, "isin"))
        if isin in DIMENSIONAL_TARGET_ISINS:
            rows_by_isin[isin] = portfolio
    return rows_by_isin


def fetch_dimensional_portfolio_details(portfolio_number: int | str) -> dict[str, Any]:
    return fetch_json(
        DIMENSIONAL_PORTFOLIO_DETAILS_API_URL,
        method="POST",
        headers={"Accept": "application/json", "x-selected-country": DIMENSIONAL_SELECTED_COUNTRY},
        json_payload={
            "portfolioNumber": int(portfolio_number),
            "regioncode": DIMENSIONAL_REGION_CODE,
            "countrycode": DIMENSIONAL_COUNTRY_CODE,
            "languagecode": DIMENSIONAL_LANGUAGE_CODE,
        },
    )


def fetch_dimensional_fund_detail(portfolio_number: int | str) -> dict[str, Any]:
    return fetch_json(
        DIMENSIONAL_FUND_DETAIL_API_URL,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-selected-country": DIMENSIONAL_SELECTED_COUNTRY,
        },
        json_payload={"portfolioNumber": str(portfolio_number)},
    )


def extract_dimensional_fund_aum(fund_detail: dict[str, Any]) -> tuple[str, str, str]:
    lens_groups = fund_detail.get("data", {}).get("lensGroups", []) or []
    for group in lens_groups:
        group_data = group.get("data") or {}
        if clean_text(group_data.get("slug")).lower() != "summary":
            continue
        for lens in group_data.get("lenses", []) or []:
            lens_data = lens.get("data") or {}
            if clean_text(lens_data.get("slug")).lower() != "fundfacts":
                continue
            blends = lens_data.get("blends", []) or []
            if not blends:
                continue
            blend_data = blends[0].get("data") or {}
            fund_facts = blend_data.get("fundFacts") or {}
            fund_aum = fund_facts.get("fundAum") or {}
            aum_value = fund_aum.get("aum") or {}
            aum_date = fund_aum.get("date") or {}
            aum_numeric = aum_value.get("value")
            if aum_numeric in (None, ""):
                return "", clean_text(fund_aum.get("dfaCurrencyCode")).upper(), parse_date(aum_date.get("value"))
            try:
                aum_decimal = Decimal(str(aum_numeric))
            except InvalidOperation:
                return "", clean_text(fund_aum.get("dfaCurrencyCode")).upper(), parse_date(aum_date.get("value"))
            return (
                format_decimal(aum_decimal / Decimal("1000000"), places=2),
                clean_text(fund_aum.get("dfaCurrencyCode")).upper(),
                parse_date(aum_date.get("value")),
            )
    return "", "", ""


def parse_dimensional_official_row(isin: str, portfolio: dict[str, Any]) -> dict[str, Any]:
    row = build_missing_row(isin, DIMENSIONAL_ISSUER)
    row["product_range"] = "Dimensional UCITS ETFs"
    row["fund_type"] = "UCITS ETF"
    row["source_kind"] = "official_fundcenter_api"
    row["official_listing_url"] = DIMENSIONAL_LISTING_URL

    meta = portfolio.get("meta") or {}
    fees = portfolio.get("fees") or []
    prices = portfolio.get("prices") or []
    portfolio_number = portfolio.get("portfolioNumber")

    marketing_name = clean_text(meta.get("marketingName"))
    share_class, distribution = derive_share_class_and_distribution(marketing_name)
    nav_value, nav_date = extract_latest_dimensional_nav(prices)
    ter_text = extract_dimensional_fee_display(fees, "net-exp-ratio")
    management_fee_text = extract_dimensional_fee_display(fees, "management-fee")

    objective = ""
    factsheet_url = ""
    kiid_url = ""
    aum_mn = ""
    aum_ccy = ""
    aum_date = ""
    try:
        fund_detail = fetch_dimensional_fund_detail(portfolio_number)
        aum_mn, aum_ccy, aum_date = extract_dimensional_fund_aum(fund_detail)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Dimensional funddetail lookup failed for %s (%s): %s", isin, portfolio_number, exc)
    try:
        details = fetch_dimensional_portfolio_details(portfolio_number)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Dimensional details lookup failed for %s (%s): %s", isin, portfolio_number, exc)
        details = {}

    documents = details.get("documents") or []
    if details and not details.get("error"):
        objective = clean_html_text(details.get("objective") or details.get("meta"))
        factsheet_url = select_dimensional_document_url(documents, ("factsheet",))
        kiid_url = select_dimensional_document_url(documents, ("priips kid - english", "ucits kiid - english", "kid"))

    row.update(
        {
            "etf_name": marketing_name,
            "isin": normalize_isin(extract_identifier_value(meta, "isin")) or isin,
            "ccy": clean_text(meta.get("dfaCurrencyCode")).upper(),
            "ter_bps": parse_percent_to_bps(ter_text),
            "aum_mn": aum_mn,
            "aum_ccy": aum_ccy,
            "date": nav_date,
            "nav": nav_value,
            "nav_date": nav_date,
            "distribution_type": distribution,
            "fund_domicile": "",
            "index_name": "",
            "index_ticker": "",
            "primary_ticker": extract_identifier_value(meta, "ticker"),
            "share_class": share_class,
            "management_company": DIMENSIONAL_ISSUER,
            "objective": objective,
            "factsheet_url": factsheet_url,
            "product_page_url": build_dimensional_product_url(portfolio_number, marketing_name),
            "kiid_url": kiid_url,
            "launch_date": parse_date((meta.get("inceptionDate") or {}).get("value")),
            "investment_manager": DIMENSIONAL_ISSUER,
            "administrator_and_custodian": "",
            "bbg": "",
            "reuters": "",
            "lipper": "",
            "management_fee_bps": parse_percent_to_bps(management_fee_text),
            "official_portfolio_number": clean_text(portfolio_number),
            "official_country_context": DIMENSIONAL_SELECTED_COUNTRY,
            "aum_date": aum_date,
        }
    )
    row["fetch_status"] = "ok" if row["etf_name"] and row["isin"] else "partial"
    return row


def extract_expat_tables(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    listing_data: dict[str, str] = {}
    summary_data: dict[str, str] = {}
    yield_data: dict[str, str] = {}

    tables = soup.find_all("table")
    if len(tables) >= 1:
        listing_data = {
            "primary_ticker": "BGX",
            "bbg": "BGX BU; BGX GY; BGX LN",
            "reuters": "BGXG.DE",
        }
    if len(tables) >= 2:
        rows = tables[1].find_all("tr")
        for row in rows:
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            if len(cells) == 1 and cells[0].startswith("Summary as of"):
                summary_data["summary_date"] = clean_text(cells[0].replace("Summary as of", ""))
            elif len(cells) >= 2:
                summary_data[cells[0]] = cells[1]
    if len(tables) >= 5:
        rows = tables[4].find_all("tr")
        for row in rows:
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            if len(cells) >= 2:
                yield_data[cells[0]] = cells[1]
    return listing_data, summary_data, yield_data


def extract_expat_links(soup: BeautifulSoup) -> dict[str, str]:
    links: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True))
        href = clean_text(anchor["href"])
        lower_label = label.lower()
        if "key investors information" in lower_label and not links.get("kiid_url"):
            links["kiid_url"] = href
        elif "prospectus" in lower_label and not links.get("prospectus_url"):
            links["prospectus_url"] = href
        elif "portfolio structure" in lower_label and not links.get("factsheet_url"):
            links["factsheet_url"] = href
    return links


def extract_expat_objective(page_text: str) -> str:
    match = re.search(
        r"The objective of the Expat Bulgaria SOFIX UCITS ETF is to track the performance of the SOFIX Index.*?(?=Intended Retail Investor:|Purchase or Redemption Orders:|$)",
        page_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return clean_text(match.group(0)) if match else ""


def extract_expat_management_company(page_text: str) -> str:
    match = re.search(
        r'Fund Manager:\s*Management company[ “"]+([^"\n]+?)["”]\s*EAD',
        page_text,
        flags=re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1) + " EAD")
    return ""


def extract_expat_ongoing_cost_bps(page_text: str) -> str:
    match = re.search(
        r"Management fees or other administrative\s+or operating costs\s+Up to\s+([0-9]+(?:[.,][0-9]+)?)%",
        page_text,
        flags=re.IGNORECASE,
    )
    return parse_percent_to_bps(match.group(1) + "%") if match else ""


def extract_expat_launch_date(html_text: str) -> str:
    match = re.search(
        r"listed on the BSE on ([A-Za-z]+ \d{1,2}, \d{4})",
        html_text,
        flags=re.IGNORECASE,
    )
    return parse_date(match.group(1)) if match else ""


def parse_expat_row() -> dict[str, Any]:
    row = build_missing_row("BG9000011163", EXPAT_ISSUER)
    row["product_page_url"] = EXPAT_FUND_URL
    row["product_range"] = "Expat UCITS ETFs"
    row["fund_type"] = "UCITS ETF"
    row["source_kind"] = "official_fund_page"

    html = fetch_html(EXPAT_FUND_URL)
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text("\n", strip=True)
    page_title = clean_text(soup.title.string if soup.title else "").replace("Expat | ", "")

    listing_data, summary_data, yield_data = extract_expat_tables(soup)
    links = extract_expat_links(soup)
    kid_text = fetch_pdf_text(links.get("kiid_url", "")) if links.get("kiid_url") else ""

    summary_date = parse_date(summary_data.get("summary_date"))
    nav_text = summary_data.get("Net asset value per share", "")
    total_nav_text = summary_data.get("Net asset value", "")

    row.update(
        {
            "etf_name": page_title,
            "isin": "BG9000011163",
            "ccy": "EUR",
            "ter_bps": extract_expat_ongoing_cost_bps(kid_text),
            "aum_mn": parse_amount_to_millions(total_nav_text),
            "aum_ccy": "EUR",
            "date": summary_date,
            "nav": parse_nav_value(nav_text),
            "nav_date": summary_date,
            "distribution_type": "Accumulating",
            "fund_domicile": "Bulgaria",
            "index_name": "SOFIX",
            "primary_ticker": listing_data.get("primary_ticker", "BGX"),
            "management_company": extract_expat_management_company(kid_text) or EXPAT_ISSUER,
            "objective": extract_expat_objective(kid_text) or extract_expat_objective(page_text),
            "factsheet_url": links.get("factsheet_url", ""),
            "kiid_url": links.get("kiid_url", ""),
            "launch_date": extract_expat_launch_date(html),
            "investment_manager": EXPAT_ISSUER,
            "bbg": listing_data.get("bbg", ""),
            "reuters": listing_data.get("reuters", ""),
            "yield_ytd": clean_text(yield_data.get("Since the beginning of the year")),
            "yield_previous_year": clean_text(yield_data.get("For the previous year")),
            "yield_since_public_offering_annualized": clean_text(
                yield_data.get("Since the beginning of the public offering (annualized)")
            ),
            "raw_summary": summary_data,
        }
    )
    row["fetch_status"] = "ok" if row["etf_name"] and row["nav"] else "partial"
    return row


def build_snapshot() -> dict[str, Any]:
    listing_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    dimensional_official_rows: dict[str, dict[str, Any]] = {}
    dimensional_fetch_status = "ok"
    try:
        dimensional_official_rows = fetch_dimensional_portfolios()
        logging.info(
            "Captured %d official Dimensional UCITS ETF rows from the fund center API.",
            len(dimensional_official_rows),
        )
    except Exception as exc:  # noqa: BLE001
        dimensional_fetch_status = f"error:{type(exc).__name__}"
        logging.warning("Failed to fetch official Dimensional fund center API: %s", exc)

    for index, isin in enumerate(DIMENSIONAL_TARGET_ISINS, start=1):
        logging.info("[%d/%d] Collecting Dimensional %s", index, len(DIMENSIONAL_TARGET_ISINS), isin)
        try:
            portfolio = dimensional_official_rows.get(isin)
            if portfolio:
                row = parse_dimensional_official_row(isin, portfolio)
            else:
                row = parse_dimensional_fallback_row(
                    isin,
                    fallback_reason="official_api_missing_target_isin",
                )
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to collect Dimensional %s: %s", isin, exc)
            try:
                row = parse_dimensional_fallback_row(
                    isin,
                    fallback_reason=f"official_row_error:{type(exc).__name__}",
                )
            except Exception as fallback_exc:  # noqa: BLE001
                logging.warning("Fallback profile also failed for Dimensional %s: %s", isin, fallback_exc)
                row = build_missing_row(isin, DIMENSIONAL_ISSUER)
                row["fetch_status"] = "error"
        listing_rows.append(row)
        status_counts[row["fetch_status"]] = status_counts.get(row["fetch_status"], 0) + 1

    logging.info("[1/1] Collecting Expat Bulgaria SOFIX BG9000011163")
    try:
        expat_row = parse_expat_row()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed to collect Expat Bulgaria SOFIX BG9000011163: %s", exc)
        expat_row = build_missing_row("BG9000011163", EXPAT_ISSUER)
        expat_row["fetch_status"] = "error"
    listing_rows.append(expat_row)
    status_counts[expat_row["fetch_status"]] = status_counts.get(expat_row["fetch_status"], 0) + 1

    matched_statuses = {"ok", "partial"}
    return {
        "source": {
            "provider": "Dimensional + Expat Bulgaria SOFIX",
            "dimensional_listing_url": DIMENSIONAL_LISTING_URL,
            "dimensional_fundcenter_api_url": DIMENSIONAL_FUND_CENTER_API_URL,
            "dimensional_portfolio_details_api_url": DIMENSIONAL_PORTFOLIO_DETAILS_API_URL,
            "dimensional_selected_country": DIMENSIONAL_SELECTED_COUNTRY,
            "dimensional_official_fetch_status": dimensional_fetch_status,
            "dimensional_fallback_profile_url_template": DIMENSIONAL_FALLBACK_PROFILE_URL,
            "expat_fund_url": EXPAT_FUND_URL,
        },
        "method": (
            "Combined ETF snapshot. For the 4 Dimensional UCITS ETF ISINs, the scraper uses the official Dimensional "
            "fund center API and enriches objective/documents from the official portfolio-details API. If an official "
            "target row is missing or errors, the scraper falls back to the ISIN-based public ETF profile page. For "
            "BG9000011163, the scraper uses the official Expat Bulgaria SOFIX fund page plus its current KID PDF."
        ),
        "captured_at": timestamp_now().isoformat(),
        "target_isin_count": len(TARGET_ISINS),
        "matched_target_isins": [row["isin"] for row in listing_rows if row["fetch_status"] in matched_statuses],
        "missing_target_isins": [row["isin"] for row in listing_rows if row["fetch_status"] not in matched_statuses],
        "status_counts": status_counts,
        "listing_rows": listing_rows,
    }


def download_snapshot(output_path: Path) -> Path:
    snapshot = build_snapshot()
    write_json(output_path, snapshot)
    logging.info("Dimensional/Expat fetch summary: %s", snapshot.get("status_counts", {}))
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved : %s", output_path)
    return output_path


async def download_dimensional_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def main() -> None:
    setup_logging()
    output_path = asyncio.run(download_dimensional_file())
    print(f"Saved Dimensional snapshot to: {output_path}")


if __name__ == "__main__":
    main()
