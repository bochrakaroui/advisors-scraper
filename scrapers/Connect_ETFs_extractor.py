"""Scrape selected Connect ETFs share classes from the official website."""

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
        "beautifulsoup4 is required for the Connect ETFs scraper. "
        "Install it with 'pip install beautifulsoup4'."
    ) from exc


ISSUER = "Connect ETFs"
BASE_URL = "https://www.connectetfs.com"
HOME_URL = f"{BASE_URL}/"
REQUEST_TIMEOUT_S = 45

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Connect_ETFs"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TARGET_ISINS = [
    "IE0002TF35X3",
    "IE000356FN00",
    "IE000AM06QU6",
    "IE000MCQF2X3",
    "IE000U8A7X11",
    "IE000VMDRT50",
    "IE000Y36NRJ2",
]

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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "connect_etfs_export.json"


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


def percent_to_bps(value: str) -> str:
    cleaned = clean_text(value).replace("%", "").replace(",", "")
    if not cleaned:
        return ""
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return ""
    return format_decimal(amount * Decimal("100"), places=2)


def amount_to_millions(value: str) -> str:
    cleaned = re.sub(r"[^\d.]", "", clean_text(value))
    if not cleaned:
        return ""
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return ""
    return format_decimal(amount / Decimal("1000000"), places=2)


def extract_currency_code(value: str) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    match = re.search(r"\b([A-Z]{3})\b", cleaned.upper())
    return match.group(1) if match else cleaned.upper()


def parse_date(value: str) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def fetch_html(url: str) -> str:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.text


def discover_product_urls() -> list[str]:
    soup = BeautifulSoup(fetch_html(HOME_URL), "html.parser")
    product_urls: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        if not href.startswith("/products/"):
            continue
        absolute = urljoin(BASE_URL, href)
        if absolute not in seen:
            seen.add(absolute)
            product_urls.append(absolute)

    if not product_urls:
        raise RuntimeError("No product pages were discovered on the official Connect ETFs homepage.")
    return product_urls


def find_heading(soup: BeautifulSoup, text: str):
    target = normalize_label(text)
    return soup.find(
        lambda tag: tag.name in {"h1", "h2", "h3"} and normalize_label(tag.get_text(" ", strip=True)) == target
    )


def extract_section_pairs(soup: BeautifulSoup, heading_text: str) -> dict[str, str]:
    heading = find_heading(soup, heading_text)
    if heading is None or heading.parent is None:
        return {}

    pairs: dict[str, str] = {}
    section = heading.parent
    for row in section.find_all("div", class_=lambda value: value and "border-t-2" in value):
        items = row.find_all("p", recursive=False)
        if len(items) < 2:
            continue
        label = clean_text(items[0].get_text(" ", strip=True))
        value = clean_text(items[1].get_text(" ", strip=True))
        if label and value:
            pairs[label] = value
    return pairs


def extract_table_after_heading(soup: BeautifulSoup, heading_text: str) -> list[dict[str, str]]:
    heading = find_heading(soup, heading_text)
    if heading is None or heading.parent is None:
        return []

    table = heading.parent.find("table")
    if table is None:
        return []

    headers = [clean_text(cell.get_text(" ", strip=True)) for cell in table.select("thead th")]
    headers = [header for header in headers if header]
    if not headers:
        return []

    rows: list[dict[str, str]] = []
    for tr in table.select("tbody tr"):
        values = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all("td", recursive=False)]
        if not any(values):
            continue
        if len(values) < len(headers):
            values.extend([""] * (len(headers) - len(values)))
        row = dict(zip(headers, values[: len(headers)]))
        rows.append(row)
    return rows


def extract_objective(soup: BeautifulSoup) -> str:
    heading = soup.find("h1")
    if heading is None:
        return ""
    paragraph = heading.find_next("p")
    if paragraph is None:
        return ""
    return clean_text(paragraph.get_text(" ", strip=True))


def extract_factsheet_url(soup: BeautifulSoup) -> str:
    heading = find_heading(soup, "Latest Factsheets")
    if heading is None or heading.parent is None:
        return ""

    for anchor in heading.parent.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True))
        if "factsheet" in normalize_label(label):
            return urljoin(BASE_URL, anchor["href"])
    return ""


def extract_kiid_urls(soup: BeautifulSoup) -> list[tuple[str, str]]:
    heading = find_heading(soup, "Latest Factsheets")
    if heading is None or heading.parent is None:
        return []

    results: list[tuple[str, str]] = []
    for anchor in heading.parent.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True))
        if "kiid" in normalize_label(label):
            results.append((label, urljoin(BASE_URL, anchor["href"])))
    return results


def choose_kiid_url(isin: str, share_class: str, kiid_links: list[tuple[str, str]]) -> str:
    normalized_isin = normalize_isin(isin).lower()
    for _, url in kiid_links:
        if normalized_isin and normalized_isin in url.lower():
            return url

    normalized_share_class = normalize_label(share_class)
    wants_acc = "accumulating" in normalized_share_class or re.search(r"\bacc\b", normalized_share_class)
    wants_dis = "distributing" in normalized_share_class or re.search(r"\bdis\b", normalized_share_class)
    wants_unlisted = "unlisted" in normalized_share_class
    wants_gbp_hedged = "gbp hedged" in normalized_share_class
    wants_eur_hedged = "eur hedged" in normalized_share_class
    wants_usd = "usd" in normalized_share_class
    wants_gbp = "gbp" in normalized_share_class
    wants_eur = "eur" in normalized_share_class

    share_tokens = []
    for token in ["usd", "gbp", "eur", "acc", "dis", "hedged", "unlisted"]:
        if token in normalized_share_class:
            share_tokens.append(token)

    best_url = ""
    best_score = -1
    for label, url in kiid_links:
        normalized_label = normalize_label(label)
        combined = f"{normalized_label} {url.lower()}"
        score = sum(1 for token in share_tokens if token in normalized_label)
        if wants_acc and "acc" in normalized_label:
            score += 2
        if wants_dis and (" dis" in f" {normalized_label}" or "dist" in normalized_label):
            score += 2
        if wants_unlisted == ("unlisted" in normalized_label):
            score += 2
        elif "unlisted" in normalized_label:
            score -= 4
        if wants_gbp_hedged and "gbph" in normalized_label:
            score += 2
        if wants_eur_hedged and "eurh" in normalized_label:
            score += 2

        if wants_usd and "usd" not in combined:
            continue
        if wants_gbp_hedged and not any(token in combined for token in ["gbph", "gbp hedged"]):
            continue
        if wants_eur_hedged and not any(token in combined for token in ["eurh", "eur hedged"]):
            continue
        if wants_gbp and not wants_gbp_hedged and "gbp" not in combined:
            continue
        if wants_eur and not wants_eur_hedged and "eur" not in combined:
            continue

        if score > best_score:
            best_score = score
            best_url = url

    if best_score <= 0:
        return ""
    if wants_dis and not any(token in normalize_label(best_url) for token in ["dis", "dist", "gbph dis", "eurh dis"]):
        return ""
    if wants_acc and "acc" not in normalize_label(best_url):
        return ""
    return best_url


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


def parse_product_page(url: str) -> list[dict[str, str]]:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    product_name = clean_text(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
    objective = extract_objective(soup)
    fund_information = extract_section_pairs(soup, "Fund Information")
    fund_data = extract_section_pairs(soup, "Fund Data")
    share_class_rows = extract_table_after_heading(soup, "Share Classes")
    pricing_rows = extract_table_after_heading(soup, "Pricing")
    factsheet_url = extract_factsheet_url(soup) or url
    kiid_links = extract_kiid_urls(soup)

    pricing_by_isin = {
        normalize_isin(row.get("ISIN")): row
        for row in pricing_rows
        if normalize_isin(row.get("ISIN"))
    }

    results: list[dict[str, str]] = []
    for share_row in share_class_rows:
        isin = normalize_isin(share_row.get("ISIN"))
        if isin not in TARGET_ISINS:
            continue

        pricing_row = pricing_by_isin.get(isin, {})
        share_class = clean_text(share_row.get("Share Class"))
        distribution_type = clean_text(share_row.get("Income Treatment"))
        share_class_currency = clean_text(share_row.get("Currency"))

        results.append(
            {
                "etf_name": clean_text(f"{product_name} {share_class}"),
                "issuer": ISSUER,
                "isin": isin,
                "ccy": extract_currency_code(share_class_currency),
                "ter_bps": percent_to_bps(fund_information.get("TER", "")),
                "aum_mn": amount_to_millions(fund_data.get("Assets under managment", "")),
                "date": parse_date(fund_data.get("Date", "")),
                "nav": clean_text(pricing_row.get("NAV per share")),
                "nav_date": parse_date(pricing_row.get("NAV Date", "")),
                "distribution_type": distribution_type,
                "fund_type": clean_text(fund_information.get("Fund Type")),
                "fund_domicile": "Ireland",
                "index_name": "",
                "index_ticker": "",
                "primary_ticker": clean_text(pricing_row.get("LSE Ticker")) or clean_text(share_row.get("BBG LSE tickers")),
                "share_class": share_class,
                "management_company": clean_text(fund_information.get("Management Company")),
                "product_range": "Connect ETFs ICAV",
                "objective": objective,
                "factsheet_url": factsheet_url,
                "product_page_url": url,
                "kiid_url": choose_kiid_url(isin, share_class, kiid_links),
                "launch_date": parse_date(fund_information.get("Launch Date", "")),
                "investment_manager": clean_text(fund_information.get("Investment Manager")),
                "administrator_and_custodian": clean_text(fund_information.get("Administrator and Custodian")),
                "bbg": clean_text(share_row.get("BBG")),
                "reuters": clean_text(share_row.get("Reuters")),
                "lipper": clean_text(share_row.get("Lipper")),
                "fetch_status": "ok",
            }
        )

    return results


def build_snapshot() -> dict[str, Any]:
    product_urls = discover_product_urls()
    rows_by_isin: dict[str, dict[str, str]] = {}
    status_counts: dict[str, int] = {}

    for index, product_url in enumerate(product_urls, start=1):
        logging.info("Scanning Connect ETFs product page [%d/%d] %s", index, len(product_urls), product_url)
        try:
            product_rows = parse_product_page(product_url)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to parse %s: %s", product_url, exc)
            continue

        for row in product_rows:
            rows_by_isin[row["isin"]] = row

    listing_rows: list[dict[str, str]] = []
    for index, isin in enumerate(TARGET_ISINS, start=1):
        logging.info("[%d/%d] Collecting %s", index, len(TARGET_ISINS), isin)
        row = rows_by_isin.get(isin, build_missing_row(isin))
        listing_rows.append(row)
        status_counts[row["fetch_status"]] = status_counts.get(row["fetch_status"], 0) + 1

    return {
        "source": {
            "provider": ISSUER,
            "homepage_url": HOME_URL,
            "base_url": BASE_URL,
            "product_urls": product_urls,
        },
        "method": (
            "Official connectetfs.com homepage product discovery plus product-page HTML parsing "
            "of Fund Information, Fund Data, Share Classes, Pricing, and Latest Factsheets, "
            "filtered to an explicit Connect ETFs target ISIN list."
        ),
        "captured_at": timestamp_now().isoformat(),
        "target_isin_count": len(TARGET_ISINS),
        "matched_target_isins": [row["isin"] for row in listing_rows if row["fetch_status"] == "ok"],
        "missing_target_isins": [row["isin"] for row in listing_rows if row["fetch_status"] != "ok"],
        "status_counts": status_counts,
        "listing_rows": listing_rows,
    }


def download_snapshot(output_path: Path) -> Path:
    snapshot = build_snapshot()
    write_json(output_path, snapshot)
    logging.info("Connect ETFs fetch summary: %s", snapshot.get("status_counts", {}))
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved : %s", output_path)
    return output_path


async def download_connect_etfs_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def main() -> None:
    setup_logging()
    output_path = asyncio.run(download_connect_etfs_file())
    print(f"Saved Connect ETFs snapshot to: {output_path}")


if __name__ == "__main__":
    main()
