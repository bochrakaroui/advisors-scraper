"""
Palmer Square ETF Scraper
=========================

Scrapes one Palmer Square UCITS ETF product page and saves RAW scraped data.

Input URL:
    https://etf.palmersquarefunds.com/funds/ucits-etfs/palmer-square-eur-clo-senior-debt-index-ucits-etf

Output folder:
    providers/palmersquare/YYYY-MM-DD/

Output file:
    palmersquare_raw_YYYY-MM-DD.csv

Raw output columns:
    ETF Name, Issuer, ISIN, Ticker, Exchange, AUM, Ongoing Charges, Date
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

URL = (
    "https://etf.palmersquarefunds.com/funds/ucits-etfs/"
    "palmer-square-eur-clo-senior-debt-index-ucits-etf"
)

ISSUER = "Palmer Square"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_DIR = PROJECT_ROOT / "providers" / "palmersquare"

RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

PAGE_LOAD_TIMEOUT = 60_000
ELEMENT_TIMEOUT = 20_000

RAW_OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "Ticker",
    "Exchange",
    "AUM",
    "Ongoing Charges",
    "Date",
]


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Palmer Square UCITS ETF details."
    )

    parser.add_argument(
        "--url",
        default=URL,
        help="Palmer Square ETF product page URL.",
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Show browser window. Default is headless.",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# FOLDER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def build_save_dir() -> Path:
    """
    Creates/reuses a date folder inside providers/palmersquare.

    Example:
        providers/palmersquare/2026-06-24/
    """

    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)

    if run_folder_name:
        save_dir = PROVIDER_DIR / run_folder_name
        save_dir.mkdir(parents=True, exist_ok=True)
        return save_dir

    today = datetime.now().strftime("%Y-%m-%d")
    save_dir = PROVIDER_DIR / today
    save_dir.mkdir(parents=True, exist_ok=True)

    os.environ[RUN_FOLDER_ENV_VAR] = save_dir.name

    return save_dir


def build_output_path(save_dir: Path) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return save_dir / f"palmersquare_raw_{today}.csv"


# ─────────────────────────────────────────────────────────────────────────────
# CLEANERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(value: object | None) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)

    if text in {"", "-", "—", "–", "None", "nan", "NaN"}:
        return ""

    return text


def clean_label(value: object | None) -> str:
    text = clean_text(value).lower()
    text = text.replace(":", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_date(value: object | None) -> str:
    """
    Converts dates like:
        Jun 23, 2026
        Aug 7, 2025
    into:
        DD/MM/YYYY
    """

    text = clean_text(value)

    if not text:
        return datetime.now().strftime("%d/%m/%Y")

    text = text.replace("AS OF", "").replace("As of", "").replace("as of", "").strip()

    for fmt in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    return text


def extract_as_of_date(full_text: str) -> str:
    """
    Extracts date from text like:
        Total Net Assets €52,680,265 AS OF JUN 23, 2026
    """

    match = re.search(
        r"as\s+of\s+([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )

    if match:
        return normalize_date(match.group(1))

    return datetime.now().strftime("%d/%m/%Y")


# ─────────────────────────────────────────────────────────────────────────────
# COOKIE / OVERLAY HANDLING
# ─────────────────────────────────────────────────────────────────────────────

def dismiss_cookie_banner(page) -> None:
    """
    Tries to dismiss common cookie banners if they appear.
    Safe if no cookie banner exists.
    """

    print("[2/5] Checking cookie banner...")

    try:
        result = page.evaluate(
            """
            () => {
                const texts = [
                    'accept all',
                    'accept cookies',
                    'accept',
                    'agree',
                    'i agree',
                    'allow all'
                ];

                const candidates = Array.from(
                    document.querySelectorAll('button, a, div[role="button"]')
                ).filter(el => {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return text && texts.some(t => text === t || text.includes(t));
                });

                for (const el of candidates) {
                    try {
                        ['mousedown', 'mouseup', 'click'].forEach(type => {
                            el.dispatchEvent(new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                view: window
                            }));
                        });

                        return 'CLICKED: ' + (el.innerText || el.textContent || '').trim().slice(0, 80);
                    } catch (_) {}
                }

                return 'NOT_FOUND';
            }
            """
        )

        print(f"  Cookie result: {result}")

        if result.startswith("CLICKED"):
            page.wait_for_timeout(1_000)

    except Exception as exc:
        print(f"  Cookie check skipped: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def extract_details_from_page(page) -> dict[str, str]:
    """
    Extracts ETF fields from the visible ETF DETAILS section.

    The Palmer Square page uses pairs like:
        data--label  = ISIN Code
        data--value  = IE000JTHNWF0
    """

    data = page.evaluate(
        """
        () => {
            const result = {
                title: '',
                pairs: [],
                allText: ''
            };

            const h1 = document.querySelector('h1');
            if (h1) {
                result.title = h1.innerText.trim();
            }

            if (!result.title) {
                const titleCandidate = document.querySelector(
                    '.hero h1, .fund-title, .page-title, h2'
                );
                if (titleCandidate) {
                    result.title = titleCandidate.innerText.trim();
                }
            }

            const wrappers = document.querySelectorAll('.data-wrapper, .data-item');

            for (const wrapper of wrappers) {
                const labelEl = wrapper.querySelector('.data--label');
                const valueEl = wrapper.querySelector('.data--value');

                if (!labelEl || !valueEl) {
                    continue;
                }

                const label = labelEl.innerText.trim();
                const value = valueEl.innerText.trim();
                const wrapperText = wrapper.innerText.trim();

                result.pairs.push({
                    label,
                    value,
                    text: wrapperText
                });
            }

            result.allText = document.body.innerText || '';

            return result;
        }
        """
    )

    title = clean_text(data.get("title", ""))
    pairs = data.get("pairs", [])
    all_text = clean_text(data.get("allText", ""))

    values: dict[str, str] = {}

    for item in pairs:
        label = clean_label(item.get("label", ""))
        value = clean_text(item.get("value", ""))
        text = clean_text(item.get("text", ""))

        if not label or not value:
            continue

        values[label] = value

        # Special case: Total Net Assets has an "AS OF ..." line.
        if "total net assets" in label:
            values["total net assets as of"] = extract_as_of_date(text)

    # Fallback title from document text if h1 was not found.
    if not title:
        match = re.search(
            r"PALMER SQUARE\s+.+?\s+UCITS ETF",
            all_text,
            flags=re.IGNORECASE,
        )
        if match:
            title = clean_text(match.group(0))

    isin = (
        values.get("isin code")
        or values.get("isin")
        or ""
    )

    ticker = values.get("ticker", "")

    exchange = values.get("exchange", "")

    aum = (
        values.get("total net assets")
        or values.get("net assets")
        or values.get("aum")
        or ""
    )

    ongoing_charges = (
        values.get("ongoing charges")
        or values.get("ongoing charge")
        or values.get("ocf")
        or ""
    )

    date_value = (
        values.get("total net assets as of")
        or extract_as_of_date(all_text)
    )

    record = {
        "ETF Name": title,
        "Issuer": ISSUER,
        "ISIN": isin.upper(),
        "Ticker": ticker,
        "Exchange": exchange,
        "AUM": aum,
        "Ongoing Charges": ongoing_charges,
        "Date": date_value,
    }

    return record


def validate_record(record: dict[str, str]) -> None:
    missing = [
        key
        for key in ["ETF Name", "ISIN", "AUM", "Ongoing Charges"]
        if not clean_text(record.get(key))
    ]

    if missing:
        raise ValueError(
            f"Missing required fields: {missing}. "
            f"Scraped record: {record}"
        )


def scrape_palmersquare(url: str, *, headless: bool = True) -> list[dict[str, str]]:
    print("=" * 60)
    print("Palmer Square ETF Scraper")
    print("=" * 60)
    print(f"URL      : {url}")
    print(f"Headless : {headless}")
    print("=" * 60)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="en-GB",
        )

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()

        print("[1/5] Opening page...")
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_timeout(2_000)

        dismiss_cookie_banner(page)

        print("[3/5] Waiting for ETF details...")

        try:
            page.wait_for_selector("text=ETF DETAILS", timeout=ELEMENT_TIMEOUT)
        except PlaywrightTimeoutError:
            print("  ETF DETAILS text not found immediately, continuing...")

        page.wait_for_timeout(1_000)

        print("[4/5] Extracting ETF details...")
        record = extract_details_from_page(page)

        validate_record(record)

        print("[5/5] Done.")
        print(f"  ETF Name        : {record['ETF Name']}")
        print(f"  ISIN            : {record['ISIN']}")
        print(f"  Ticker          : {record['Ticker']}")
        print(f"  Exchange        : {record['Exchange']}")
        print(f"  AUM             : {record['AUM']}")
        print(f"  Ongoing Charges : {record['Ongoing Charges']}")
        print(f"  Date            : {record['Date']}")

        context.close()
        browser.close()

    return [record]


# ─────────────────────────────────────────────────────────────────────────────
# CSV WRITER
# ─────────────────────────────────────────────────────────────────────────────

def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def process(url: str, *, headless: bool = True) -> Path:
    rows = scrape_palmersquare(url, headless=headless)

    save_dir = build_save_dir()
    output_path = build_output_path(save_dir)

    write_csv(output_path, rows)

    print()
    print("=" * 60)
    print("✅ Palmer Square raw file saved")
    print("=" * 60)
    print(f"Rows written : {len(rows)}")
    print(f"Output folder: {save_dir}")
    print(f"Raw file     : {output_path}")
    print("=" * 60)

    return output_path


async def download_palmersquare_file(url: str = URL, *, headless: bool = True) -> Path:
    return await asyncio.to_thread(process, url, headless=headless)


def main() -> None:
    args = parse_args()
    process(args.url, headless=not args.headed)


if __name__ == "__main__":
    main()
