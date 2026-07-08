"""Scrape Market Access ETF data from official Market Access product pages.

Output: providers/Market_Access/YYYY-MM-DD/market_access_etf_export.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
        "beautifulsoup4 is required for the Market Access scraper. "
        "Install it with 'pip install beautifulsoup4'."
    ) from exc

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover - runtime guidance for local usage
    sync_playwright = None  # type: ignore[assignment]


ISSUER = "Market Access"
BASE_URL = "https://www.marketaccessetf.com"
ABOUT_URL = f"{BASE_URL}/Home/MAAboutUs?clientType=0&cc=gb"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
REQUEST_TIMEOUT_S = 45

TARGET_ISIN_URLS = {
    "LU0249326488": f"{BASE_URL}/Products/MAETFsDetail?ISIN=LU0249326488&clientType=0&CC=gb",
    "LU0259322260": f"{BASE_URL}/Products/MAETFsDetail?ISIN=LU0259322260&clientType=0&CC=gb",
    "LU1750178011": f"{BASE_URL}/Products/MAETFsDetail?ISIN=LU1750178011&clientType=0&CC=gb",
}
SECTION_STOPS = (
    "Fund Information",
    "Performance as of",
    "Nav History",
    "Fund Data",
    "XETRA",
    "SIX Swiss Exchange",
    "London Stock Exchange",
    "Index Target Weights",
    "Index composition",
    "Index facts",
    "Top 10 index constituents",
    "Equity Portfolio Holdings",
    "Latest Factsheets",
    "Annual and Semi-Annual Reports",
    "Legal Documents",
)

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Market_Access"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SPACE_PATTERN = re.compile(r"\s+")
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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "market_access_etf_export.json"


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


def normalize_label(value: object | None) -> str:
    return clean_text(value).casefold()


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def percentage_to_bps(raw: object | None) -> str:
    cleaned = clean_text(raw).replace("%", "").replace(",", ".")
    if not cleaned:
        return ""
    try:
        return format_decimal(Decimal(cleaned) * Decimal("100"), places=2)
    except InvalidOperation:
        return ""


def amount_to_millions(raw: object | None) -> str:
    cleaned = clean_text(raw)
    if not cleaned:
        return ""
    numeric = re.sub(r"[^\d.,]", "", cleaned)
    if not numeric:
        return ""
    if "," in numeric and "." in numeric:
        numeric = numeric.replace(",", "")
    elif "," in numeric and "." not in numeric:
        parts = numeric.split(",")
        if len(parts) > 2 or len(parts[-1]) == 3:
            numeric = numeric.replace(",", "")
        else:
            numeric = numeric.replace(",", ".")
    try:
        value = Decimal(numeric)
    except InvalidOperation:
        return ""
    return format_decimal(value / Decimal("1000000"), places=2)


def fetch_html(url: str) -> str:
    try:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if sync_playwright is None or (status_code is not None and status_code < 500):
            raise
        logging.info(
            "Market Access HTTP fetch failed for %s with status %s; retrying in Playwright.",
            url,
            status_code,
        )
        return fetch_html_with_playwright(url)


def fetch_html_with_playwright(url: str) -> str:
    if sync_playwright is None:  # pragma: no cover - guarded by caller
        raise RuntimeError("playwright is not installed")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_S * 1000)
            page.wait_for_load_state("networkidle", timeout=REQUEST_TIMEOUT_S * 1000)
            return page.content()
        finally:
            browser.close()


def ensure_market_access_name(name: str) -> str:
    cleaned = clean_text(name)
    if not cleaned:
        return ""
    if normalize_label(cleaned).startswith("market access"):
        return cleaned
    return f"Market Access {cleaned}"


def find_heading(soup: BeautifulSoup, heading_text: str):
    wanted = normalize_label(heading_text)
    return soup.find(
        lambda tag: tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
        and normalize_label(tag.get_text(" ", strip=True)) == wanted
    )


def extract_section_links(soup: BeautifulSoup, heading_text: str) -> list[tuple[str, str]]:
    heading = find_heading(soup, heading_text)
    if heading is None:
        return []

    links: list[tuple[str, str]] = []
    for element in heading.next_elements:
        tag_name = getattr(element, "name", None)
        if tag_name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            break
        if tag_name == "a" and element.get("href"):
            label = clean_text(element.get_text(" ", strip=True))
            if label:
                links.append((label, urljoin(BASE_URL, element["href"])))
    return links


def extract_objective(text: str, title: str) -> str:
    if not text or not title:
        return ""
    match = re.search(
        rf"{re.escape(title)}\s+(.*?)\s+Fund Information\b",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return clean_text(match.group(1)) if match else ""


def extract_first(pattern: str, text: str, *, flags: int = re.IGNORECASE) -> str:
    match = re.search(pattern, text, flags)
    if not match:
        return ""
    return clean_text(match.group(1))


def extract_exchange_block(text: str, heading: str) -> str:
    stop_pattern = "|".join(re.escape(marker) for marker in SECTION_STOPS if marker != heading)
    match = re.search(
        rf"\b{re.escape(heading)}\b\s+(.*?)(?=\s+(?:{stop_pattern})\b|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return clean_text(match.group(1)) if match else ""


def choose_primary_ticker(text: str) -> str:
    for heading in ("XETRA", "London Stock Exchange", "SIX Swiss Exchange"):
        block = extract_exchange_block(text, heading)
        if not block:
            continue
        ticker = extract_first(r"Bloomberg Ticker\s+([A-Z0-9]{2,10})", block)
        if ticker:
            return ticker
    return ""


def build_missing_row(isin: str) -> dict[str, str]:
    return {
        "etf_name": "",
        "issuer": ISSUER,
        "isin": isin,
        "ccy": "",
        "ter_bps": "",
        "aum_mn": "",
        "date": "",
        "nav": "",
        "nav_date": "",
        "distribution_type": "",
        "fund_type": "",
        "fund_domicile": "",
        "index_name": "",
        "index_ticker": "",
        "primary_ticker": "",
        "share_class": "",
        "management_company": "",
        "product_range": "",
        "objective": "",
        "factsheet_url": "",
        "product_page_url": TARGET_ISIN_URLS[isin],
        "kiid_url": "",
        "investment_manager": "",
        "administrator_and_custodian": "",
        "fetch_status": "not_found",
    }


def parse_product_page(isin: str, url: str) -> dict[str, str]:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text("\n", strip=True))

    title_tag = soup.find(
        lambda tag: tag.name in {"h1", "h2", "h3", "h4"} and "ucits etf" in normalize_label(tag.get_text(" ", strip=True))
    )
    title = clean_text(title_tag.get_text(" ", strip=True)) if title_tag else ""
    factsheet_links = extract_section_links(soup, "Latest Factsheets")
    legal_links = extract_section_links(soup, "Legal Documents")

    kiid_url = ""
    for label, link in legal_links:
        normalized_label = normalize_label(label)
        if "kiid" in normalized_label and normalize_isin(isin).lower() in link.lower():
            kiid_url = link
            break

    date_value = extract_first(r"\bFund Data\s+.*?\bDate\s+(\d{2}/\d{2}/\d{4})", text, flags=re.IGNORECASE | re.DOTALL)
    nav_date = date_value
    objective = extract_objective(text, title)

    row = build_missing_row(isin)
    row.update(
        {
            "etf_name": ensure_market_access_name(title),
            "issuer": ISSUER,
            "isin": normalize_isin(extract_first(r"\bISIN\s+([A-Z0-9]{12})", text) or isin),
            "ccy": extract_first(r"\bFund Currency\s+([A-Z]{3})", text).upper(),
            "ter_bps": percentage_to_bps(extract_first(r"\bTER\s+(\d+(?:[.,]\d+)?)\s*%", text)),
            "aum_mn": amount_to_millions(
                extract_first(r"\bAssets under managment\s+[A-Z]{3}\s+([\d,]+(?:\.\d+)?)", text)
            ),
            "date": date_value or extract_first(r"\bLaunch Date\s+(\d{2}/\d{2}/\d{4})", text),
            "nav": extract_first(r"\bNAV per share\s+[A-Z]{3}\s+([\d,]+(?:\.\d+)?)", text),
            "nav_date": nav_date,
            "distribution_type": extract_first(r"\bDividend Treatment\s+(.+?)\s+Fund Type\b", text),
            "fund_type": extract_first(r"\bFund Type\s+(.+?)\s+UCITS Compliant\b", text),
            "fund_domicile": "Luxembourg",
            "index_name": extract_first(r"\bIndex facts\s+Name\s+(.+?)\s+Bloomberg Ticker\b", text, flags=re.IGNORECASE | re.DOTALL),
            "index_ticker": extract_first(r"\bIndex facts\s+.*?\bBloomberg Ticker\s+([A-Z0-9]+)", text, flags=re.IGNORECASE | re.DOTALL),
            "primary_ticker": choose_primary_ticker(text),
            "share_class": "",
            "management_company": extract_first(r"\bManagement Company\s+(.+?)\s+Investment Manager\b", text),
            "product_range": "Market Access",
            "objective": objective,
            "factsheet_url": factsheet_links[0][1] if factsheet_links else "",
            "product_page_url": url,
            "kiid_url": kiid_url,
            "investment_manager": extract_first(r"\bInvestment Manager\s+(.+?)\s+Custodian and Administrator\b", text),
            "administrator_and_custodian": extract_first(
                r"\bCustodian and Administrator\s+(.+?)\s+Countries authorised for distribution\b",
                text,
            ),
        }
    )

    row["fetch_status"] = "ok" if row["etf_name"] else "missing_data"
    return row


def build_snapshot() -> dict[str, Any]:
    listing_rows: list[dict[str, str]] = []
    status_counts: dict[str, int] = {}

    for index, (isin, url) in enumerate(TARGET_ISIN_URLS.items(), start=1):
        logging.info("[%d/%d] Fetching %s", index, len(TARGET_ISIN_URLS), isin)
        try:
            row = parse_product_page(isin, url)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to parse Market Access %s: %s", isin, exc)
            row = build_missing_row(isin)
            row["fetch_status"] = f"error:{type(exc).__name__}"
        listing_rows.append(row)
        status = row["fetch_status"]
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "source": {
            "provider": ISSUER,
            "about_url": ABOUT_URL,
            "base_url": BASE_URL,
            "product_urls": list(TARGET_ISIN_URLS.values()),
        },
        "method": (
            "Official marketaccessetf.com product-page HTML parsing of Fund Information, Fund Data, "
            "exchange listings, Index facts, Latest Factsheets, and Legal Documents for an explicit "
            "Market Access target ISIN list."
        ),
        "captured_at": timestamp_now().isoformat(),
        "target_isin_count": len(TARGET_ISIN_URLS),
        "matched_target_isins": [row["isin"] for row in listing_rows if row["fetch_status"] == "ok"],
        "missing_target_isins": [row["isin"] for row in listing_rows if row["fetch_status"] != "ok"],
        "status_counts": status_counts,
        "listing_rows": listing_rows,
    }


def download_snapshot(output_path: Path) -> Path:
    snapshot = build_snapshot()
    write_json(output_path, snapshot)
    logging.info("Market Access fetch summary: %s", snapshot.get("status_counts", {}))
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved : %s", output_path)
    return output_path


async def download_market_access_file() -> Path:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def main() -> None:
    setup_logging()
    output_path = asyncio.run(download_market_access_file())
    print(f"Saved Market Access snapshot to: {output_path}")


if __name__ == "__main__":
    main()
