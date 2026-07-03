"""Scrape all publicly listed Pacer ETFs from the official products website."""

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
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
except ImportError as exc:  # pragma: no cover - runtime guidance for local usage
    raise RuntimeError(
        "playwright is required for the Pacer ETFs scraper. "
        "Install it with 'pip install playwright' and run "
        "'python -m playwright install chromium'."
    ) from exc


ISSUER = "Pacer ETFs"
BASE_URL = "https://www.paceretfs.com"
PRODUCTS_URL = f"{BASE_URL}/products"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Pacer_ETFs"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

PLAYWRIGHT_TIMEOUT_MS = 120000
DETAIL_READY_TIMEOUT_MS = 60000
MAX_DETAIL_ATTEMPTS = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

SPACE_RE = re.compile(r"\s+")
TICKER_RE = re.compile(r"^[A-Z]{3,5}$")
STOP_OBJECTIVE_LINES = {
    "factsheet",
    "fund summary prospectus",
    "summary prospectus",
    "prospectus",
    "add pacer etfs to your portfolio",
    "fund details",
    "overview",
    "performance",
    "portfolio",
    "distributions",
    "materials",
    "fund documents",
    "save cash management",
    "separately managed accounts",
}

NON_FUND_PRODUCT_SLUGS = {
    "",
    "cash-cows",
    "pacer-custom-etf-series",
    "pacer-dividend-multiplier-series",
    "pacer-factor-etf-series",
    "pacer-leaders-series",
    "pacer-thematic-series",
    "structured-outcome-strategies",
}

LISTING_READY_JS = """
() => Array.from(document.querySelectorAll('a[href^="/products/"]'))
  .filter(anchor => (anchor.textContent || "").trim().length > 0)
  .length >= 40
"""


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now()


def env_flag(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "pacer_etfs_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = (
        str(value)
        .replace("\u00ad", "")
        .replace("\u00a0", " ")
        .replace("\r", "\n")
        .strip()
    )
    cleaned = SPACE_RE.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "None", "null", "N/A"} else cleaned


def normalize_search_text(value: object | None) -> str:
    cleaned = clean_text(value)
    cleaned = cleaned.replace("\u00ae", "").replace("\u2122", "").replace("\u2019", "'")
    return cleaned.casefold()


def normalize_url(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    absolute = urljoin(BASE_URL, cleaned)
    split_url = urlsplit(absolute)
    normalized_path = re.sub(r"/{2,}", "/", split_url.path).rstrip("/")
    if not normalized_path:
        normalized_path = "/"
    return urlunsplit(
        (
            split_url.scheme.lower(),
            split_url.netloc.lower(),
            normalized_path.lower(),
            split_url.query,
            "",
        )
    )


def parse_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    normalized = re.sub(r"[^\d.,+-]", "", cleaned)
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


def normalize_numeric_string(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    normalized = format(decimal_value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def percent_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value * Decimal("100"), places=2)


def money_to_millions(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def normalize_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def detect_currency_from_value(value: object | None, fallback: str = "USD") -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    if "$" in cleaned:
        return "USD"
    match = re.search(r"\b([A-Z]{3})\b", cleaned)
    if match:
        return match.group(1)
    return fallback


def is_detail_product_url(url: str) -> bool:
    split_url = urlsplit(url)
    segments = [segment for segment in split_url.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "products":
        return False
    if len(segments) == 2:
        return segments[1] not in NON_FUND_PRODUCT_SLUGS
    return len(segments) == 3 and segments[1] == "structured-outcome-strategies"


def guess_ticker_from_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    if not path:
        return ""
    return path.split("/")[-1].upper()


def choose_ticker(texts: list[str], url: str) -> str:
    for text in texts:
        candidate = clean_text(text).upper()
        if TICKER_RE.fullmatch(candidate):
            return candidate
    guessed = guess_ticker_from_url(url)
    return guessed if TICKER_RE.fullmatch(guessed) else guessed


def choose_name(texts: list[str], ticker: str) -> str:
    candidates = [
        text
        for text in texts
        if clean_text(text)
        and clean_text(text).upper() != ticker
        and clean_text(text).casefold() not in {"learn more", "series overview"}
    ]
    etf_candidates = [text for text in candidates if "ETF" in text.upper()]
    if etf_candidates:
        return max(etf_candidates, key=len)
    return max(candidates, key=len) if candidates else ""


def lines_from_text(value: str) -> list[str]:
    lines: list[str] = []
    for line in value.replace("\t", "\n").splitlines():
        cleaned = clean_text(line)
        if cleaned:
            lines.append(cleaned)
    return lines


def find_line_index(lines: list[str], candidates: list[str]) -> int:
    normalized_candidates = [normalize_search_text(candidate) for candidate in candidates if clean_text(candidate)]
    if not normalized_candidates:
        return -1
    for idx, line in enumerate(lines):
        normalized_line = normalize_search_text(line)
        if normalized_line in normalized_candidates:
            return idx
    for idx, line in enumerate(lines):
        normalized_line = normalize_search_text(line)
        if any(candidate and candidate in normalized_line for candidate in normalized_candidates):
            return idx
    return -1


def extract_category(lines: list[str], ticker: str, name_candidates: list[str]) -> str:
    index = find_line_index(lines[:120], name_candidates)
    if index <= 0:
        return ""
    for candidate in reversed(lines[max(0, index - 3) : index]):
        normalized = normalize_search_text(candidate)
        if not normalized:
            continue
        if normalized == normalize_search_text(ticker):
            continue
        if "series" in normalized or ">" in candidate:
            continue
        if "home" == normalized or "pacer etfs" == normalized:
            continue
        return candidate
    return ""


def extract_objective(lines: list[str], name_candidates: list[str]) -> str:
    index = find_line_index(lines[:160], name_candidates)
    if index < 0:
        return ""
    collected: list[str] = []
    for line in lines[index + 1 :]:
        normalized = normalize_search_text(line)
        if normalized in STOP_OBJECTIVE_LINES:
            break
        if TICKER_RE.fullmatch(line.upper()):
            continue
        collected.append(line)
    objective = clean_text(" ".join(collected))
    return objective


def extract_fund_details_section(body_text: str) -> str:
    start = body_text.find("Fund Details")
    if start < 0:
        return ""
    end = len(body_text)
    for marker in (
        "View Historical Premium/Discount",
        "Performance quoted represents past performance",
        "Overview\n",
        "\nOverview",
    ):
        marker_index = body_text.find(marker, start)
        if marker_index >= 0:
            end = min(end, marker_index)
    return body_text[start:end]


def is_known_detail_label(line: str) -> bool:
    normalized = normalize_search_text(line)
    if normalized.startswith("as of "):
        return True
    if normalized.startswith("implied liquidity"):
        return True
    if normalized.startswith("nav"):
        return True
    if normalized.startswith("market price"):
        return True
    return normalized in {
        "net assets",
        "shares outstanding",
        "fund ticker",
        "intraday nav (iiv)",
        "cusip#",
        "isin",
        "inception date",
        "total expenses",
        "number of securities",
        "30 day sec yield",
        "premium/discount",
    }


def parse_fund_details_section(section_text: str) -> dict[str, str]:
    lines = lines_from_text(section_text)
    data: dict[str, str] = {}
    if not lines:
        return data

    label_map = {
        "NAV Change in Dollars": "nav_change_dollars_raw",
        "NAV Change (%)": "nav_change_percentage_raw",
        "30-Day Median Bid/Ask Spread": "median_bid_ask_spread_30d",
        "Market Price Change in Dollars": "market_price_change_dollars_raw",
        "Market Price Change (%)": "market_price_change_percentage_raw",
        "Market Price": "market_price_raw",
        "Net Assets": "net_assets_raw",
        "Shares Outstanding": "shares_outstanding",
        "Fund Ticker": "fund_ticker",
        "Intraday NAV (IIV)": "intraday_nav_iiv",
        "CUSIP#": "cusip",
        "ISIN": "isin",
        "Inception Date": "inception_date_raw",
        "Total Expenses": "total_expenses_raw",
        "Number of Securities": "number_of_securities",
        "30 Day SEC Yield": "sec_yield_30d_raw",
        "Premium/Discount": "premium_discount_raw",
        "NAV": "nav_raw",
    }
    ordered_labels = sorted(label_map.keys(), key=len, reverse=True)

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        normalized = normalize_search_text(line)

        if normalized.startswith("as of "):
            raw_date = clean_text(line[5:].strip())
            data["fund_details_as_of_raw"] = raw_date
            data["fund_details_as_of"] = normalize_date(raw_date)
            idx += 1
            continue

        implied_match = re.match(
            r"^Implied Liquidity\d*\s+as of\s+([0-9/]+)\s+\((Shares|USD)\)\s*(.*)$",
            line,
            flags=re.IGNORECASE,
        )
        if implied_match:
            raw_date, liquidity_kind, inline_value = implied_match.groups()
            raw_value = clean_text(inline_value)
            if not raw_value and idx + 1 < len(lines) and not is_known_detail_label(lines[idx + 1]):
                raw_value = lines[idx + 1]
                idx += 1
            if liquidity_kind.lower() == "shares":
                data["implied_liquidity_shares_as_of_raw"] = raw_date
                data["implied_liquidity_shares_as_of"] = normalize_date(raw_date)
                data["implied_liquidity_shares_raw"] = raw_value
            else:
                data["implied_liquidity_usd_as_of_raw"] = raw_date
                data["implied_liquidity_usd_as_of"] = normalize_date(raw_date)
                data["implied_liquidity_usd_raw"] = raw_value
            idx += 1
            continue

        matched_label = ""
        matched_key = ""
        for label in ordered_labels:
            label_pattern = re.sub(r"[\d]+$", "", label)
            if line.startswith(label) or line.startswith(label_pattern):
                matched_label = label if line.startswith(label) else label_pattern
                matched_key = label_map[label]
                break

        if matched_key:
            inline_value = clean_text(line[len(matched_label) :].strip())
            value = inline_value
            if not value and idx + 1 < len(lines) and not is_known_detail_label(lines[idx + 1]):
                value = lines[idx + 1]
                idx += 1
            data[matched_key] = value
        idx += 1

    return data


def parse_fund_details_html(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    data: dict[str, str] = {}

    rate_date_node = soup.select_one("#fd-Rate-Date")
    if rate_date_node is not None:
        rate_date_text = clean_text(rate_date_node.get_text(" ", strip=True))
        rate_date_text = re.sub(r"^as of\s+", "", rate_date_text, flags=re.IGNORECASE)
        if rate_date_text:
            data["fund_details_as_of_raw"] = rate_date_text
            data["fund_details_as_of"] = normalize_date(rate_date_text)

    label_map = {
        "NAV Change in Dollars": "nav_change_dollars_raw",
        "NAV Change (%)": "nav_change_percentage_raw",
        "30-Day Median Bid/Ask Spread": "median_bid_ask_spread_30d",
        "Market Price Change in Dollars": "market_price_change_dollars_raw",
        "Market Price Change (%)": "market_price_change_percentage_raw",
        "Market Price": "market_price_raw",
        "Net Assets": "net_assets_raw",
        "Shares Outstanding": "shares_outstanding",
        "Fund Ticker": "fund_ticker",
        "Intraday NAV (IIV)": "intraday_nav_iiv",
        "CUSIP#": "cusip",
        "ISIN": "isin",
        "Inception Date": "inception_date_raw",
        "Total Expenses": "total_expenses_raw",
        "Number of Securities": "number_of_securities",
        "30 Day SEC Yield": "sec_yield_30d_raw",
        "Premium/Discount": "premium_discount_raw",
        "NAV": "nav_raw",
    }

    for row in soup.select("#fd-tbody tr"):
        label_node = row.select_one("th")
        value_node = row.select_one("td")
        if label_node is None or value_node is None:
            continue

        label = clean_text(label_node.get_text(" ", strip=True))
        value = clean_text(value_node.get_text(" ", strip=True))
        if not label:
            continue

        normalized_label = re.sub(r"\s+\d+$", "", label).strip()
        mapped_key = label_map.get(normalized_label)
        if mapped_key:
            data[mapped_key] = value

    return data


def extract_document_links(anchors: list[dict[str, str]]) -> dict[str, str]:
    documents = {
        "factsheet_url": "",
        "fund_summary_prospectus_url": "",
        "prospectus_url": "",
        "daily_holdings_url": "",
        "distribution_schedule_url": "",
        "historical_premium_discount_url": "",
    }
    for anchor in anchors:
        text = clean_text(anchor.get("text"))
        href = normalize_url(anchor.get("href"))
        if not text or not href:
            continue
        normalized = text.casefold()
        if not documents["factsheet_url"] and "factsheet" in normalized:
            documents["factsheet_url"] = href
        elif not documents["fund_summary_prospectus_url"] and "fund summary prospectus" in normalized:
            documents["fund_summary_prospectus_url"] = href
        elif not documents["prospectus_url"] and normalized == "prospectus":
            documents["prospectus_url"] = href
        elif not documents["daily_holdings_url"] and "daily holdings" in normalized:
            documents["daily_holdings_url"] = href
        elif not documents["distribution_schedule_url"] and "distribution schedule" in normalized:
            documents["distribution_schedule_url"] = href
        elif not documents["historical_premium_discount_url"] and "historical premium/discount" in normalized:
            documents["historical_premium_discount_url"] = href
    return documents


async def build_browser_context(browser) -> BrowserContext:
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 2200},
        locale="en-US",
    )
    await context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = window.chrome || { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """
    )
    return context


async def wait_for_listing_ready(page: Page) -> None:
    await page.wait_for_function(LISTING_READY_JS, timeout=DETAIL_READY_TIMEOUT_MS)


async def wait_for_detail_ready(page: Page) -> None:
    await page.wait_for_selector("#fd-tbody tr", timeout=DETAIL_READY_TIMEOUT_MS)


async def discover_detail_pages(page: Page) -> list[dict[str, str]]:
    await wait_for_listing_ready(page)
    raw_links = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href^="/products/"]')).map(anchor => ({
            href: anchor.getAttribute("href") || "",
            text: (anchor.textContent || "").trim(),
        }))"""
    )

    grouped: dict[str, list[str]] = {}
    for item in raw_links:
        href = normalize_url(item.get("href"))
        text = clean_text(item.get("text"))
        if not href or not is_detail_product_url(href):
            continue
        grouped.setdefault(href, [])
        if text:
            grouped[href].append(text)

    detail_rows: list[dict[str, str]] = []
    for href, texts in grouped.items():
        ticker = choose_ticker(texts, href)
        name = choose_name(texts, ticker)
        detail_rows.append(
            {
                "detail_url": href,
                "ticker": ticker,
                "listing_name": name,
            }
        )
    detail_rows.sort(key=lambda row: (row["ticker"], row["detail_url"]))
    if not detail_rows:
        raise RuntimeError("No Pacer ETF detail pages were discovered on the official products page.")
    return detail_rows


async def extract_page_payload(page: Page) -> dict[str, Any]:
    return await page.evaluate(
        """() => ({
            title: document.title || "",
            html: document.documentElement ? document.documentElement.outerHTML : "",
            anchors: Array.from(document.querySelectorAll('a[href]')).map(anchor => ({
                text: (anchor.textContent || "").trim(),
                href: anchor.href || "",
            })),
        })"""
    )


async def scrape_detail_page(
    page: Page,
    detail_row: dict[str, str],
    index: int,
    total: int,
) -> dict[str, str]:
    ticker = detail_row["ticker"]
    row: dict[str, str] = {
        "etf_name": detail_row["listing_name"],
        "issuer": ISSUER,
        "ticker": ticker,
        "detail_url": detail_row["detail_url"],
        "listing_name": detail_row["listing_name"],
        "source_kind": "detail_page_dom",
    }

    logging.info("Fetching Pacer detail [%d/%d] %s", index, total, ticker)
    try:
        payload: dict[str, Any] = {}
        html = ""
        last_error = ""
        for attempt in range(1, MAX_DETAIL_ATTEMPTS + 1):
            await page.goto(detail_row["detail_url"], wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
            try:
                await wait_for_detail_ready(page)
            except Exception as wait_exc:
                payload = await extract_page_payload(page)
                html = str(payload.get("html") or "")
                html_text = clean_text(html)
                if "Performing security verification" in html_text:
                    last_error = "Cloudflare security verification page returned instead of the ETF detail content."
                elif "Enable JavaScript and cookies to continue" in html_text or "Just a moment..." in html_text:
                    last_error = "Cloudflare challenge page returned instead of the ETF detail content."
                else:
                    last_error = str(wait_exc)
            else:
                payload = await extract_page_payload(page)
                html = str(payload.get("html") or "")
                last_error = ""
                break

            if attempt < MAX_DETAIL_ATTEMPTS:
                logging.info(
                    "Retrying Pacer detail [%d/%d] %s after attempt %d/%d: %s",
                    index,
                    total,
                    ticker,
                    attempt,
                    MAX_DETAIL_ATTEMPTS,
                    last_error,
                )
                await page.wait_for_timeout(4000 * attempt)
        if last_error:
            raise RuntimeError(last_error)

        documents = extract_document_links(payload.get("anchors", []))
        fund_details = parse_fund_details_html(html)
        if not fund_details:
            raise RuntimeError("Fund details table was not found in the Pacer ETF detail HTML.")

        row.update(
            {
                "page_title": clean_text(payload.get("title")),
                "resolved_url": normalize_url(page.url),
                "category": "",
                "objective": "",
                "product_page_url": detail_row["detail_url"],
                "factsheet_url": documents["factsheet_url"],
                "fund_summary_prospectus_url": documents["fund_summary_prospectus_url"],
                "prospectus_url": documents["prospectus_url"],
                "daily_holdings_url": documents["daily_holdings_url"],
                "distribution_schedule_url": documents["distribution_schedule_url"],
                "historical_premium_discount_url": documents["historical_premium_discount_url"],
            }
        )
        row.update(fund_details)

        if row.get("net_assets_raw"):
            row["aum_mn"] = money_to_millions(row["net_assets_raw"])
            row["aum_currency"] = detect_currency_from_value(row["net_assets_raw"], fallback="USD")
        if row.get("nav_raw") and not row.get("nav"):
            row["nav"] = normalize_numeric_string(row["nav_raw"])
        if row.get("market_price_raw") and not row.get("market_price"):
            row["market_price"] = normalize_numeric_string(row["market_price_raw"])
        if row.get("total_expenses_raw"):
            row["ter_bps"] = percent_to_bps(row["total_expenses_raw"])
        if row.get("inception_date_raw"):
            row["inception_date"] = normalize_date(row["inception_date_raw"])
        if row.get("fund_details_as_of") and not row.get("rate_date"):
            row["rate_date"] = row["fund_details_as_of"]
            row["rate_date_raw"] = row.get("fund_details_as_of_raw", "")
        if row.get("fund_ticker"):
            row["ticker"] = row["fund_ticker"]

        row["fetch_status"] = "ok"
        logging.info("Completed Pacer detail [%d/%d] %s", index, total, ticker)
        return row
    except Exception as exc:
        row["fetch_status"] = "failed"
        row["error"] = str(exc)
        logging.warning("Pacer detail [%d/%d] %s failed: %s", index, total, ticker, exc)
        return row


async def scrape_all_details(
    context: BrowserContext,
    detail_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    page = await context.new_page()
    results: list[dict[str, str]] = []
    try:
        for index, detail_row in enumerate(detail_rows, 1):
            if page.is_closed():
                page = await context.new_page()
            result = await scrape_detail_page(
                page=page,
                detail_row=detail_row,
                index=index,
                total=len(detail_rows),
            )
            results.append(result)
            await page.wait_for_timeout(1500)
    finally:
        if not page.is_closed():
            await page.close()
    return results

async def run() -> Path:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=env_flag("PACER_HEADLESS", False),
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await build_browser_context(browser)
        page = await context.new_page()

        try:
            logging.info("Opening Pacer products page")
            await page.goto(PRODUCTS_URL, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
            detail_rows = await discover_detail_pages(page)
            logging.info("Discovered %d Pacer ETF detail URL(s)", len(detail_rows))
            rows = await scrape_all_details(context, detail_rows)
        finally:
            await page.close()
            await context.close()
            await browser.close()

    rows.sort(key=lambda row: clean_text(row.get("ticker")).upper())
    status_counts: dict[str, int] = {}
    for row in rows:
        status = clean_text(row.get("fetch_status")) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    payload = {
        "source": {
            "provider": ISSUER,
            "products_url": PRODUCTS_URL,
        },
        "method": (
            "Official products page detail discovery + per-detail-page DOM extraction"
        ),
        "captured_at": now.isoformat(),
        "detail_url_count": len(detail_rows),
        "row_count": len(rows),
        "status_counts": status_counts,
        "listing_rows": rows,
    }
    write_json(output_path, payload)
    logging.info("Saved %d Pacer ETF row(s) to %s", len(rows), output_path)
    return output_path


def main() -> None:
    output_path = asyncio.run(run())
    print(output_path)


if __name__ == "__main__":
    main()
