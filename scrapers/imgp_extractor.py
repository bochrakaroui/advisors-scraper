"""Download iM Global Partner ETF data from the official funds page into a provider-specific raw snapshot.

Strategy: Playwright (headless Chromium) to handle the country/investor-type
modal, then BeautifulSoup to parse the SSR fund list.

ETF detection: share class name contains "UCITS ETF" (case-insensitive).
Each ETF share class becomes one row; the parent fund supplies fund-level
fields (AUM, asset class, inception).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright
import requests


PAGE_URL = "https://www.imgp.com/funds/"
ISSUER = "iM Global Partner"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "imgp"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
DETAIL_PAGE_TIMEOUT_S = 45
NAVIGATION_TIMEOUT_MS = 60_000
NAVIGATION_RETRIES = 3
NAVIGATION_RETRY_DELAY_S = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

DETAIL_PAGE_SESSION = requests.Session()
DETAIL_PAGE_SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Output / path helpers
# ---------------------------------------------------------------------------

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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "imgp_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -"} else cleaned


def percentage_to_bps(raw_value: object | None) -> str:
    cleaned = clean_text(raw_value).replace("%", "").replace(",", ".")
    if not cleaned:
        return ""

    try:
        numeric_value = float(cleaned)
    except ValueError:
        return ""

    if "%" in str(raw_value):
        bps = numeric_value * 100
    elif numeric_value <= 0.05:
        bps = numeric_value * 10000
    elif numeric_value <= 5:
        bps = numeric_value * 100
    else:
        bps = numeric_value

    return f"{bps:.2f}"


def is_etf_share_class(name: str) -> bool:
    return "ucits etf" in name.lower()


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def safe_click(page: Page, selectors: list[str], *, timeout: int = 3_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                loc.click(timeout=5_000)
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


def navigate_with_retry(page: Page, url: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, NAVIGATION_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            return
        except PlaywrightError as exc:
            last_error = exc
            message = str(exc)
            is_retryable = "ERR_NETWORK_CHANGED" in message or "ERR_ABORTED" in message
            if attempt >= NAVIGATION_RETRIES or not is_retryable:
                raise
            logging.warning(
                "Transient navigation error while loading %s (attempt %s/%s): %s",
                url,
                attempt,
                NAVIGATION_RETRIES,
                message,
            )
            time.sleep(NAVIGATION_RETRY_DELAY_S)

    if last_error is not None:
        raise last_error


def dismiss_imgp_modals(page: Page) -> None:
    """
    The site shows a country-selector modal and then an investor-type modal
    before the fund list is accessible.

    Step 1 — Accept T&C / select investor type (Institutional button is most
              permissive and leads directly to the full fund list).
    Step 2 — Accept cookie banner if present.
    """
    logging.info("Dismissing country / investor-type modal ...")

    # The initial modal has three cards: Individual | Investment Professional | Institutional
    # Clicking "Institutional" + confirming T&C gives full access.
    safe_click(
        page,
        [
            # Institutional card click (text-based)
            "text='Institutional'",
            ":text('Institutional')",
            "div.investor-type:has-text('Institutional')",
            "h1:has-text('Institutional')",
        ],
        timeout=5_000,
    )
    page.wait_for_timeout(800)

    # Confirm / Accept button (T&C acceptance)
    safe_click(
        page,
        [
            "button:has-text('Accept')",
            "button:has-text('Confirm')",
            "button:has-text('I Agree')",
            "a:has-text('Accept')",
            ".modal-accept",
            "[data-action='accept']",
        ],
        timeout=5_000,
    )
    page.wait_for_timeout(1_000)

    # Cookie banner
    safe_click(
        page,
        [
            "button:has-text('Accept All')",
            "button:has-text('Accept all cookies')",
            "button:has-text('Accept Cookies')",
            "#onetrust-accept-btn-handler",
            ".cookie-accept",
        ],
        timeout=3_000,
    )
    page.wait_for_timeout(500)


def fetch_rendered_html(page: Page) -> str:
    """Navigate to the funds page and return the fully-rendered HTML."""
    logging.info("Navigating to %s ...", PAGE_URL)
    navigate_with_retry(page, PAGE_URL)
    page.wait_for_timeout(3_000)

    dismiss_imgp_modals(page)

    # Wait for at least one fund block to appear
    try:
        page.wait_for_selector("div.sub-fund-item", timeout=20_000)
        logging.info("Fund list loaded.")
    except Exception:
        logging.warning(
            "div.sub-fund-item not found after modal dismissal — "
            "saving debug snapshot and proceeding anyway."
        )
        debug_path = OUTPUT_DIR / "debug_imgp_dom.html"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(page.content(), encoding="utf-8")
        logging.info("Debug DOM saved to %s", debug_path)

    return page.content()


# ---------------------------------------------------------------------------
# HTML parsing helpers
# (DOM: div.sub-fund-item > div.sub-fund-detail + a.fund-link elements)
# ---------------------------------------------------------------------------

def _content(tag) -> str:
    """Extract text from the first span.content child of a BeautifulSoup tag."""
    if tag is None:
        return ""
    content_span = tag.find("span", class_="content")
    return clean_text(content_span.get_text() if content_span else tag.get_text())


def parse_fund_size(raw: str) -> tuple[str, str]:
    """
    'USD 538.4 mm' → ('USD', '538.40')
    'EUR 1.2 bn'   → ('EUR', '1200.00')
    """
    if not raw:
        return "", ""
    m = re.match(r"([A-Z]{3})\s*([\d,.]+)\s*(mm|m|bn|b)", raw, re.IGNORECASE)
    if not m:
        return "", ""
    ccy = m.group(1).upper()
    try:
        val = float(m.group(2).replace(",", ""))
    except ValueError:
        return ccy, ""
    if m.group(3).lower() in ("bn", "b"):
        val *= 1000.0
    return ccy, f"{val:.2f}"


def extract_fee_from_detail_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    fee_priority = (
        "Total Fees",
        "Annual Fund Operating Expenses",
        "Net Expense Ratio",
        "Gross Expense Ratio",
        "Adjusted Expense Ratio",
        "Management Fee",
    )

    fee_values: dict[str, str] = {}
    for fee_block in soup.select(".fees-content .fee"):
        title = clean_text(fee_block.select_one(".title").get_text() if fee_block.select_one(".title") else "")
        content = clean_text(fee_block.select_one(".content").get_text() if fee_block.select_one(".content") else "")
        if title and content:
            fee_values[title] = content

    for label in fee_priority:
        fee_value = fee_values.get(label, "")
        if fee_value:
            return percentage_to_bps(fee_value)

    embedded_patterns = (
        r'"total_fees":"([^"]+)"',
        r'"annual_fund_operating_expenses":"([^"]+)"',
        r'"net_exp_ratio":"([^"]+)"',
        r'"gross_exp_ratio":"([^"]+)"',
        r'"adjust_exp_ratio":"([^"]+)"',
        r'"management_fee":"([^"]+)"',
    )
    for pattern in embedded_patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if not match:
            continue
        fee_value = clean_text(match.group(1))
        if fee_value and fee_value.lower() != "null":
            return percentage_to_bps(fee_value)

    return ""


def fetch_fee_bps(product_url: str) -> str:
    response = DETAIL_PAGE_SESSION.get(product_url, timeout=DETAIL_PAGE_TIMEOUT_S)
    response.raise_for_status()
    return extract_fee_from_detail_html(response.text)


def extract_listing_rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, str]] = []
    fee_cache: dict[str, str] = {}

    fund_blocks = soup.find_all("div", class_="sub-fund-item")
    logging.info("Parsing %s fund blocks ...", len(fund_blocks))

    for fund_block in fund_blocks:
        # ── Fund-level fields ──────────────────────────────────────────────
        detail = fund_block.find("div", class_="sub-fund-detail")
        if not detail:
            continue

        fund_name = _content(detail.find("span", class_="name"))
        fund_inception = _content(detail.find("span", class_="inception"))
        fund_size_raw = _content(detail.find("span", class_="size"))
        asset_class = _content(detail.find("span", class_="asset_class"))
        fund_ccy, fund_aum_mn = parse_fund_size(fund_size_raw)

        # ── Share-class rows ───────────────────────────────────────────────
        for link in fund_block.find_all("a", class_="fund-link"):
            sc_name = _content(link.find("span", class_="name"))

            if not is_etf_share_class(sc_name):
                continue

            isin_span = link.find("span", class_="isin")
            isin = _content(isin_span).upper() if isin_span else ""

            sc_inception = _content(link.find("span", class_="inception-date"))
            share_price_raw = _content(link.find("span", class_="share-price"))

            # Derive currency from share price string e.g. "EUR 121.55 as of 06/22/2026"
            sc_ccy = ""
            ccy_m = re.match(r"([A-Z]{3})\s", share_price_raw)
            if ccy_m:
                sc_ccy = ccy_m.group(1)

            href = link.get("href", "")
            product_url = href if href.startswith("http") else f"https://www.imgp.com{href}"
            ter_bps = fee_cache.get(product_url, "")
            if product_url and product_url not in fee_cache:
                try:
                    ter_bps = fetch_fee_bps(product_url)
                except Exception as exc:
                    logging.warning("Could not fetch iMGP fee for %s: %s", product_url, exc)
                    ter_bps = ""
                fee_cache[product_url] = ter_bps

            if not isin:
                logging.warning("ETF share class '%s' has no ISIN — skipping.", sc_name)
                continue

            rows.append(
                {
                    "etf_name": sc_name,
                    "fund_name": fund_name,
                    "issuer": ISSUER,
                    "isin": isin,
                    "asset_class": asset_class,
                    "ccy": sc_ccy or fund_ccy,
                    "fund_size_raw": fund_size_raw,
                    "aum_mn": fund_aum_mn,
                    "fund_inception": fund_inception,
                    "share_class_inception": sc_inception,
                    "share_price_raw": share_price_raw,
                    "product_url": product_url,
                    "ter_bps": ter_bps,
                }
            )

    return rows


# ---------------------------------------------------------------------------
# Main snapshot builder
# ---------------------------------------------------------------------------

def build_snapshot(now: datetime) -> dict[str, object]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
        )
        page = context.new_page()
        html = fetch_rendered_html(page)
        browser.close()

    listing_rows = extract_listing_rows(html)
    logging.info(
        "Captured %s iMGP ETF share class rows (UCITS ETF filter applied).",
        len(listing_rows),
    )

    return {
        "source_url": PAGE_URL,
        "method": "Playwright (networkidle) + BeautifulSoup — UCITS ETF share classes only",
        "captured_at": now.isoformat(),
        "listing_rows": listing_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now = timestamp_now()
    snapshot = build_snapshot(now)
    write_json(destination, snapshot)
    logging.info("Data method used: %s", snapshot["method"])
    logging.info("Raw snapshot saved: %s", destination)


async def download_imgp_file() -> Path:
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
