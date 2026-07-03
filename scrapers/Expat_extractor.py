"""Scrape Expat ETF data from the official Expat fund page.

Output: providers/Expat/YYYY-MM-DD/expat_export.json
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

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ModuleNotFoundError:  # pragma: no cover - optional PDF enrichment
    PdfReader = None  # type: ignore[assignment]


ISSUER = "Expat Asset Management"
EXPAT_FUND_URL = "https://expat.bg/en/funds/ExpatBulgariaSOFIX"
REQUEST_TIMEOUT_S = 60
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
TARGET_ISINS = ["BG9000011163"]

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Expat"

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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "expat_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None", "null", "N/A"} else cleaned


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


def fetch_pdf_text(url: str) -> str:
    if PdfReader is None:
        return ""
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def build_missing_row(isin: str) -> dict[str, Any]:
    return {
        "etf_name": "",
        "issuer": ISSUER,
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
        "product_page_url": EXPAT_FUND_URL,
        "kiid_url": "",
        "launch_date": "",
        "investment_manager": "",
        "administrator_and_custodian": "",
        "bbg": "",
        "reuters": "",
        "lipper": "",
        "fetch_status": "not_found",
    }


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
        r'Fund Manager:\s*Management company[ â€œ"]+([^"\n]+?)["â€]\s*EAD',
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
    row = build_missing_row("BG9000011163")
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
            "management_company": extract_expat_management_company(kid_text) or f"{ISSUER} EAD",
            "objective": extract_expat_objective(kid_text) or extract_expat_objective(page_text),
            "factsheet_url": links.get("factsheet_url", ""),
            "kiid_url": links.get("kiid_url", ""),
            "launch_date": extract_expat_launch_date(html),
            "investment_manager": ISSUER,
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

    logging.info("[1/1] Collecting Expat Bulgaria SOFIX BG9000011163")
    try:
        row = parse_expat_row()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed to collect Expat Bulgaria SOFIX BG9000011163: %s", exc)
        row = build_missing_row("BG9000011163")
        row["fetch_status"] = f"error:{type(exc).__name__}"

    listing_rows.append(row)
    status_counts[row["fetch_status"]] = status_counts.get(row["fetch_status"], 0) + 1

    matched_statuses = {"ok", "partial"}
    return {
        "source": {
            "provider": ISSUER,
            "expat_fund_url": EXPAT_FUND_URL,
        },
        "method": (
            "Official Expat Bulgaria SOFIX fund-page HTML parsing plus KID PDF enrichment for "
            "the Expat Bulgaria SOFIX UCITS ETF target ISIN."
        ),
        "captured_at": timestamp_now().isoformat(),
        "target_isin_count": len(TARGET_ISINS),
        "matched_target_isins": [item["isin"] for item in listing_rows if item["fetch_status"] in matched_statuses],
        "missing_target_isins": [item["isin"] for item in listing_rows if item["fetch_status"] not in matched_statuses],
        "status_counts": status_counts,
        "listing_rows": listing_rows,
    }


def download_snapshot(output_path: Path) -> Path:
    snapshot = build_snapshot()
    write_json(output_path, snapshot)
    logging.info("Expat fetch summary: %s", snapshot.get("status_counts", {}))
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved : %s", output_path)
    return output_path


async def download_expat_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def main() -> None:
    setup_logging()
    output_path = asyncio.run(download_expat_file())
    print(f"Saved Expat snapshot to: {output_path}")


if __name__ == "__main__":
    main()
