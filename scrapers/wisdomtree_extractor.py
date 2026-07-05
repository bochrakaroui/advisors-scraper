"""Download WisdomTree Europe UCITS ETF data from product detail pages."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright


START_URL = "https://www.wisdomtree.eu/products?structure=UCITS+ETFs"
ISSUER = "WisdomTree"
PROVIDER = "WisdomTree"
BASE_URL = "https://www.wisdomtree.eu"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "wisdomtree"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
TIMEOUT_MS = 120_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
DATE_PATTERN = re.compile(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})")
PRODUCT_URL_PATTERN = re.compile(r"^https://www\.wisdomtree\.eu/en-gb/etfs/[^?#]+$")
SUPPLEMENTAL_PRODUCT_URLS = (
    "https://www.wisdomtree.eu/en-gb/etfs/commodities/weng---wisdomtree-strategic-metals-ucits-etf---gbp-hedged-acc",
    "https://www.wisdomtree.eu/en-gb/etfs/commodities/wenu---wisdomtree-strategic-metals-ucits-etf---usd-acc",
)
FLAG_COUNTRY_MAP = {
    "gbr": "United Kingdom",
    "deu": "Germany",
    "ita": "Italy",
    "che": "Switzerland",
    "fra": "France",
    "nld": "Netherlands",
    "esp": "Spain",
    "swe": "Sweden",
}
MIN_PRODUCT_URL_COUNT = 10


def build_run_output_dir(base_dir: Path, run_date: str) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    output_dir = base_dir / run_date
    suffix = 1
    while output_dir.exists():
        output_dir = base_dir / f"{run_date} ({suffix})"
        suffix += 1
    output_dir.mkdir(parents=True, exist_ok=False)
    os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name
    return output_dir


def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "wisdomtree_etf_export.json"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def normalize_header(value: object | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def normalize_ter_bps(raw_value: str) -> str:
    cleaned = clean_text(raw_value).replace("%", "").strip()
    if not cleaned:
        return ""
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        ter_pct = Decimal(cleaned)
    except InvalidOperation:
        return ""
    return format_decimal(ter_pct * Decimal("100"), places=2)


def normalize_amount_text(raw_value: str) -> str:
    compact = clean_text(raw_value).replace("Ã‚", "").replace(" ", "")
    if "," in compact and "." not in compact:
        parts = compact.split(",")
        if len(parts) > 2 or len(parts[-1]) == 3:
            return compact.replace(",", "")
        return compact.replace(",", ".")
    return compact.replace(",", "")


def normalize_aum_millions(raw_value: str) -> tuple[str, str]:
    cleaned = clean_text(raw_value).replace("Ã¢â€šÂ¬", "â‚¬").replace("Ã‚Â£", "Â£")
    if not cleaned:
        return "", ""

    currency_code = ""
    for symbol, code in (("â‚¬", "EUR"), ("$", "USD"), ("Â£", "GBP")):
        if symbol in cleaned:
            currency_code = code
            cleaned = cleaned.replace(symbol, "")

    if not currency_code:
        upper_cleaned = cleaned.upper()
        for prefix, code in (("EUR", "EUR"), ("USD", "USD"), ("GBP", "GBP")):
            if upper_cleaned.startswith(prefix):
                currency_code = code
                cleaned = cleaned[len(prefix):]
                break

    normalized = re.sub(r"[^0-9,.\-]", "", normalize_amount_text(cleaned))
    if not normalized:
        return "", currency_code

    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return "", currency_code

    millions = amount / Decimal("1000000")
    millions_text = format(millions, "f").rstrip("0").rstrip(".")
    return millions_text or "0", currency_code


def normalize_product_url(url: str) -> str:
    parsed = urlparse(urljoin(BASE_URL, clean_text(url)))
    normalized = parsed._replace(query="", fragment="").geturl()
    return normalized.rstrip("/")


def is_product_url(url: str) -> bool:
    return bool(PRODUCT_URL_PATTERN.fullmatch(normalize_product_url(url)))


def extract_country_value(cell) -> str:
    texts = [clean_text(text) for text in cell.stripped_strings]
    alt_texts = [clean_text(image.get("alt")) for image in cell.find_all("img")]
    combined = " ".join(part for part in alt_texts + texts if part)
    combined = combined.replace("Image:", "").strip()
    return combined


def extract_product_name(soup: BeautifulSoup) -> str:
    heading = soup.find("h1")
    if heading:
        return clean_text(heading.get_text(" ", strip=True))

    title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    return clean_text(title.split("|", 1)[0])


def extract_overview_metrics(soup: BeautifulSoup) -> dict[str, str]:
    metrics: dict[str, str] = {
        "base_currency": "",
        "ter_raw": "",
        "ter_bps": "",
        "aum_raw": "",
        "aum_numeric": "",
        "aum_m": "",
        "aum_currency": "",
        "as_of_date": "",
    }

    overview_section = soup.select_one("#fund-overview")
    if overview_section:
        for row in overview_section.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            key = normalize_header(cells[0].get_text(" ", strip=True))
            value = clean_text(cells[1].get_text(" ", strip=True))
            if key == "basecurrency":
                metrics["base_currency"] = value.upper()
            elif key == "ter":
                metrics["ter_raw"] = value
                metrics["ter_bps"] = normalize_ter_bps(value)

    nav_section = soup.select_one("#fund-nav")
    if nav_section:
        nav_header = nav_section.select_one("thead th:nth-child(2)")
        if nav_header:
            date_match = DATE_PATTERN.search(clean_text(nav_header.get_text(" ", strip=True)))
            if date_match:
                metrics["as_of_date"] = clean_text(date_match.group(1))

        for row in nav_section.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            key = normalize_header(cells[0].get_text(" ", strip=True))
            value = clean_text(cells[1].get_text(" ", strip=True))
            if key == "totalaumoffund":
                metrics["aum_raw"] = value
                metrics["aum_numeric"], metrics["aum_currency"] = normalize_aum_millions(value)
                metrics["aum_m"] = metrics["aum_numeric"]

    return metrics


def extract_country_from_listing_cell(cell) -> str:
    image = cell.find("img")
    if image:
        source = clean_text(image.get("src"))
        match = re.search(r"/flags/([a-z]{3})\.", source, flags=re.IGNORECASE)
        if match:
            return FLAG_COUNTRY_MAP.get(match.group(1).lower(), match.group(1).upper())
    return extract_country_value(cell)


def extract_listings_from_table(soup: BeautifulSoup) -> list[dict[str, str]]:
    listings: list[dict[str, str]] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        headers = [normalize_header(cell.get_text(" ", strip=True)) for cell in header_cells]
        if not {"country", "exchange", "tradingcurrency", "exchangeticker", "isin"}.issubset(headers):
            continue

        header_indexes = {header: index for index, header in enumerate(headers)}
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < len(headers):
                continue

            listing = {
                "country": extract_country_from_listing_cell(cells[header_indexes["country"]]),
                "exchange": clean_text(cells[header_indexes["exchange"]].get_text(" ", strip=True)),
                "ccy": clean_text(cells[header_indexes["tradingcurrency"]].get_text(" ", strip=True)).upper(),
                "ticker": clean_text(cells[header_indexes["exchangeticker"]].get_text(" ", strip=True)).upper(),
                "isin": clean_text(cells[header_indexes["isin"]].get_text(" ", strip=True)).upper(),
            }
            if ISIN_PATTERN.fullmatch(listing["isin"]):
                listings.append(listing)

    return listings


def extract_listings_from_text(page_text: str) -> list[dict[str, str]]:
    lines = [clean_text(line) for line in page_text.splitlines()]
    lines = [line for line in lines if line]

    listings: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_listings = False

    for line in lines:
        if not in_listings:
            if line == "Listings & Codes":
                in_listings = True
            continue

        if line.startswith("####") or line in {"Holdings", "Documents"} or line.startswith("Performance is total return"):
            break

        if line.startswith("Country"):
            if current and ISIN_PATTERN.fullmatch(current.get("isin", "")):
                listings.append(current)
            current = {"country": clean_text(line.removeprefix("Country")).replace("Image:", "").strip()}
            continue

        if current is None:
            continue

        if line.startswith("Exchange Ticker"):
            current["ticker"] = clean_text(line.removeprefix("Exchange Ticker")).upper()
        elif line.startswith("Trading Currency"):
            current["ccy"] = clean_text(line.removeprefix("Trading Currency")).upper()
        elif line.startswith("Exchange"):
            current["exchange"] = clean_text(line.removeprefix("Exchange"))
        elif line.startswith("ISIN"):
            current["isin"] = clean_text(line.removeprefix("ISIN")).upper()

    if current and ISIN_PATTERN.fullmatch(current.get("isin", "")):
        listings.append(current)

    return listings


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (
            clean_text(row.get("isin")).upper(),
            clean_text(row.get("exchange")),
            clean_text(row.get("ticker")).upper(),
            clean_text(row.get("ccy")).upper(),
            clean_text(row.get("country")),
        )
        deduped.setdefault(key, row)
    return list(deduped.values())


def load_historical_product_urls() -> list[str]:
    historical_urls: set[str] = set()
    for snapshot_path in sorted(OUTPUT_DIR.rglob("wisdomtree_etf_export.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        rows = payload.get("rows", [])
        if not isinstance(rows, list) or not rows:
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue
            product_url = clean_text(row.get("product_url") or row.get("source_url"))
            if product_url and is_product_url(product_url):
                historical_urls.add(normalize_product_url(product_url))

        if historical_urls:
            break

    return sorted(historical_urls)


async def collect_product_urls(page) -> list[str]:
    await page.goto(START_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(5_000)
    html = await page.content()
    if "Attention Required! | Cloudflare" in html or "Sorry, you have been blocked" in html:
        raise ValueError("WisdomTree products page was blocked by Cloudflare.")
    discovered_urls = {
        normalize_product_url(href)
        for href in await page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
        if clean_text(href).startswith(f"{BASE_URL}/en-gb/etfs/")
    }

    urls = sorted(discovered_urls | {normalize_product_url(url) for url in SUPPLEMENTAL_PRODUCT_URLS})
    if len(urls) < MIN_PRODUCT_URL_COUNT:
        historical_urls = load_historical_product_urls()
        if historical_urls:
            print(
                f"[WARN] WisdomTree live product discovery only found {len(urls)} links; "
                f"reusing {len(historical_urls)} historical product URLs."
            )
            urls = sorted(set(urls) | set(historical_urls))
    if not urls:
        raise ValueError("Could not find any WisdomTree ETF detail links on the filtered products page.")
    return urls


async def new_wisdomtree_context(browser):
    context = await browser.new_context(
        locale="en-GB",
        timezone_id="Europe/London",
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 1600},
    )
    return context


async def scrape_detail_rows(page, product_url: str, scraped_at: str) -> list[dict[str, str]]:
    await page.goto(product_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(5_000)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    etf_name = extract_product_name(soup)
    metrics = extract_overview_metrics(soup)
    listings = extract_listings_from_table(soup)
    if not listings:
        page_text = await page.locator("body").inner_text()
        listings = extract_listings_from_text(page_text)

    if not listings:
        raise ValueError("No Listings & Codes rows were parsed from the product detail page.")

    rows: list[dict[str, str]] = []
    for listing in listings:
        isin = clean_text(listing.get("isin")).upper()
        if not ISIN_PATTERN.fullmatch(isin):
            continue

        rows.append(
            {
                "provider": PROVIDER,
                "issuer": ISSUER,
                "etf_name": etf_name,
                "ticker": clean_text(listing.get("ticker")).upper(),
                "exchange": clean_text(listing.get("exchange")),
                "country": clean_text(listing.get("country")),
                "ccy": clean_text(listing.get("ccy") or metrics["base_currency"]).upper(),
                "base_currency": metrics["base_currency"],
                "isin": isin,
                "aum_raw": metrics["aum_raw"],
                "aum_numeric": metrics["aum_numeric"],
                "aum_m": metrics["aum_m"],
                "aum_currency": metrics["aum_currency"],
                "ter_raw": metrics["ter_raw"],
                "ter_bps": metrics["ter_bps"],
                "product_url": product_url,
                "source_url": product_url,
                "scraped_at": scraped_at,
                "as_of_date": metrics["as_of_date"],
            }
        )

    if not rows:
        raise ValueError("No valid listing rows with ISIN were produced from the product detail page.")

    return dedupe_rows(rows)


async def build_snapshot(now: datetime) -> dict[str, object]:
    scraped_at = now.isoformat()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        product_urls: list[str] = []
        last_listing_error: Exception | None = None
        for _ in range(3):
            listing_context = await new_wisdomtree_context(browser)
            listing_page = await listing_context.new_page()
            await listing_page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            try:
                product_urls = await collect_product_urls(listing_page)
                last_listing_error = None
                break
            except Exception as exc:
                last_listing_error = exc
                await asyncio.sleep(2)
            finally:
                await listing_page.close()
                await listing_context.close()

        if last_listing_error is not None:
            raise last_listing_error
        print(f"Start URL used: {START_URL}")
        print(f"Product detail links found: {len(product_urls):,}")

        rows: list[dict[str, str]] = []
        warnings: list[str] = []

        for index, product_url in enumerate(product_urls, start=1):
            last_error: Exception | None = None
            for attempt in range(2):
                detail_context = await new_wisdomtree_context(browser)
                detail_page = await detail_context.new_page()
                await detail_page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                try:
                    detail_rows = await scrape_detail_rows(detail_page, product_url, scraped_at=scraped_at)
                    rows.extend(detail_rows)
                    print(f"[{index}/{len(product_urls)}] Listings extracted: {len(detail_rows):,} -> {product_url}")
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                finally:
                    await detail_page.close()
                    await detail_context.close()

            if last_error is not None:
                if isinstance(last_error, PlaywrightTimeoutError):
                    warning = f"{product_url} -> timed out"
                else:
                    warning = f"{product_url} -> {last_error}"
                warnings.append(warning)
                print(f"[WARN] WisdomTree detail page failed: {warning}")

        await browser.close()

    rows = dedupe_rows(rows)
    missing_aum_count = sum(1 for row in rows if not clean_text(row.get("aum_numeric")))
    missing_ccy_count = sum(1 for row in rows if not clean_text(row.get("ccy")))
    valid_isin_count = sum(1 for row in rows if ISIN_PATTERN.fullmatch(clean_text(row.get("isin")).upper()))

    print(f"Rows extracted: {len(rows):,}")
    print(f"Valid ISINs: {valid_isin_count:,}")
    print(f"Missing CCY values: {missing_ccy_count:,}")
    print(f"Missing AUM values: {missing_aum_count:,}")

    return {
        "source_url": START_URL,
        "method": "filtered products page -> ETF detail pages",
        "captured_at": scraped_at,
        "warning_count": len(warnings),
        "warnings": warnings,
        "rows": rows,
    }


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def download_snapshot(destination: Path) -> None:
    snapshot = asyncio.run(build_snapshot(datetime.now()))
    write_json(destination, snapshot)
    print(f"Raw snapshot saved: {destination}")


async def download_wisdomtree_file() -> Path:
    output_path = build_output_path(datetime.now())
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected WisdomTree snapshot in {path}: expected a list of rows.")
    return rows


def main() -> None:
    output_path = asyncio.run(download_wisdomtree_file())
    print(f"Done! Open your file at: {output_path.resolve()}")


if __name__ == "__main__":
    main()
