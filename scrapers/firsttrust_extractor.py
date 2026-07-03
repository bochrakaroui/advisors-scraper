"""Download First Trust UK ETF listing data from the official products page and fund-facts endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from scrapers.tls_compat import requests_get
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from tls_compat import requests_get


PAGE_URL = "https://www.ftglobalportfolios.com/uk/professional/Products/"
BASE_URL = "https://www.ftglobalportfolios.com"
ISSUER = "First Trust"
BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "firsttrust"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": PAGE_URL,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}
FUND_FACTS_URL_TEMPLATE = (
    "https://www.ftglobalportfolios.com/srp/api/part"
    "?id=9397&share_class_id={shareclass_id}&entity_id={entity_id}&version=live&route=3558&audience=235"
)
ISIN_PATTERN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")


def build_run_output_dir(base_dir: Path, run_date: str) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def timestamp_now() -> datetime:
    return datetime.now()


def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "firsttrust_etf_export.json"


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


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
        percentage = Decimal(cleaned)
    except InvalidOperation:
        return ""
    return format_decimal(percentage * Decimal("100"), places=2)


def normalize_aum_mn(raw_value: str) -> str:
    cleaned = clean_text(raw_value)
    if not cleaned:
        return ""

    compact = (
        cleaned.replace(",", "")
        .replace(" ", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
        .replace("Ł", "")
    )
    compact_lower = compact.lower()

    multiplier = Decimal("0.000001")
    if compact_lower.endswith("billion"):
        multiplier = Decimal("1000")
        compact_lower = compact_lower[:-7]
    elif compact_lower.endswith("bn"):
        multiplier = Decimal("1000")
        compact_lower = compact_lower[:-2]
    elif compact_lower.endswith("b"):
        multiplier = Decimal("1000")
        compact_lower = compact_lower[:-1]
    elif compact_lower.endswith("million"):
        multiplier = Decimal("1")
        compact_lower = compact_lower[:-7]
    elif compact_lower.endswith("mn"):
        multiplier = Decimal("1")
        compact_lower = compact_lower[:-2]
    elif compact_lower.endswith("m"):
        multiplier = Decimal("1")
        compact_lower = compact_lower[:-1]

    if "," in compact_lower and "." not in compact_lower:
        compact_lower = compact_lower.replace(",", ".")

    try:
        amount = Decimal(compact_lower)
    except InvalidOperation:
        return ""

    return format_decimal(amount * multiplier, places=2)


def load_listing_payload() -> dict[str, Any]:
    response = requests_get(PAGE_URL, headers=REQUEST_HEADERS, timeout=120)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    for script in soup.find_all("script"):
        script_text = script.string or script.get_text()
        script_text = script_text.strip()
        if not script_text.startswith("{") or '"funds": [' not in script_text:
            continue
        payload = json.loads(script_text)
        if isinstance(payload, dict) and isinstance(payload.get("funds"), list):
            return payload

    raise ValueError("Could not find the embedded First Trust products JSON on the official page.")


def is_etf_fund_row(fund: dict[str, Any]) -> bool:
    return "ETF" in clean_text(fund.get("displayName")).upper() or "ETF" in clean_text(fund.get("longName")).upper()


def iter_lse_shareclass_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for fund in payload.get("funds", []):
        if not isinstance(fund, dict) or not is_etf_fund_row(fund):
            continue

        fund_name = clean_text(fund.get("displayName") or fund.get("longName"))
        product_url = urljoin(BASE_URL, clean_text(fund.get("url")))
        shareclasses = fund.get("shareclasses") or []
        for shareclass in shareclasses:
            if not isinstance(shareclass, dict):
                continue

            lse_listing = next(
                (
                    listing
                    for listing in shareclass.get("listings") or []
                    if isinstance(listing, dict)
                    and "london stock exchange" in clean_text(listing.get("exchange")).lower()
                ),
                None,
            )
            if not lse_listing:
                continue

            isin = clean_text(shareclass.get("isin")).upper()
            if not ISIN_PATTERN.fullmatch(isin):
                continue

            rows.append(
                {
                    "etf_name": fund_name,
                    "issuer": ISSUER,
                    "isin": isin,
                    "ccy": clean_text(shareclass.get("currencyDisplayName") or shareclass.get("currency")).upper(),
                    "product_url": product_url,
                    "shareclass_id": clean_text(shareclass.get("id")),
                    "entity_id": clean_text(lse_listing.get("id")),
                    "ter_raw": "",
                    "ter_bps": "",
                    "aum_raw": "",
                    "aum_mn": "",
                }
            )
    return rows


def parse_fund_facts_table(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    facts: dict[str, str] = {}
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        key = clean_text(cells[0].get_text(" ", strip=True))
        value = clean_text(cells[1].get_text(" ", strip=True))
        if key:
            facts[key] = value
    return facts


def fetch_detail_metrics(row: dict[str, str]) -> dict[str, str]:
    endpoint = FUND_FACTS_URL_TEMPLATE.format(
        shareclass_id=row["shareclass_id"],
        entity_id=row["entity_id"],
    )
    response = requests_get(endpoint, headers=REQUEST_HEADERS, timeout=120)
    response.raise_for_status()
    facts = parse_fund_facts_table(response.text)

    ter_raw = clean_text(facts.get("Total Expense Ratio"))
    if ter_raw and not ter_raw.endswith("%"):
        ter_raw = f"{ter_raw}%"
    aum_raw = clean_text(facts.get("Total Fund AUM"))

    return {
        "ter_raw": ter_raw,
        "ter_bps": normalize_ter_bps(ter_raw),
        "aum_raw": aum_raw,
        "aum_mn": normalize_aum_mn(aum_raw),
    }


def enrich_rows(rows: list[dict[str, str]]) -> None:
    print(f"[1/2] Fetching AUM and TER from {len(rows):,} official First Trust fund-facts endpoints ...")
    for index, row in enumerate(rows, start=1):
        row.update(fetch_detail_metrics(row))
        if index % 10 == 0 or index == len(rows):
            print(f"      Processed {index:,}/{len(rows):,} First Trust share classes.")


def write_snapshot(destination: Path, rows: list[dict[str, str]]) -> None:
    payload = {
        "source_url": PAGE_URL,
        "method": "embedded products JSON + official fund-facts HTML endpoint",
        "captured_at": datetime.now().isoformat(),
        "rows": rows,
    }
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


async def download_firsttrust_file() -> Path:
    output_path = build_output_path(timestamp_now())
    payload = await asyncio.to_thread(load_listing_payload)
    rows = iter_lse_shareclass_rows(payload)

    print(f"Source page : {PAGE_URL}")
    print("Data method : embedded products JSON + official fund-facts HTML endpoint")
    print(f"[2/2] Found {len(rows):,} London-listed First Trust ETF share classes in the official products JSON.")

    await asyncio.to_thread(enrich_rows, rows)
    write_snapshot(output_path, rows)
    print(f"Raw file    : {output_path}")
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected First Trust raw snapshot in {path}: expected a list of rows.")
    return rows


def main() -> None:
    output_path = asyncio.run(download_firsttrust_file())
    print(f"Done! Open your file at: {output_path.resolve()}")


if __name__ == "__main__":
    main()
