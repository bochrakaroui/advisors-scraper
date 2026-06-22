"""Download HSBC ETF data from the official UK website into a provider-specific raw snapshot."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import sys
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import Page, sync_playwright


PAGE_URL = "https://www.assetmanagement.hsbc.co.uk/en/institutional-investor/funds?f=Yes"
FUNDS_API_URL = "https://www.assetmanagement.hsbc.co.uk/api/v1/nav/funds"
FACTSHEET_URL_TEMPLATE = "https://www.assetmanagement.hsbc.co.uk/api/v1/download/document/{isin}/gb/en/factsheet"
ISSUER = "HSBC Asset Management"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "hsbc" / "hsbc_downloads"
VENDOR_PYPDF_DIR = BASE_DIR / ".vendor_pypdf"

if str(VENDOR_PYPDF_DIR) not in sys.path and VENDOR_PYPDF_DIR.exists():
    sys.path.insert(0, str(VENDOR_PYPDF_DIR))

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def timestamp_now() -> datetime:
    return datetime.now()


def build_output_path(now: datetime) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"hsbc_etf_export_{now.strftime('%Y%m%d_%H%M%S')}.json"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -"} else cleaned


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_click(page: Page, selectors: list[str]) -> None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1_000):
                locator.click(timeout=5_000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def accept_hsbc_overlays(page: Page) -> None:
    safe_click(
        page,
        [
            "button:has-text('Accept all cookies')",
            "button:has-text('Accept All Cookies')",
            "text='Accept all cookies'",
        ],
    )
    safe_click(
        page,
        [
            "button:has-text('Accept')",
            "text='Accept'",
        ],
    )


def capture_initial_api_context(page: Page) -> tuple[str, dict[str, object], dict[str, object]]:
    captured: dict[str, object] = {}

    def on_request(request) -> None:
        if request.url == FUNDS_API_URL and request.method == "POST" and "payload" not in captured:
            captured["authorization"] = request.headers.get("authorization", "")
            captured["payload"] = json.loads(request.post_data or "{}")

    def on_response(response) -> None:
        if response.url == FUNDS_API_URL and response.request.method == "POST" and "response" not in captured:
            captured["response"] = response.json()

    page.on("request", on_request)
    page.on("response", on_response)

    logging.info("Official URL used: %s", PAGE_URL)
    page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=120_000)
    accept_hsbc_overlays(page)

    for _ in range(40):
        if {"authorization", "payload", "response"} <= captured.keys():
            break
        page.wait_for_timeout(500)

    missing = {"authorization", "payload", "response"} - captured.keys()
    if missing:
        raise RuntimeError(f"Failed to capture HSBC funds API context. Missing: {sorted(missing)}")

    authorization = clean_text(captured["authorization"])
    payload = captured["payload"]
    response = captured["response"]

    if not authorization:
        raise RuntimeError("Captured HSBC funds API request did not include an authorization header.")

    return authorization, payload, response


def fetch_funds_page(page: Page, authorization: str, payload: dict[str, object]) -> dict[str, object]:
    return page.evaluate(
        """async ({url, authorization, payload}) => {
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "Authorization": authorization,
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(payload)
            });
            return await response.json();
        }""",
        {
            "url": FUNDS_API_URL,
            "authorization": authorization,
            "payload": payload,
        },
    )


def extract_share_class_link(share_class: dict[str, object]) -> str:
    for item in share_class.get("data", []):
        if item.get("columnId") == "UniqueIdentifier":
            return clean_text(item.get("link"))
    return ""


def extract_currency(share_class: dict[str, object]) -> str:
    for item in share_class.get("data", []):
        if item.get("groupType") != "currency":
            continue

        selected = clean_text(item.get("selected"))
        for group in item.get("groups") or []:
            for option in group.get("options") or []:
                if clean_text(option.get("id")) == selected:
                    return clean_text(option.get("value")).upper()

        for group in item.get("groups") or []:
            options = group.get("options") or []
            if options:
                return clean_text(options[0].get("value")).upper()

    return ""


def extract_listing_rows(page_information: dict[str, object], api_pages: list[dict[str, object]]) -> list[dict[str, str]]:
    detail_base = clean_text(page_information.get("fundDetailPageUrl")) or "/en/institutional-investor/funds"
    rows: list[dict[str, str]] = []

    for api_page in api_pages:
        for fund in api_page.get("funds", []):
            fund_id = clean_text(fund.get("id"))
            fund_name = clean_text(fund.get("name"))
            for share_class in fund.get("shareClasses", []):
                isin = clean_text(share_class.get("isin")).upper()
                link = extract_share_class_link(share_class)
                detail_url = urljoin("https://www.assetmanagement.hsbc.co.uk", detail_base.rstrip("/") + link)
                rows.append(
                    {
                        "fund_id": fund_id,
                        "etf_name": fund_name,
                        "issuer": ISSUER,
                        "isin": isin,
                        "ccy": extract_currency(share_class),
                        "detail_url": detail_url,
                    }
                )

    return rows


def parse_aum_to_millions(raw_value: str) -> str:
    cleaned = clean_text(raw_value)
    if not cleaned:
        return ""

    cleaned = re.sub(r"\b[A-Z]{3}\b", "", cleaned).strip()
    cleaned = cleaned.lower().replace(" ", "")

    multiplier = Decimal("1")
    if cleaned.endswith("bn"):
        multiplier = Decimal("1000")
        cleaned = cleaned[:-2]
    elif cleaned.endswith("billion"):
        multiplier = Decimal("1000")
        cleaned = cleaned[:-7]
    elif cleaned.endswith("m"):
        cleaned = cleaned[:-1]
    elif cleaned.endswith("million"):
        cleaned = cleaned[:-7]
    else:
        multiplier = Decimal("0.000001")

    cleaned = cleaned.replace(",", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return ""

    return format_decimal(amount * multiplier, places=2)


def parse_percentage_to_bps(raw_value: str) -> str:
    cleaned = clean_text(raw_value).replace("%", "").strip()
    if not cleaned:
        return ""
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        percentage = Decimal(cleaned)
    except InvalidOperation:
        return ""
    return format_decimal(percentage * Decimal("100"), places=2)


def open_prices_and_fees_tab(page: Page) -> None:
    for selector in (
        "text='Prices and fees'",
        "h2:has-text('Prices and fees')",
        "[role='tab']:has-text('Prices and fees')",
    ):
        try:
            page.locator(selector).first.click(timeout=5_000, force=True)
            page.wait_for_timeout(2_000)
            return
        except Exception:
            continue


def capture_detail_page_fields(page: Page, detail_url: str) -> dict[str, str]:
    payloads: list[dict[str, object]] = []

    def on_response(response) -> None:
        if "/api/v1/detail/list" not in response.url:
            return
        try:
            payloads.append(response.json())
        except Exception:
            return

    page.on("response", on_response)
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=120_000)
        accept_hsbc_overlays(page)
        page.wait_for_timeout(2_500)
        open_prices_and_fees_tab(page)
    finally:
        page.remove_listener("response", on_response)

    fields = {"aum_raw": "", "ter_bps": ""}
    for payload in payloads:
        if clean_text(payload.get("title")) == "Fees":
            for item in payload.get("items", []):
                if clean_text(item.get("title")).startswith("Ongoing charge figure") and not fields["ter_bps"]:
                    fields["ter_bps"] = parse_percentage_to_bps(clean_text(item.get("value")))
        for item in payload.get("items", []):
            if clean_text(item.get("title")) == "Fund AUM" and not fields["aum_raw"]:
                fields["aum_raw"] = clean_text(item.get("value"))

    return fields


@lru_cache(maxsize=None)
def extract_factsheet_fields(isin: str) -> dict[str, str]:
    if PdfReader is None:
        return {"ccy": "", "ter_bps": "", "aum_raw": ""}

    response = requests.get(
        FACTSHEET_URL_TEMPLATE.format(isin=isin.lower()),
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=120,
    )
    if response.status_code != 200 or "pdf" not in (response.headers.get("content-type") or "").lower():
        return {"ccy": "", "ter_bps": "", "aum_raw": ""}

    reader = PdfReader(io.BytesIO(response.content))
    text = "\n".join(pdf_page.extract_text() or "" for pdf_page in reader.pages)
    aum_match = re.search(
        r"Fund size\s+[A-Z]{3}\s+([0-9][0-9,]*(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    )
    ccy_match = re.search(r"Share class base currency\s+([A-Z]{3})\b", text, flags=re.IGNORECASE)
    ter_match = re.search(
        r"Ongoing charge figure[^\d]{0,12}([0-9]+(?:[.,][0-9]+)?)%",
        text,
        flags=re.IGNORECASE,
    )
    return {
        "ccy": clean_text(ccy_match.group(1)).upper() if ccy_match else "",
        "ter_bps": parse_percentage_to_bps(ter_match.group(1)) if ter_match else "",
        "aum_raw": clean_text(aum_match.group(1)) if aum_match else "",
    }


def collect_aum_map(page: Page, listing_rows: list[dict[str, str]]) -> tuple[dict[str, str], list[dict[str, str]]]:
    aum_by_fund_id: dict[str, str] = {}
    raw_entries: list[dict[str, str]] = []

    detail_targets: dict[str, list[tuple[str, str, str]]] = {}
    for row in listing_rows:
        detail_targets.setdefault(row["fund_id"], []).append((row["detail_url"], row["etf_name"], row["isin"]))

    for fund_id, targets in detail_targets.items():
        etf_name = targets[0][1]
        raw_aum_value = ""
        source = ""
        source_url = ""

        for detail_url, _, share_class_isin in targets:
            raw_aum_value = capture_detail_page_fields(page, detail_url).get("aum_raw", "")
            if raw_aum_value:
                source = "detail_api"
                source_url = detail_url
                break

        if not raw_aum_value:
            for _, _, share_class_isin in targets:
                raw_aum_value = extract_factsheet_fields(share_class_isin).get("aum_raw", "")
                if raw_aum_value:
                    source = "factsheet_pdf"
                    source_url = FACTSHEET_URL_TEMPLATE.format(isin=share_class_isin.lower())
                    break

        aum_mn = parse_aum_to_millions(raw_aum_value)
        aum_by_fund_id[fund_id] = aum_mn
        raw_entries.append(
            {
                "fund_id": fund_id,
                "etf_name": etf_name,
                "share_class_isins": [share_class_isin for _, _, share_class_isin in targets],
                "source": source,
                "source_url": source_url,
                "raw_aum_value": raw_aum_value,
                "aum_mn": aum_mn,
            }
        )

    return aum_by_fund_id, raw_entries


def collect_detail_ter_map(page: Page, listing_rows: list[dict[str, str]]) -> dict[str, str]:
    ter_by_isin: dict[str, str] = {}

    for row in listing_rows:
        isin = row["isin"]
        if extract_factsheet_fields(isin).get("ter_bps"):
            continue
        ter_bps = capture_detail_page_fields(page, row["detail_url"]).get("ter_bps", "")
        if ter_bps:
            ter_by_isin[isin] = ter_bps

    return ter_by_isin


def build_snapshot(now: datetime) -> dict[str, object]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        list_page = browser.new_page()

        authorization, initial_payload, initial_response = capture_initial_api_context(list_page)
        total_pages = int(initial_response.get("paging", {}).get("totalPages", 1))
        page_information = initial_payload.get("pageInformation", {})

        api_pages = [initial_response]
        for page_number in range(2, total_pages + 1):
            page_payload = deepcopy(initial_payload)
            page_payload["paging"]["currentPage"] = page_number
            api_pages.append(fetch_funds_page(list_page, authorization, page_payload))

        detail_page = browser.new_page()
        listing_rows = extract_listing_rows(page_information, api_pages)
        aum_by_fund_id, raw_aum_entries = collect_aum_map(detail_page, listing_rows)
        detail_ter_by_isin = collect_detail_ter_map(detail_page, listing_rows)
        browser.close()

    enriched_rows = []
    for row in listing_rows:
        factsheet_fields = extract_factsheet_fields(row["isin"])
        enriched_rows.append(
            {
                "fund_id": row["fund_id"],
                "etf_name": row["etf_name"],
                "issuer": row["issuer"],
                "isin": row["isin"],
                "ccy": row["ccy"] or factsheet_fields.get("ccy", ""),
                "ter_bps": factsheet_fields.get("ter_bps", "") or detail_ter_by_isin.get(row["isin"], ""),
                "aum_mn": aum_by_fund_id.get(row["fund_id"], ""),
            }
        )

    return {
        "source_url": PAGE_URL,
        "method": "official API + detail API/product factsheets",
        "captured_at": now.isoformat(),
        "pages": api_pages,
        "aum_entries": raw_aum_entries,
        "listing_rows": enriched_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now = timestamp_now()
    snapshot = build_snapshot(now)
    write_json(destination, snapshot)
    logging.info("Data method used: %s", snapshot["method"])
    logging.info("Raw snapshot saved: %s", destination)


async def download_hsbc_file() -> Path:
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
