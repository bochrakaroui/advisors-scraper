"""Download Janus Henderson UK ETF data from official jhetf.com product pages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

try:
    from scrapers.tls_compat import session_get
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from tls_compat import session_get


ISSUER = "Janus Henderson"
PRODUCTS_PAGE_URL = "https://www.jhetf.com/products/"
SITEMAP_URL = "https://www.jhetf.com/wp-sitemap-posts-page-1.xml"
SEARCH_API_URL = "https://www.jhetf.com/wp-json/wp/v2/search"
ARCHIVE_CDX_URL = "https://web.archive.org/cdx/search/cdx"
REQUEST_TIMEOUT_S = 45
HTTP_RETRY_ATTEMPTS = 3
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

TARGET_ISINS = [
    "IE0002P9KZW1",
    "IE0007W7MZL0",
    "IE0008B0OAD5",
    "IE0008C3G0Y9",
    "IE0009ZTL4B5",
    "IE000CV0WWL4",
    "IE000GETKIK8",
    "IE000J8RGOJ4",
    "IE000JL9SV51",
    "IE000L1I4R94",
    "IE000LJG9WK1",
    "IE000LSFKN16",
    "IE000LZC9NM0",
    "IE000P7C7930",
    "IE000RH1ZG27",
    "IE000XIITCN5",
    "IE000Y3FZEN4",
    "IE000YMBL844",
    "IE00BMDWWS85",
    "IE00BN0T9H70",
    "LU2941599081",
    "LU2941599248",
    "LU2941599834",
    "LU2994520851",
    "LU2994520935",
    "LU2994521669",
]

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Janus_Henderson"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": PRODUCTS_PAGE_URL,
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SPACE_PATTERN = re.compile(r"\s+")
PRODUCT_URL_PATTERN = re.compile(
    r"https://www\.jhetf\.com/products/[^\"'<>\\\s]+/overview/?",
    re.IGNORECASE,
)
AS_OF_PATTERN = re.compile(r"\bas of\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
NON_VALUE_STRINGS = {"", "-", "--", "none", "null", "n/a"}
XML_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "janushenderson_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned.lower() in NON_VALUE_STRINGS else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def http_get(url: str, **kwargs: Any) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            return session_get(
                SESSION,
                url,
                logger=logging.getLogger(__name__),
                **kwargs,
            )
        except requests.exceptions.ConnectionError as exc:
            last_error = exc
            if attempt >= HTTP_RETRY_ATTEMPTS:
                break
            logging.warning(
                "GET %s failed on attempt %d/%d with a connection error; retrying: %s",
                url,
                attempt,
                HTTP_RETRY_ATTEMPTS,
                exc,
            )
        except requests.exceptions.ChunkedEncodingError as exc:
            last_error = exc
            if attempt >= HTTP_RETRY_ATTEMPTS:
                break
            logging.warning(
                "GET %s failed on attempt %d/%d with a chunked encoding error; retrying: %s",
                url,
                attempt,
                HTTP_RETRY_ATTEMPTS,
                exc,
            )

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"HTTP GET failed unexpectedly without an exception for {url}")


def strip_label_suffix(value: str) -> str:
    return clean_text(value).rstrip(":").strip()


def to_decimal(value: object | None) -> Decimal | None:
    cleaned = re.sub(r"[^0-9.\-]", "", clean_text(value))
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
    return str(int((decimal_value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def amount_text_to_millions(value: object | None) -> str:
    cleaned = clean_text(value).upper().replace(",", "")
    if not cleaned:
        return ""

    multiplier = Decimal("1")
    stripped = cleaned
    if stripped.endswith("BN"):
        multiplier = Decimal("1000")
        stripped = stripped[:-2]
    elif stripped.endswith("B"):
        multiplier = Decimal("1000")
        stripped = stripped[:-1]
    elif stripped.endswith("M"):
        multiplier = Decimal("1")
        stripped = stripped[:-1]
    elif stripped.endswith("K"):
        multiplier = Decimal("0.001")
        stripped = stripped[:-1]

    decimal_value = to_decimal(stripped)
    if decimal_value is None:
        return ""

    if stripped == cleaned:
        decimal_value = decimal_value / Decimal("1000000")
    else:
        decimal_value = decimal_value * multiplier
    return format_decimal(decimal_value, places=2)


def normalize_product_url(url: str) -> str:
    cleaned_url = clean_text(url)
    if not cleaned_url:
        return ""
    absolute_url = urljoin(PRODUCTS_PAGE_URL, cleaned_url)
    split_url = urlsplit(absolute_url)
    path = split_url.path.rstrip("/")
    if not path.endswith("/overview"):
        path = f"{path}/overview"
    path = f"{path}/"
    return urlunsplit((split_url.scheme.lower(), split_url.netloc.lower(), path, "", ""))


def extract_overview_urls(html: str) -> list[str]:
    return [normalize_product_url(match) for match in PRODUCT_URL_PATTERN.findall(html)]


def parse_product_path(url: str) -> list[str]:
    path_parts = [part for part in urlsplit(normalize_product_url(url)).path.strip("/").split("/") if part]
    if len(path_parts) < 3 or path_parts[0].lower() != "products" or path_parts[-1].lower() != "overview":
        return []
    return [normalize_isin(part) for part in path_parts[1:-1]]


def fetch_text(url: str) -> str:
    response = http_get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.content.decode("utf-8", errors="replace")


def build_wayback_raw_url(original_url: str, timestamp: str) -> str:
    return f"https://web.archive.org/web/{timestamp}if_/{clean_text(original_url)}"


def fetch_candidate_overview_urls() -> set[str]:
    candidate_urls: set[str] = set()

    products_html = fetch_text(PRODUCTS_PAGE_URL)
    candidate_urls.update(extract_overview_urls(products_html))

    sitemap_xml = fetch_text(SITEMAP_URL)
    root = ET.fromstring(sitemap_xml)
    for loc_node in root.findall(".//sm:loc", XML_NS):
        if loc_node.text:
            location = clean_text(loc_node.text)
            if "/products/" in location and "/overview" in location:
                candidate_urls.add(normalize_product_url(location))

    return candidate_urls


def resolve_target_urls(candidate_urls: set[str]) -> dict[str, str]:
    ranked_matches: dict[str, list[tuple[int, str]]] = {isin: [] for isin in TARGET_ISINS}

    for candidate_url in candidate_urls:
        product_path_isins = parse_product_path(candidate_url)
        if not product_path_isins:
            continue

        if len(product_path_isins) == 1:
            primary_isin = product_path_isins[0]
            if primary_isin in ranked_matches:
                ranked_matches[primary_isin].append((0, candidate_url))
            continue

        primary_isin = product_path_isins[0]
        secondary_isin = product_path_isins[1]
        if secondary_isin in ranked_matches:
            ranked_matches[secondary_isin].append((0, candidate_url))
        if primary_isin in ranked_matches:
            ranked_matches[primary_isin].append((1, candidate_url))

    resolved: dict[str, str] = {}
    for isin, matches in ranked_matches.items():
        if not matches:
            continue
        resolved[isin] = sorted(matches, key=lambda item: (item[0], item[1]))[0][1]
    return resolved


def parse_summary_stats(soup: BeautifulSoup) -> dict[str, str]:
    summary_stats: dict[str, str] = {}
    for stat_block in soup.select("div.tab-index-block"):
        label_node = stat_block.select_one(".index-label")
        value_node = stat_block.select_one(".index-desc")
        if label_node is None:
            continue
        label = strip_label_suffix(label_node.get_text(" ", strip=True))
        value = clean_text(value_node.get_text(" ", strip=True)) if value_node else ""
        if label and label not in summary_stats:
            summary_stats[label] = value
    return summary_stats


def parse_table_fields(soup: BeautifulSoup) -> dict[str, str]:
    table_fields: dict[str, str] = {}
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = strip_label_suffix(cells[0].get_text(" ", strip=True))
        value = clean_text(cells[1].get_text(" ", strip=True))
        if label and label not in table_fields:
            table_fields[label] = value
    return table_fields


def parse_data_as_of(soup: BeautifulSoup) -> str:
    note_node = soup.select_one("p.top-table-note")
    if note_node is None:
        return ""
    match = AS_OF_PATTERN.search(note_node.get_text(" ", strip=True))
    return match.group(1) if match else ""


def parse_factsheet_url(html: str, soup: BeautifulSoup) -> str:
    for link in soup.find_all("a", href=True):
        href = clean_text(link.get("href"))
        if href.lower().endswith(".pdf") and "factsheet" in href.lower():
            return href
    pdf_matches = re.findall(r"https://www\.jhetf\.com/[^\"'<>\\\s]+\.pdf", html, re.IGNORECASE)
    for match in pdf_matches:
        if "factsheet" in match.lower():
            return clean_text(match)
    return ""


# Override the legacy splitter so both current and mojibake title separators are handled.
def split_name_and_share_class(page_title: str) -> tuple[str, str]:
    cleaned_title = clean_text(page_title)
    if not cleaned_title:
        return "", ""

    if " – " in cleaned_title:
        fund_name, share_class_name = cleaned_title.split(" – ", 1)
    else:
        fund_name, share_class_name = cleaned_title, ""

    if fund_name.startswith(f"{ISSUER} "):
        fund_name = fund_name[len(ISSUER) + 1 :]
    return fund_name, share_class_name


def split_name_and_share_class(page_title: str) -> tuple[str, str]:
    cleaned_title = clean_text(page_title)
    if not cleaned_title:
        return "", ""

    fund_name = cleaned_title
    share_class_name = ""
    for separator in (" – ", " - ", " â€“ "):
        if separator in cleaned_title:
            fund_name, share_class_name = cleaned_title.split(separator, 1)
            break

    if fund_name.startswith(f"{ISSUER} "):
        fund_name = fund_name[len(ISSUER) + 1 :]
    return fund_name, share_class_name


def infer_ccy_from_share_class_text(share_class_name: str, fallback_title: str = "") -> str:
    share_class_text = clean_text(share_class_name)
    if share_class_text:
        token = share_class_text.split(" ", 1)[0].upper().rstrip(".")
        direct_match = re.fullmatch(r"([A-Z]{3})(?:\b|-.+)", token)
        if direct_match:
            return direct_match.group(1)
        leading_match = re.match(r"^([A-Z]{3})(?:\b|[-/])", share_class_text.upper())
        if leading_match:
            return leading_match.group(1)

    title_match = re.search(r"\(([A-Z]{3})\)", clean_text(fallback_title))
    if title_match:
        return title_match.group(1)
    return ""


def parse_products_listing(products_html: str) -> dict[str, dict[str, str]]:
    soup = BeautifulSoup(products_html, "html.parser")
    listing_index: dict[str, dict[str, str]] = {}

    for row in soup.select("tr.clickable-row"):
        row_title = clean_text(row.get("data-product-master-title"))
        link_node = row.select_one("a.fund-title-link")
        if not row_title:
            row_title = clean_text(link_node.get_text(" ", strip=True)) if link_node else ""

        fund_name, row_share_class_name = split_name_and_share_class(row_title)
        base_url = normalize_product_url(row.get("data-href") or (link_node.get("href") if link_node else ""))
        base_ticker = clean_text((row.select_one("td.ticker") or {}).get_text(" ", strip=True) if row.select_one("td.ticker") else "").upper()
        base_charges = clean_text((row.select_one("td.charges") or {}).get_text(" ", strip=True) if row.select_one("td.charges") else "")
        base_aum_display = clean_text((row.select_one("td.aum-currency") or {}).get_text(" ", strip=True) if row.select_one("td.aum-currency") else "")

        option_nodes = row.select("select.alternative-fund-select option")
        if option_nodes:
            for option_node in option_nodes:
                option_url = clean_text(option_node.get("data-href") or option_node.get("value"))
                if not option_url:
                    continue
                normalized_option_url = normalize_product_url(option_url)
                product_path_isins = parse_product_path(normalized_option_url)
                if not product_path_isins:
                    continue

                isin = product_path_isins[1] if len(product_path_isins) >= 2 else product_path_isins[0]
                share_class_name = clean_text(option_node.get_text(" ", strip=True))
                aum_mn = clean_text(option_node.get("data-aum"))
                currency_symbol = clean_text(option_node.get("data-currency"))
                listing_index[isin] = {
                    "etf_name": fund_name or row_title,
                    "page_title": row_title,
                    "share_class_name": share_class_name,
                    "ticker": clean_text(option_node.get("data-ticker") or base_ticker).upper(),
                    "ter_percent": clean_text(option_node.get("data-charges") or base_charges),
                    "ter_bps": percent_to_bps(option_node.get("data-charges") or base_charges),
                    "aum_display": f"{currency_symbol}{aum_mn}" if currency_symbol and aum_mn else base_aum_display,
                    "aum_mn": aum_mn,
                    "ccy": infer_ccy_from_share_class_text(share_class_name, row_title),
                    "detail_url": normalized_option_url,
                }
            continue

        product_path_isins = parse_product_path(base_url)
        if not product_path_isins:
            continue
        isin = product_path_isins[0]
        listing_index[isin] = {
            "etf_name": fund_name or row_title,
            "page_title": row_title,
            "share_class_name": row_share_class_name,
            "ticker": base_ticker,
            "ter_percent": base_charges,
            "ter_bps": percent_to_bps(base_charges),
            "aum_display": base_aum_display,
            "aum_mn": amount_text_to_millions(base_aum_display),
            "ccy": infer_ccy_from_share_class_text(row_share_class_name, row_title),
            "detail_url": base_url,
        }

    return listing_index


def merge_non_empty(base_row: dict[str, str], detail_row: dict[str, str]) -> dict[str, str]:
    merged_row = dict(base_row)
    for key, value in detail_row.items():
        if clean_text(value):
            merged_row[key] = value
    return merged_row


def dedupe_product_urls(candidate_urls: list[str]) -> list[str]:
    deduped_urls: list[str] = []
    seen_urls: set[str] = set()
    for candidate in candidate_urls:
        normalized_candidate = normalize_product_url(candidate)
        if not normalized_candidate or normalized_candidate in seen_urls:
            continue
        seen_urls.add(normalized_candidate)
        deduped_urls.append(normalized_candidate)
    return deduped_urls


def extract_reference_product_urls(reference_payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(reference_payload, dict):
        return []

    candidate_urls: list[str] = []
    for link in reference_payload.get("legacy_product_links", []):
        if isinstance(link, dict):
            candidate_urls.append(clean_text(link.get("url")))
    return dedupe_product_urls(candidate_urls)


def row_has_required_listing_fields(row: dict[str, str]) -> bool:
    required_keys = ("etf_name", "isin", "ccy", "ticker", "ter_bps", "aum_mn")
    return all(clean_text(row.get(key)) for key in required_keys)


def fetch_archive_listing_index(target_isins: set[str]) -> tuple[dict[str, dict[str, str]], str]:
    if not target_isins:
        return {}, ""

    try:
        response = http_get(
            ARCHIVE_CDX_URL,
            params={
                "url": PRODUCTS_PAGE_URL,
                "output": "json",
                "fl": "timestamp,original,statuscode",
                "filter": "statuscode:200",
                "from": 2025,
                "limit": 20,
            },
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Janus Henderson archive listing index fetch failed, continuing without it: %s", exc)
        return {}, ""

    if not isinstance(payload, list) or len(payload) <= 1:
        return {}, ""

    for row in reversed(payload[1:]):
        if not isinstance(row, list) or not row:
            continue
        snapshot_timestamp = clean_text(row[0])
        if not snapshot_timestamp:
            continue
        archive_url = build_wayback_raw_url(PRODUCTS_PAGE_URL, snapshot_timestamp)
        try:
            archive_html = fetch_text(archive_url)
        except Exception:  # noqa: BLE001
            continue

        archive_listing_index = parse_products_listing(archive_html)
        if target_isins.intersection(archive_listing_index):
            return archive_listing_index, snapshot_timestamp

    return {}, ""


def fetch_archived_product_page(
    requested_isin: str,
    candidate_urls: list[str],
) -> tuple[dict[str, str] | None, dict[str, str]]:
    for original_url in candidate_urls:
        cleaned_url = normalize_product_url(original_url)
        if not cleaned_url:
            continue

        try:
            response = http_get(
                ARCHIVE_CDX_URL,
                params={
                    "url": cleaned_url,
                    "output": "json",
                    "fl": "timestamp,original,statuscode",
                    "filter": "statuscode:200",
                    "from": 2025,
                    "limit": 20,
                },
                timeout=REQUEST_TIMEOUT_S,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001
            continue

        if not isinstance(payload, list) or len(payload) <= 1:
            continue

        for row in reversed(payload[1:]):
            if not isinstance(row, list) or not row:
                continue
            snapshot_timestamp = clean_text(row[0])
            if not snapshot_timestamp:
                continue

            archive_request_url = build_wayback_raw_url(cleaned_url, snapshot_timestamp)
            try:
                archive_detail_row = scrape_product_page(
                    requested_isin,
                    cleaned_url,
                    request_url=archive_request_url,
                )
                return archive_detail_row, {
                    "archive_snapshot_timestamp": snapshot_timestamp,
                    "archive_request_url": archive_request_url,
                }
            except Exception:  # noqa: BLE001
                continue

    return None, {}


def scrape_product_page(
    requested_isin: str,
    detail_url: str,
    request_url: str | None = None,
) -> dict[str, str]:
    response = http_get(request_url or detail_url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()

    if request_url and "web.archive.org/web/" in request_url:
        final_url = normalize_product_url(detail_url)
    else:
        final_url = normalize_product_url(response.url)
    html = response.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    page_title = clean_text(next((node.get_text(" ", strip=True) for node in soup.find_all("h1") if clean_text(node.get_text(" ", strip=True))), ""))
    fund_name, share_class_name = split_name_and_share_class(page_title)
    summary_stats = parse_summary_stats(soup)
    table_fields = parse_table_fields(soup)
    data_as_of = parse_data_as_of(soup)
    factsheet_url = parse_factsheet_url(html, soup)

    scraped_isin = normalize_isin(table_fields.get("ISIN"))
    path_isins = set(parse_product_path(final_url))
    if scraped_isin and scraped_isin != requested_isin:
        raise ValueError(
            f"Requested ISIN {requested_isin} resolved to a different page ISIN {scraped_isin}: {final_url}"
        )
    if requested_isin not in path_isins and scraped_isin != requested_isin:
        raise ValueError(
            f"Requested ISIN {requested_isin} redirected to a non-product or generic page: {final_url}"
        )
    scraped_isin = scraped_isin or requested_isin

    ter_percent = clean_text(table_fields.get("TER")) or clean_text(summary_stats.get("Ongoing charges"))
    aum_display = clean_text(summary_stats.get("AuM"))
    ticker = (
        clean_text(table_fields.get("Primary ticker"))
        or clean_text(summary_stats.get("Ticker"))
    ).upper()
    ccy = (
        clean_text(table_fields.get("Share class currency"))
        or clean_text(table_fields.get("Listing currency"))
        or clean_text(table_fields.get("Base currency"))
    ).upper()

    return {
        "etf_name": fund_name,
        "issuer": ISSUER,
        "isin": scraped_isin,
        "ccy": ccy,
        "base_currency": clean_text(table_fields.get("Base currency")).upper(),
        "listing_currency": clean_text(table_fields.get("Listing currency")).upper(),
        "ticker": ticker,
        "ter_percent": ter_percent,
        "ter_bps": percent_to_bps(ter_percent),
        "ongoing_charge_percent": clean_text(summary_stats.get("Ongoing charges")),
        "aum_display": aum_display,
        "aum_mn": amount_text_to_millions(aum_display),
        "date": data_as_of,
        "data_as_of": data_as_of,
        "page_title": page_title,
        "share_class_name": share_class_name,
        "income_treatment": clean_text(table_fields.get("Income treatment")),
        "domicile": clean_text(table_fields.get("Domicile")),
        "fund_inception": clean_text(table_fields.get("Fund inception")),
        "share_class_inception": clean_text(table_fields.get("Share class inception")),
        "factsheet_url": factsheet_url,
        "detail_url": final_url,
    }


def search_reference_pages(isin: str) -> dict[str, Any]:
    try:
        response = http_get(
            SEARCH_API_URL,
            params={"search": isin, "per_page": 20},
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"search_results": [], "legacy_product_links": [], "error": str(exc)}

    if not isinstance(payload, list):
        return {"search_results": [], "legacy_product_links": [], "error": "Unexpected search payload."}

    legacy_links: list[dict[str, str]] = []
    search_results: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        result_url = clean_text(item.get("url"))
        result_title = clean_text(item.get("title"))
        if not result_url:
            continue
        search_results.append({"title": result_title, "url": result_url})
        try:
            html = fetch_text(result_url)
        except Exception:  # noqa: BLE001
            continue

        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = clean_text(link.get("href"))
            if isin.lower() in href.lower() and "/products/" in href.lower():
                legacy_links.append(
                    {
                        "url": href,
                        "text": clean_text(link.get_text(" ", strip=True)),
                        "source_page": result_url,
                    }
                )

    deduped_legacy_links: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for link in legacy_links:
        url = clean_text(link.get("url"))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped_legacy_links.append(link)

    return {
        "search_results": search_results,
        "legacy_product_links": deduped_legacy_links,
    }


def build_snapshot(now: datetime) -> dict[str, Any]:
    logging.info("Loaded %d Janus Henderson target ISINs from the scraper constant list.", len(TARGET_ISINS))
    products_html = fetch_text(PRODUCTS_PAGE_URL)
    listing_index = parse_products_listing(products_html)
    archive_listing_index, archive_snapshot_timestamp = fetch_archive_listing_index(
        set(TARGET_ISINS) - set(listing_index)
    )
    candidate_urls = set(extract_overview_urls(products_html))
    try:
        candidate_urls.update(fetch_candidate_overview_urls())
    except Exception as exc:  # noqa: BLE001
        logging.warning("Janus Henderson sitemap fetch failed, using products page links only: %s", exc)
    resolved_urls = resolve_target_urls(candidate_urls)

    listing_rows: list[dict[str, str]] = []
    target_results: list[dict[str, Any]] = []

    for index, isin in enumerate(TARGET_ISINS, start=1):
        logging.info("[%d/%d] Fetching %s", index, len(TARGET_ISINS), isin)
        row_index = listing_index.get(isin, {})
        archive_row_index = archive_listing_index.get(isin, {})
        base_source = row_index if row_index else archive_row_index
        base_row = {
            "etf_name": clean_text(base_source.get("etf_name")),
            "issuer": ISSUER,
            "isin": isin,
            "ccy": clean_text(base_source.get("ccy")).upper(),
            "base_currency": "",
            "listing_currency": "",
            "ticker": clean_text(base_source.get("ticker")).upper(),
            "ter_percent": clean_text(base_source.get("ter_percent")),
            "ter_bps": clean_text(base_source.get("ter_bps")),
            "ongoing_charge_percent": clean_text(base_source.get("ter_percent")),
            "aum_display": clean_text(base_source.get("aum_display")),
            "aum_mn": clean_text(base_source.get("aum_mn")),
            "date": "",
            "data_as_of": "",
            "page_title": clean_text(base_source.get("page_title")),
            "share_class_name": clean_text(base_source.get("share_class_name")),
            "income_treatment": "",
            "domicile": "",
            "fund_inception": "",
            "share_class_inception": "",
            "factsheet_url": "",
            "detail_url": clean_text(base_source.get("detail_url")),
        }
        detail_url = resolved_urls.get(isin) or base_row.get("detail_url", "")
        if detail_url:
            base_row["detail_url"] = detail_url

        if not detail_url and not clean_text(base_row.get("etf_name")):
            target_results.append(
                {
                    "isin": isin,
                    "status": "unresolved",
                    "detail_url": "",
                    "references": search_reference_pages(isin),
                }
            )
            logging.warning("Janus Henderson ISIN %s could not be resolved to a current product page", isin)
            continue

        row = dict(base_row)
        status = "listing_only" if clean_text(base_row.get("etf_name")) else "unresolved"
        references: dict[str, Any] | None = None
        if detail_url:
            try:
                detail_row = scrape_product_page(isin, detail_url)
                row = merge_non_empty(base_row, detail_row)
                status = "ok"
            except Exception as exc:  # noqa: BLE001
                archive_candidate_urls = dedupe_product_urls([
                    candidate
                    for candidate in [
                        clean_text(row_index.get("detail_url")),
                        clean_text(archive_row_index.get("detail_url")),
                        detail_url,
                    ]
                    if candidate
                ])
                reference_search_payload: dict[str, Any] | None = None
                archive_detail_row: dict[str, str] | None = None
                archive_metadata: dict[str, str] = {}

                if archive_candidate_urls:
                    logging.info(
                        "Live Janus Henderson page unavailable for %s, using archived official page fallback",
                        isin,
                    )
                    archive_detail_row, archive_metadata = fetch_archived_product_page(
                        isin,
                        archive_candidate_urls,
                    )
                if archive_detail_row is None:
                    reference_search_payload = search_reference_pages(isin)
                    reference_candidate_urls = extract_reference_product_urls(reference_search_payload)
                    if reference_candidate_urls:
                        supplemental_archive_candidate_urls = dedupe_product_urls(
                            archive_candidate_urls + reference_candidate_urls
                        )
                        if supplemental_archive_candidate_urls != archive_candidate_urls:
                            logging.info(
                                "Trying Janus Henderson search-discovered legacy product links for %s",
                                isin,
                            )
                            archive_detail_row, archive_metadata = fetch_archived_product_page(
                                isin,
                                supplemental_archive_candidate_urls,
                            )

                if archive_detail_row is not None:
                    row = merge_non_empty(base_row, archive_detail_row)
                    status = "archive_ok"
                    references = {
                        "live_page_error": str(exc),
                        **archive_metadata,
                    }
                    if reference_search_payload and clean_text(reference_search_payload.get("error")):
                        references["reference_search_error"] = clean_text(reference_search_payload.get("error"))
                else:
                    archive_error_message = (
                        "No archived Janus Henderson detail page succeeded for the candidate URLs."
                    )
                    if status == "unresolved":
                        references = reference_search_payload or search_reference_pages(isin)
                        logging.warning(
                            "Janus Henderson archive fallback failed for %s: %s",
                            isin,
                            archive_error_message,
                        )
                        target_results.append(
                            {
                                "isin": isin,
                                "status": "error",
                                "detail_url": detail_url,
                                "error": str(exc),
                                "archive_error": archive_error_message,
                                "references": references,
                            }
                        )
                        continue

                    references = {
                        "page_error": str(exc),
                        "archive_error": archive_error_message,
                    }
                    if reference_search_payload:
                        references["search_results"] = reference_search_payload.get("search_results", [])
                        references["legacy_product_links"] = reference_search_payload.get(
                            "legacy_product_links", []
                        )
                        if clean_text(reference_search_payload.get("error")):
                            references["reference_search_error"] = clean_text(
                                reference_search_payload.get("error")
                            )

                    if row_has_required_listing_fields(row):
                        logging.info(
                            "Janus Henderson detail page remained unavailable for %s; keeping official listing data",
                            isin,
                        )
                    else:
                        logging.warning(
                            "Janus Henderson archive fallback failed for %s: %s",
                            isin,
                            archive_error_message,
                        )

        listing_rows.append(row)
        target_results.append(
            {
                "isin": isin,
                "status": status,
                "detail_url": row.get("detail_url", detail_url),
                "factsheet_url": row.get("factsheet_url", ""),
                "references": references or {},
            }
        )

    successful_statuses = {"ok", "archive_ok"}
    usable_statuses = successful_statuses | {"listing_only"}
    missing_target_isins = [
        result["isin"] for result in target_results if result.get("status") not in usable_statuses
    ]
    successful_scrape_count = sum(
        1 for result in target_results if result.get("status") in successful_statuses
    )
    listing_only_count = sum(1 for result in target_results if result.get("status") == "listing_only")

    return {
        "source": {
            "provider": ISSUER,
            "page_url": PRODUCTS_PAGE_URL,
            "service_url": SITEMAP_URL,
            "country": "gb",
            "language": "en",
        },
        "method": (
            "Explicit Janus Henderson target ISIN list + official jhetf.com ETF overview pages "
            "+ archived official Janus pages for retired share classes when live pages are unavailable"
        ),
        "captured_at": now.isoformat(),
        "requested_target_isin_count": len(TARGET_ISINS),
        "resolved_target_isin_count": len(resolved_urls),
        "archive_resolved_target_isin_count": len(archive_listing_index),
        "archive_products_snapshot_timestamp": archive_snapshot_timestamp,
        "successful_scrape_count": successful_scrape_count,
        "listing_only_count": listing_only_count,
        "output_row_count": len(listing_rows),
        "missing_target_isins": missing_target_isins,
        "candidate_overview_url_count": len(candidate_urls),
        "target_isins": TARGET_ISINS,
        "target_results": target_results,
        "listing_rows": listing_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now = timestamp_now()
    snapshot = build_snapshot(now)
    write_json(destination, snapshot)
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved: %s", destination)


async def download_janus_henderson_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def main() -> None:
    output_path = build_output_path(timestamp_now())
    download_snapshot(output_path)


if __name__ == "__main__":
    main()
