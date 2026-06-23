"""Download HSBC ETF data from the official UK website into a provider-specific raw snapshot."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from playwright.sync_api import Page, sync_playwright


PAGE_URL = "https://www.assetmanagement.hsbc.co.uk/en/institutional-investor/funds?f=Yes"
FUNDS_API_URL = "https://www.assetmanagement.hsbc.co.uk/api/v1/nav/funds"
FACTSHEET_URL_TEMPLATE = "https://www.assetmanagement.hsbc.co.uk/api/v1/download/document/{isin}/gb/en/factsheet"
ISSUER = "HSBC Asset Management"
FACTSHEET_TIMEOUT = (15, 45)
MAX_FACTSHEET_WORKERS = 8

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "hsbc"
VENDOR_PYPDF_DIR = BASE_DIR / ".vendor_pypdf"
REFERENCE_ISIN_PATH = BASE_DIR / "ISIN-list.xlsx"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

if str(VENDOR_PYPDF_DIR) not in sys.path and VENDOR_PYPDF_DIR.exists():
    sys.path.insert(0, str(VENDOR_PYPDF_DIR))

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


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


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now()


def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "hsbc_etf_export.json"


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


def normalize_header(value: object | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def normalize_isin(value: object | None) -> str:
    cleaned = clean_text(value).upper().replace("\u00a0", "")
    return re.sub(r"\s+", "", cleaned)


def column_index_from_ref(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref or "")
    if not match:
        return -1

    index = 0
    for character in match.group(1):
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


def get_xlsx_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    value = cell.find("main:v", XLSX_NS)
    inline_text = cell.find("main:is", XLSX_NS)
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr" and inline_text is not None:
        return "".join(node.text or "" for node in inline_text.iterfind(".//main:t", XLSX_NS)).strip()

    if value is None or value.text is None:
        return ""

    raw_value = value.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)].strip()
        except (ValueError, IndexError):
            return ""
    return raw_value


def load_reference_provider_isins(provider_name: str) -> set[str]:
    if not REFERENCE_ISIN_PATH.exists():
        return set()

    provider_isins: set[str] = set()
    with zipfile.ZipFile(REFERENCE_ISIN_PATH) as workbook_zip:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in workbook_zip.namelist():
            shared_strings_root = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
            for item in shared_strings_root.findall("main:si", XLSX_NS):
                shared_strings.append(
                    "".join(node.text or "" for node in item.iterfind(".//main:t", XLSX_NS)).strip()
                )

        workbook_root = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
        relationships_root = ET.fromstring(workbook_zip.read("xl/_rels/workbook.xml.rels"))
        relationship_map = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships_root.findall("rel:Relationship", XLSX_NS)
        }

        for sheet in workbook_root.findall("main:sheets/main:sheet", XLSX_NS):
            relationship_id = sheet.attrib.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                "",
            )
            target = relationship_map.get(relationship_id)
            if not target:
                continue

            sheet_root = ET.fromstring(workbook_zip.read(f"xl/{target}"))
            sheet_rows: list[dict[int, str]] = []
            for row in sheet_root.findall("main:sheetData/main:row", XLSX_NS):
                row_values: dict[int, str] = {}
                for cell in row.findall("main:c", XLSX_NS):
                    column_index = column_index_from_ref(cell.attrib.get("r", ""))
                    if column_index >= 0:
                        row_values[column_index] = get_xlsx_cell_text(cell, shared_strings)
                sheet_rows.append(row_values)

            if not sheet_rows:
                continue

            headers = {index: normalize_header(value) for index, value in sheet_rows[0].items()}
            provider_column = next((index for index, header in headers.items() if header == "provider"), None)
            isin_column = next((index for index, header in headers.items() if header in {"isin", "isincode"}), None)
            if provider_column is None or isin_column is None:
                continue

            for row_values in sheet_rows[1:]:
                if clean_text(row_values.get(provider_column)).lower() != provider_name.lower():
                    continue
                isin = normalize_isin(row_values.get(isin_column))
                if isin:
                    provider_isins.add(isin)

    return provider_isins


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


def extract_listing_rows(api_pages: list[dict[str, object]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    for api_page in api_pages:
        for fund in api_page.get("funds", []):
            fund_id = clean_text(fund.get("id"))
            fund_name = clean_text(fund.get("name"))
            for share_class in fund.get("shareClasses", []):
                isin = clean_text(share_class.get("isin")).upper()
                rows.append(
                    {
                        "fund_id": fund_id,
                        "etf_name": fund_name,
                        "issuer": ISSUER,
                        "isin": isin,
                        "ccy": extract_currency(share_class),
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


def extract_fund_name_from_factsheet(text: str) -> str:
    lines = [clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    title_lines: list[str] = []
    for line in lines[:12]:
        if line.lower().startswith("marketing communication"):
            break
        if line.upper() in {"HSBC GLOBAL FUNDS ICAV", "HSBC ETFS PLC", "HSBC GLOBAL LIQUIDITY FUNDS PLC"} and not title_lines:
            continue
        title_lines.append(line)

    title = " ".join(title_lines)
    title = re.sub(r"\s+", " ", title).strip()
    if "UCITS ETF" in title.upper():
        return title if title.upper().startswith("HSBC") else f"HSBC {title}"
    if title and any("UCITS ETF" in line.upper() for line in lines[:6]):
        return title if title.upper().startswith("HSBC") else f"HSBC {title}"
    return ""


@lru_cache(maxsize=None)
def extract_factsheet_fields(isin: str) -> dict[str, str]:
    if PdfReader is None:
        return {"ccy": "", "ter_bps": "", "aum_raw": ""}
    try:
        response = requests.get(
            FACTSHEET_URL_TEMPLATE.format(isin=isin.lower()),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=FACTSHEET_TIMEOUT,
        )
    except requests.RequestException:
        return {"ccy": "", "ter_bps": "", "aum_raw": ""}

    if response.status_code != 200 or "pdf" not in (response.headers.get("content-type") or "").lower():
        return {"ccy": "", "ter_bps": "", "aum_raw": ""}
    try:
        reader = PdfReader(io.BytesIO(response.content))
        text = "\n".join(pdf_page.extract_text() or "" for pdf_page in reader.pages)
    except Exception:
        return {"etf_name": "", "ccy": "", "ter_bps": "", "aum_raw": ""}
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
        "etf_name": extract_fund_name_from_factsheet(text),
        "ccy": clean_text(ccy_match.group(1)).upper() if ccy_match else "",
        "ter_bps": parse_percentage_to_bps(ter_match.group(1)) if ter_match else "",
        "aum_raw": clean_text(aum_match.group(1)) if aum_match else "",
    }


def fetch_factsheet_map(isins: list[str], *, label: str) -> dict[str, dict[str, str]]:
    unique_isins = [isin for isin in dict.fromkeys(isin for isin in isins if isin)]
    if not unique_isins:
        return {}

    total = len(unique_isins)
    logging.info("Fetching official HSBC factsheets for %s (%s documents) ...", label, total)
    results: dict[str, dict[str, str]] = {}

    with ThreadPoolExecutor(max_workers=min(MAX_FACTSHEET_WORKERS, total)) as executor:
        future_map = {executor.submit(extract_factsheet_fields, isin): isin for isin in unique_isins}
        completed = 0

        for future in as_completed(future_map):
            isin = future_map[future]
            try:
                results[isin] = future.result()
            except Exception:
                results[isin] = {"ccy": "", "ter_bps": "", "aum_raw": ""}

            completed += 1
            if completed == total or completed % 25 == 0:
                logging.info("Factsheets %s: %s/%s complete", label, completed, total)

    return results


def build_aum_map(
    listing_rows: list[dict[str, str]],
    factsheet_by_isin: dict[str, dict[str, str]],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    aum_by_fund_id: dict[str, str] = {}
    raw_entries: list[dict[str, str]] = []

    targets_by_fund_id: dict[str, list[dict[str, str]]] = {}
    for row in listing_rows:
        targets_by_fund_id.setdefault(row["fund_id"], []).append(row)

    for fund_id, targets in targets_by_fund_id.items():
        representative = next((row for row in targets if row["isin"]), targets[0])
        raw_aum_value = ""
        source_isin = ""

        for candidate in targets:
            candidate_isin = candidate["isin"]
            factsheet_fields = factsheet_by_isin.get(candidate_isin, {})
            raw_aum_value = clean_text(factsheet_fields.get("aum_raw"))
            if raw_aum_value:
                representative = candidate
                source_isin = candidate_isin
                break

        if not source_isin:
            source_isin = representative["isin"]

        aum_mn = parse_aum_to_millions(raw_aum_value)
        aum_by_fund_id[fund_id] = aum_mn
        raw_entries.append(
            {
                "fund_id": fund_id,
                "etf_name": representative["etf_name"],
                "share_class_isins": [row["isin"] for row in targets if row["isin"]],
                "source": "factsheet_pdf" if raw_aum_value else "",
                "source_url": FACTSHEET_URL_TEMPLATE.format(isin=source_isin.lower()) if source_isin else "",
                "raw_aum_value": raw_aum_value,
                "aum_mn": aum_mn,
            }
        )

    return aum_by_fund_id, raw_entries


def build_reference_factsheet_rows(
    listing_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    reference_isins = load_reference_provider_isins("HSBC")
    listed_isins = {normalize_isin(row["isin"]) for row in listing_rows if row["isin"]}
    missing_reference_isins = sorted(reference_isins - listed_isins)
    if not missing_reference_isins:
        return []

    factsheet_by_isin = fetch_factsheet_map(missing_reference_isins, label="reference HSBC gaps")
    rows: list[dict[str, str]] = []
    for isin in missing_reference_isins:
        factsheet_fields = factsheet_by_isin.get(isin, {})
        etf_name = clean_text(factsheet_fields.get("etf_name"))
        ccy = clean_text(factsheet_fields.get("ccy")).upper()
        aum_raw = clean_text(factsheet_fields.get("aum_raw"))

        # Only add rows that are confirmed by a usable official HSBC factsheet.
        if not etf_name or not ccy or not aum_raw:
            continue

        rows.append(
            {
                "fund_id": f"factsheet:{isin}",
                "etf_name": etf_name,
                "issuer": ISSUER,
                "isin": isin,
                "ccy": ccy,
                "ter_bps": clean_text(factsheet_fields.get("ter_bps")),
                "aum_mn": parse_aum_to_millions(aum_raw),
            }
        )

    logging.info(
        "Added %s HSBC reference ISIN rows from official factsheets; %s reference ISINs were not in the listing API.",
        len(rows),
        len(missing_reference_isins),
    )
    return rows


def build_snapshot(now: datetime) -> dict[str, object]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        list_page = browser.new_page()

        authorization, initial_payload, initial_response = capture_initial_api_context(list_page)
        total_pages = int(initial_response.get("paging", {}).get("totalPages", 1))

        api_pages = [initial_response]
        for page_number in range(2, total_pages + 1):
            page_payload = deepcopy(initial_payload)
            page_payload["paging"]["currentPage"] = page_number
            api_pages.append(fetch_funds_page(list_page, authorization, page_payload))

        listing_rows = extract_listing_rows(api_pages)
        browser.close()

    logging.info("Captured %s HSBC ETF listing rows from the official listing API.", len(listing_rows))
    factsheet_by_isin = fetch_factsheet_map([row["isin"] for row in listing_rows], label="share classes")
    aum_by_fund_id, raw_aum_entries = build_aum_map(listing_rows, factsheet_by_isin)

    enriched_rows = []
    for row in listing_rows:
        factsheet_fields = factsheet_by_isin.get(row["isin"], {})
        enriched_rows.append(
            {
                "fund_id": row["fund_id"],
                "etf_name": row["etf_name"],
                "issuer": row["issuer"],
                "isin": row["isin"],
                "ccy": row["ccy"] or factsheet_fields.get("ccy", ""),
                "ter_bps": factsheet_fields.get("ter_bps", ""),
                "aum_mn": aum_by_fund_id.get(row["fund_id"], ""),
            }
        )

    enriched_rows.extend(build_reference_factsheet_rows(enriched_rows))

    return {
        "source_url": PAGE_URL,
        "method": "official listing API + official factsheet PDFs",
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
