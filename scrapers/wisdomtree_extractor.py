"""Download WisdomTree Europe UCITS ETF data from the official product-list PDF."""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import requests


PAGE_URL = "https://www.wisdomtree.eu/en-gb/products"
PRODUCT_LIST_PDF_URL = (
    "https://www.wisdomtree.eu/-/media/eu-media-files/other-documents/product-list/etf-product-list.pdf?sc_lang=en-gb"
)
ISSUER = "WisdomTree"
PROVIDER = "WisdomTree"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "wisdomtree" / "wisdomtree_downloads"
VENDOR_PYPDF_DIR = BASE_DIR / ".vendor_pypdf"

if str(VENDOR_PYPDF_DIR) not in sys.path and VENDOR_PYPDF_DIR.exists():
    sys.path.insert(0, str(VENDOR_PYPDF_DIR))

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


LISTING_PATTERN = re.compile(
    r"(?P<prefix>.*?)(?P<exchange>UK|IT|CH|DE|FR & NL)\s+"
    r"(?P<exchange_code>\S+)\s+"
    r"(?P<bloomberg_code>.+?)\s+"
    r"(?P<isin>[A-Z]{2}[A-Z0-9]{10})\s+"
    r"(?P<trading_currency>[A-Z]{3})\s+"
    r"(?P<base_currency>[A-Z]{3})\s+"
    r"(?P<ter>\d+(?:\.\d+)?)\s*$"
)
SECTION_PATTERN = re.compile(r"^[A-Z][A-Za-z&\- ]+\s+Exchange$")
ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT = 60

HEADER_LINES = {
    "Exchange",
    "Code",
    "Code ISIN",
    "Bloomberg",
    "Trading",
    "Currency",
    "Base",
    "Currency TER/MER % Issuer",
    "UCITS ETFs AND UNLEVERAGED ETPs",
    "SHORT AND LEVERAGED ETPs",
    "Contents",
}
MARKER_LINES = {"WT", "WIXL", "FXL", "WTMA"}
SECTION_TITLES = {
    "EQUITIES",
    "COMMODITIES",
    "CURRENCIES",
    "FIXED INCOME",
    "DIGITAL ASSETS",
    "AGRICULTURE",
}


def build_output_path(now: datetime) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"wisdomtree_etf_export_{now.strftime('%Y%m%d_%H%M%S')}.json"


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
        ter_pct = Decimal(cleaned)
    except InvalidOperation:
        return ""

    return format_decimal(ter_pct * Decimal("100"), places=2)


def is_valid_isin(value: str) -> bool:
    return bool(ISIN_PATTERN.fullmatch(clean_text(value).upper()))


def fetch_product_list_pdf() -> bytes:
    response = requests.get(PRODUCT_LIST_PDF_URL, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").lower()
    if "pdf" not in content_type:
        raise ValueError(f"Unexpected WisdomTree product list content type: {content_type or 'unknown'}")

    return response.content


def iter_pdf_lines(pdf_bytes: bytes) -> list[tuple[int, str]]:
    if PdfReader is None:
        raise RuntimeError("pypdf is not available. The WisdomTree product-list PDF cannot be parsed.")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    lines: list[tuple[int, str]] = []

    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        for line in page_text.splitlines():
            cleaned = " ".join(line.replace("\u00a0", " ").split())
            if cleaned:
                lines.append((page_number, cleaned))

    return lines


def is_header_or_section(line: str) -> bool:
    if line in HEADER_LINES or line in MARKER_LINES:
        return True
    if line in SECTION_TITLES:
        return True
    if re.fullmatch(r"\d+", line):
        return True
    if SECTION_PATTERN.fullmatch(line):
        return True
    return False


def build_product_rows(pdf_bytes: bytes, scraped_at: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    started = False
    current_name = ""

    for page_number, line in iter_pdf_lines(pdf_bytes):
        if line == "Currency TER/MER % Issuer":
            started = True
            current_name = ""
            continue

        if not started:
            continue

        if is_header_or_section(line):
            current_name = ""
            continue

        match = LISTING_PATTERN.search(line)
        if match:
            prefix = clean_text(match.group("prefix")).strip(" -")
            if prefix:
                wisdomtree_idx = prefix.find("WisdomTree")
                if wisdomtree_idx >= 0:
                    current_name = clean_text(prefix[wisdomtree_idx:])
                elif current_name:
                    current_name = clean_text(f"{current_name} {prefix}")
                else:
                    current_name = prefix

            etf_name = " ".join(current_name.split())
            isin = clean_text(match.group("isin")).upper()

            if "UCITS ETF" not in etf_name or not is_valid_isin(isin):
                continue

            rows.append(
                {
                    "provider": PROVIDER,
                    "issuer": ISSUER,
                    "etf_name": etf_name,
                    "isin": isin,
                    "ccy": clean_text(match.group("base_currency")).upper(),
                    "aum_raw": "",
                    "aum_numeric": "",
                    "aum_currency": "",
                    "ter_raw": clean_text(match.group("ter")),
                    "ter_bps": normalize_ter_bps(match.group("ter")),
                    "product_url": "",
                    "scraped_at": scraped_at,
                    "source_page": str(page_number),
                }
            )
            continue

        wisdomtree_idx = line.find("WisdomTree")
        if wisdomtree_idx >= 0:
            current_name = clean_text(line[wisdomtree_idx:])
        elif current_name:
            current_name = clean_text(f"{current_name} {line}")
        else:
            current_name = clean_text(line)

    deduped_rows: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["isin"], row["product_url"])
        deduped_rows.setdefault(key, row)

    return list(deduped_rows.values())


def build_snapshot(now: datetime) -> dict[str, object]:
    pdf_bytes = fetch_product_list_pdf()
    scraped_at = now.isoformat()
    rows = build_product_rows(pdf_bytes, scraped_at=scraped_at)
    valid_isin_count = sum(1 for row in rows if is_valid_isin(row["isin"]))
    missing_ccy_count = sum(1 for row in rows if not row["ccy"])
    missing_aum_count = sum(1 for row in rows if not row["aum_numeric"])

    print(f"Official URL used: {PAGE_URL}")
    print("Data method used: official product-list PDF linked from the WisdomTree products page")
    print(f"Rows extracted: {len(rows):,}")
    print(f"Valid ISINs: {valid_isin_count:,}")
    print(f"Missing CCY values: {missing_ccy_count:,}")
    print(f"Missing AUM values: {missing_aum_count:,}")

    return {
        "source_url": PAGE_URL,
        "source_document_url": PRODUCT_LIST_PDF_URL,
        "method": "official product-list PDF",
        "captured_at": scraped_at,
        "limitations": [
            "Only rows whose official product names contain 'UCITS ETF' are kept.",
            "The downloaded official product-list PDF exposes ISIN, base currency, and TER.",
            "Fund-level AUM and product detail URLs are not exposed in the downloaded PDF, so they remain blank in this snapshot.",
        ],
        "rows": rows,
    }


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def download_snapshot(destination: Path) -> None:
    snapshot = build_snapshot(datetime.now())
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
