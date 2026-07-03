"""Download PIMCO UK ETF data from the official GB ETF pages."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests

try:
    from playwright.async_api import Page, async_playwright
except ModuleNotFoundError as exc:  # pragma: no cover - handled at runtime for user guidance
    raise ModuleNotFoundError(
        "playwright is required for the PIMCO scraper. Install it with "
        "'pip install playwright' and run 'playwright install chromium'."
    ) from exc


ISSUER = "PIMCO"
PAGE_URL = "https://www.pimco.com/gb/en/investment-strategies/etfs#af8aeb93-d53f-48fa-91dd-358814aa45f9"
SITEMAP_URL = "https://www.pimco.com/gb/en/sitemap.xml"
REQUEST_TIMEOUT_S = 45
PLAYWRIGHT_TIMEOUT_MS = 120000
API_CAPTURE_WAIT_MS = 30000
MAX_CONCURRENT_PAGES = 3
DETAIL_PAGE_MAX_ATTEMPTS = 3
DETAIL_PAGE_RETRY_WAIT_MS = 3000
PIMCO_API_BASE_URL = "https://fund-ui.pimco.com/fund-detail-api"
REQUIRED_PAYLOAD_KEYS = ("as_of_date", "metadata", "share_classes", "fund_stats")

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "PIMCO"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": PAGE_URL,
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SPACE_PATTERN = re.compile(r"\s+")
ETF_URL_PATTERN = re.compile(r"https://www\.pimco\.com/gb/en/investments/etf/[^<\s]+")
BOOTSTRAP_PATTERN = re.compile(r"PIMCO\.FundIntegration\.Init\.App\(window, '([^']+)'\)\.load\(\)")

TARGET_ISINS = [
    "IE000J46YVX2",
    "IE000KXNPEV8",
    "IE000Y2B34V0",
    "IE00B4P11460",
    "IE00B622SG73",
    "IE00B67B7N93",
    "IE00B7N3YW49",
    "IE00BD26N851",
    "IE00BF8HV600",
    "IE00BH3X8336",
    "IE00BK9YKZ79",
    "IE00BP9F2H18",
    "IE00BVZ6SQ11",
    "IE00BYXVWC37",
]


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "pimco_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None", "null"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def filter_target_results(
    records: list[dict[str, Any]],
    listing_rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    rows_by_isin = {
        normalize_isin(row.get("isin")): row
        for row in listing_rows
        if normalize_isin(row.get("isin"))
    }
    records_by_isin = {
        normalize_isin(get_dict(record.get("listing_row")).get("isin")): record
        for record in records
        if normalize_isin(get_dict(record.get("listing_row")).get("isin"))
    }

    filtered_rows = [rows_by_isin[isin] for isin in TARGET_ISINS if isin in rows_by_isin]
    filtered_records = [records_by_isin[isin] for isin in TARGET_ISINS if isin in records_by_isin]
    missing_target_isins = [isin for isin in TARGET_ISINS if isin not in rows_by_isin]
    return filtered_records, filtered_rows, missing_target_isins


def get_dict(value: object | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def to_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value).replace(",", "").replace("%", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def amount_to_millions(value: object | None) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def percent_to_bps(value: object | None) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return ""
    return str(int((decimal_value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def fetch_detail_urls() -> list[str]:
    response = SESSION.get(SITEMAP_URL, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return sorted(set(ETF_URL_PATTERN.findall(response.text)))


def decode_bootstrap_context(html: str) -> dict[str, Any]:
    match = BOOTSTRAP_PATTERN.search(html)
    if not match:
        raise RuntimeError("PIMCO detail page bootstrap payload was not found.")
    payload = base64.b64decode(match.group(1)).decode("utf-8")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise RuntimeError("PIMCO detail page bootstrap payload was not a JSON object.")
    return decoded


def fetch_detail_bootstrap(url: str) -> dict[str, Any]:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    bootstrap = decode_bootstrap_context(response.text)
    bootstrap["detail_url"] = url
    return bootstrap


def fetch_bootstrap_contexts(detail_urls: list[str]) -> list[dict[str, Any]]:
    bootstrap_contexts: list[dict[str, Any]] = []
    total = len(detail_urls)
    for index, url in enumerate(detail_urls, start=1):
        bootstrap_contexts.append(fetch_detail_bootstrap(url))
        if index == 1 or index == total or index % 5 == 0:
            logging.info("Fetched PIMCO bootstrap context [%d/%d]", index, total)
    return bootstrap_contexts


async def accept_gate_if_present(page: Page) -> None:
    try:
        await page.locator("#onetrust-accept-btn-handler").click(timeout=5000)
    except Exception:  # noqa: BLE001
        pass

    await page.wait_for_timeout(1000)

    modal = page.locator("#VisitorSettingsModalContent")
    try:
        visible = await modal.is_visible()
    except Exception:  # noqa: BLE001
        visible = False

    if not visible:
        return

    for selector in (
        'input[name="role"][value="Individual Investor"]',
        "#termsAgree",
        "#VisitorSettingsModalContent .submit-button button",
    ):
        try:
            await page.locator(selector).click(force=True, timeout=5000)
        except Exception:  # noqa: BLE001
            continue

    await page.wait_for_timeout(5000)


def get_api_base_url(bootstrap: dict[str, Any]) -> str:
    asset = get_dict(bootstrap.get("asset"))
    api_url = clean_text(asset.get("apiUrl")).rstrip("/")
    return api_url or PIMCO_API_BASE_URL


def identify_response_key(url: str, cusip: str) -> str:
    if f"/fund-detail-api/api/as-of-date?cusip={cusip}" in url:
        return "as_of_date"
    if f"/fund-detail-api/api/funds/{cusip}/metadata/GB" in url:
        return "metadata"
    if f"/fund-detail-api/api/funds/{cusip}/share-classes" in url:
        return "share_classes"
    if f"/fund-detail-api/api/funds/{cusip}/fund-stats" in url:
        return "fund_stats"
    return ""


def build_api_urls(bootstrap: dict[str, Any], cusip: str) -> dict[str, str]:
    base_url = get_api_base_url(bootstrap)
    return {
        "as_of_date": f"{base_url}/api/as-of-date?cusip={cusip}",
        "metadata": f"{base_url}/api/funds/{cusip}/metadata/GB",
        "share_classes": f"{base_url}/api/funds/{cusip}/share-classes",
        "fund_stats": f"{base_url}/api/funds/{cusip}/fund-stats",
    }


def fetch_missing_payloads_direct(
    bootstrap: dict[str, Any],
    detail_url: str,
    cusip: str,
    missing_keys: set[str],
) -> dict[str, Any]:
    headers = dict(HEADERS)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = detail_url
    payloads: dict[str, Any] = {}

    for key, api_url in build_api_urls(bootstrap, cusip).items():
        if key not in missing_keys:
            continue
        response = SESSION.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        payloads[key] = response.json()

    return payloads


async def capture_payloads_for_detail(
    page: Page,
    bootstrap: dict[str, Any],
    url: str,
    cusip: str,
) -> dict[str, Any]:
    captured: dict[str, str] = {}
    tasks: list[asyncio.Task[None]] = []

    async def handle_response(response: Any) -> None:
        key = identify_response_key(response.url, cusip)
        if not key or key in captured:
            return
        try:
            captured[key] = await response.text()
        except Exception as exc:  # noqa: BLE001
            captured[key] = json.dumps({"error": str(exc)})

    def response_listener(response: Any) -> None:
        tasks.append(asyncio.create_task(handle_response(response)))

    page.on("response", response_listener)
    try:
        await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        await accept_gate_if_present(page)

        waited_ms = 0
        while len(captured) < 4 and waited_ms < API_CAPTURE_WAIT_MS:
            await page.wait_for_timeout(1000)
            waited_ms += 1000

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        missing = set(REQUIRED_PAYLOAD_KEYS) - set(captured)
        if missing:
            direct_payloads = await asyncio.to_thread(
                fetch_missing_payloads_direct,
                bootstrap,
                url,
                cusip,
                missing,
            )
            if direct_payloads:
                logging.info(
                    "Filled %d missing PIMCO payload(s) for %s via direct API fallback: %s",
                    len(direct_payloads),
                    cusip,
                    sorted(direct_payloads),
                )
                for key, payload in direct_payloads.items():
                    captured[key] = json.dumps(payload)

        missing = set(REQUIRED_PAYLOAD_KEYS) - set(captured)
        if missing:
            raise RuntimeError(f"Missing PIMCO API payloads for {cusip}: {sorted(missing)}")

        return {key: json.loads(value) for key, value in captured.items()}
    finally:
        page.remove_listener("response", response_listener)


def build_listing_row(detail_url: str, bootstrap: dict[str, Any], payloads: dict[str, Any]) -> dict[str, str]:
    metadata = get_dict(payloads.get("metadata"))
    share_classes = get_dict(payloads.get("share_classes"))
    fund_stats = get_dict(payloads.get("fund_stats"))
    as_of_date = get_dict(payloads.get("as_of_date"))
    share_class = get_dict(share_classes.get("shareClass"))
    oldest_share_class = get_dict(share_classes.get("oldestShareClass"))

    ter_percent_value = metadata.get("unifiedFeeWaiver")
    if ter_percent_value in {None, ""}:
        ter_percent_value = metadata.get("unifiedFee")

    aum_raw = fund_stats.get("totalNetAssetsDaily")
    aum_date = clean_text(fund_stats.get("totalNetAssetsAsOfDateDaily"))
    aum_currency = clean_text(fund_stats.get("totalNetAssetsCurrencyDaily")).upper()
    if aum_raw in {None, ""}:
        aum_raw = fund_stats.get("totalNetAssets")
        aum_date = clean_text(fund_stats.get("totalNetAssetsAsOfDate"))
        aum_currency = clean_text(fund_stats.get("totalNetAssetsCurrency")).upper()

    return {
        "etf_name": clean_text(metadata.get("fundName") or metadata.get("displayFundName")),
        "issuer": ISSUER,
        "detail_url": detail_url,
        "cusip": clean_text(metadata.get("cusip") or bootstrap.get("cusip")),
        "ticker": clean_text(metadata.get("ticker") or share_class.get("ticker")).upper(),
        "isin": normalize_isin(metadata.get("isin") or share_class.get("isin")),
        "ccy": clean_text(metadata.get("shareClassCurrency") or oldest_share_class.get("navCurrency")).upper(),
        "base_currency": clean_text(metadata.get("baseCurrency")).upper(),
        "share_class_code": clean_text(metadata.get("shareClassCode") or share_class.get("code")),
        "share_type": clean_text(oldest_share_class.get("displayShareType") or oldest_share_class.get("shareType")),
        "trust_code": clean_text(metadata.get("trustCode")),
        "primary_benchmark": clean_text(metadata.get("primaryBenchmark")),
        "sfdr": clean_text(metadata.get("sfdrAcctStatusName")),
        "share_class_inception": clean_text(metadata.get("shareClassInception")),
        "dividend_frequency": clean_text(metadata.get("dividendFrequency")),
        "ter_percent": clean_text(ter_percent_value),
        "ter_bps": percent_to_bps(ter_percent_value),
        "aum_mn": amount_to_millions(aum_raw),
        "aum_raw": clean_text(aum_raw),
        "aum_date": aum_date,
        "aum_currency": aum_currency,
        "daily_nav": clean_text(fund_stats.get("dailyNav")),
        "daily_nav_currency": clean_text(fund_stats.get("navCurrency")).upper(),
        "daily_nav_date": clean_text(as_of_date.get("latestDay")),
        "month_end_date": clean_text(as_of_date.get("latestMonthEnd")),
        "quarter_end_date": clean_text(as_of_date.get("latestQuarterEnd")),
        "bootstrap_fund_page_title": clean_text(bootstrap.get("fundPageTitle")),
        "bootstrap_country_code": clean_text(bootstrap.get("countryCode")),
        "bootstrap_site_name": clean_text(bootstrap.get("siteName")),
    }


async def process_detail_page(
    context: Any,
    bootstrap: dict[str, Any],
    index: int,
    total: int,
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, Any], dict[str, str]]:
    detail_url = clean_text(bootstrap.get("detail_url"))
    cusip = clean_text(bootstrap.get("cusip"))
    logging.info("Fetching PIMCO detail [%d/%d] %s", index, total, cusip or detail_url)

    last_error: Exception | None = None
    for attempt in range(1, DETAIL_PAGE_MAX_ATTEMPTS + 1):
        try:
            async with semaphore:
                page = await context.new_page()
                try:
                    payloads = await capture_payloads_for_detail(page, bootstrap, detail_url, cusip)
                finally:
                    await page.close()
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= DETAIL_PAGE_MAX_ATTEMPTS:
                raise
            logging.warning(
                "Retrying PIMCO detail [%d/%d] %s after attempt %d/%d failed: %s",
                index,
                total,
                cusip or detail_url,
                attempt,
                DETAIL_PAGE_MAX_ATTEMPTS,
                exc,
            )
            await asyncio.sleep(DETAIL_PAGE_RETRY_WAIT_MS / 1000)
    else:
        raise RuntimeError(
            f"PIMCO detail processing failed for {cusip or detail_url}: {last_error}"
        )

    listing_row = build_listing_row(detail_url, bootstrap, payloads)
    logging.info("Completed PIMCO detail [%d/%d] %s", index, total, cusip or detail_url)
    return (
        {
            "detail_url": detail_url,
            "bootstrap_context": bootstrap,
            "api_payloads": payloads,
            "listing_row": listing_row,
        },
        listing_row,
    )


async def build_snapshot_async(now: datetime) -> dict[str, object]:
    logging.info("Fetching PIMCO ETF detail URLs from the official sitemap")
    detail_urls = fetch_detail_urls()
    logging.info("Discovered %d PIMCO ETF detail URLs", len(detail_urls))
    bootstrap_contexts = fetch_bootstrap_contexts(detail_urls)
    records: list[dict[str, Any]] = []
    listing_rows: list[dict[str, str]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()

        bootstrap_page = await context.new_page()
        try:
            await bootstrap_page.goto(detail_urls[0], wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
            await accept_gate_if_present(bootstrap_page)
        finally:
            await bootstrap_page.close()

        try:
            logging.info(
                "Processing %d PIMCO detail pages with concurrency=%d",
                len(bootstrap_contexts),
                MAX_CONCURRENT_PAGES,
            )
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
            results = await asyncio.gather(
                *(
                    process_detail_page(
                        context,
                        bootstrap,
                        index,
                        len(bootstrap_contexts),
                        semaphore,
                    )
                    for index, bootstrap in enumerate(bootstrap_contexts, start=1)
                )
            )
            for record, listing_row in results:
                records.append(record)
                listing_rows.append(listing_row)
        finally:
            await browser.close()

    source_total_share_classes = len(listing_rows)
    records, listing_rows, missing_target_isins = filter_target_results(records, listing_rows)
    logging.info(
        "Matched %d/%d target PIMCO ISINs",
        len(listing_rows),
        len(TARGET_ISINS),
    )
    unique_funds = {clean_text(row.get("etf_name")) for row in listing_rows if clean_text(row.get("etf_name"))}
    return {
        "source": {
            "provider": ISSUER,
            "page_url": PAGE_URL,
            "sitemap_url": SITEMAP_URL,
            "country": "gb",
            "language": "en",
        },
        "method": "Official PIMCO GB ETF sitemap + public fund-detail app responses filtered to target ISINs",
        "captured_at": now.isoformat(),
        "requested_target_isin_count": len(TARGET_ISINS),
        "matched_target_isin_count": len(listing_rows),
        "missing_target_isins": missing_target_isins,
        "total_funds": len(unique_funds),
        "total_share_classes": len(listing_rows),
        "source_total_share_classes": source_total_share_classes,
        "detail_urls": detail_urls,
        "records": records,
        "listing_rows": listing_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now = timestamp_now()
    snapshot = asyncio.run(build_snapshot_async(now))
    write_json(destination, snapshot)
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved: %s", destination)


async def download_pimco_file() -> Path:
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
