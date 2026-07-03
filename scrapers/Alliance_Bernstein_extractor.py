"""Scrape official AllianceBernstein UCITS ETFs and export selected fields CSV."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


ISSUER = "AllianceBernstein"
BASE_URL = "https://www.alliancebernstein.com"
SITEMAP_URL = f"{BASE_URL}/gb/en-gb/investor.sitemap.xml"
API_BASE_URL = "https://webapi.alliancebernstein.com"
REGION = "gb"
LANGUAGE = "en-gb"
SITE_SEGMENT = "investor"
REQUEST_TIMEOUT_S = 60
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Alliance_Bernstein"
RAW_FILENAME = "alliance_bernstein_etf_export.json"

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "Date",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SPACE_RE = re.compile(r"\s+")
ISIN_RE = re.compile(r"^[A-Z0-9]{12}$")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now(UTC)


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
    return (
        build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d"))
        / RAW_FILENAME
    )


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_RE.sub(" ", cleaned)
    return "" if cleaned.lower() in {"", "-", "--", "none", "null", "n/a", "nan"} else cleaned


def normalize_isin(value: object | None) -> str:
    cleaned = clean_text(value).upper().replace(" ", "")
    return cleaned if ISIN_RE.fullmatch(cleaned) else ""


def normalize_ccy(value: object | None) -> str:
    cleaned = clean_text(value).upper()
    return cleaned if re.fullmatch(r"[A-Z]{3}", cleaned) else ""


def parse_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None

    normalized = re.sub(r"[^0-9,.\-+]", "", cleaned)
    if not normalized:
        return None

    if "," in normalized and "." in normalized:
        if normalized.rfind(".") > normalized.rfind(","):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized and "." not in normalized:
        parts = normalized.split(",")
        if len(parts) > 2 or all(len(part) == 3 for part in parts[1:]):
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


def decimal_to_string(value: Decimal, places: int = 2) -> str:
    rendered = format_decimal(value, places)
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def percent_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return decimal_to_string(decimal_value * Decimal("100"), places=2)


def parse_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    for candidate in [cleaned, cleaned.replace("Z", "+00:00")]:
        try:
            return datetime.fromisoformat(candidate).strftime("%d/%m/%Y")
        except ValueError:
            continue

    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


def write_json(output_path: Path, payload: object) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.text


def fetch_json(session: requests.Session, url: str) -> Any:
    response = session.get(url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, str):
        data = json.loads(data)
    return data


def discover_etf_detail_urls(session: requests.Session) -> list[str]:
    xml = fetch_text(session, SITEMAP_URL)
    urls = re.findall(r"<loc>(.*?)</loc>", xml)
    detail_urls = sorted(
        {
            clean_text(url)
            for url in urls
            if ".ucits-etf-" in clean_text(url).lower()
        }
    )
    return detail_urls


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.find("title")
    if title_node is None:
        return ""
    title = clean_text(title_node.get_text(" ", strip=True))
    return re.sub(r"\s+\|\s+AB$", "", title)


def extract_isin_from_detail_url(detail_url: str) -> str:
    match = re.search(r"\.([A-Z0-9]{12})\.html$", detail_url, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def normalize_etf_name(fund_name: str, share_class_name: str, detail_url: str, page_title: str) -> str:
    cleaned_fund_name = clean_text(fund_name)
    cleaned_share_class_name = clean_text(share_class_name)
    cleaned_page_title = clean_text(page_title)

    if cleaned_fund_name.endswith(" Portfolio"):
        cleaned_fund_name = cleaned_fund_name[: -len(" Portfolio")]
    if cleaned_page_title.endswith(" Portfolio"):
        cleaned_page_title = cleaned_page_title[: -len(" Portfolio")]

    if cleaned_share_class_name.upper().startswith("UCITS ETF"):
        base_name = cleaned_fund_name or cleaned_page_title
        return clean_text(f"{base_name} {cleaned_share_class_name}")

    slug_match = re.search(r"/([^/]+)\.([A-Z0-9]{12})\.html$", detail_url, re.IGNORECASE)
    if slug_match:
        slug_name = slug_match.group(1)
        slug_name = slug_name.replace(".", " ")
        slug_name = re.sub(r"-", " ", slug_name)
        words = []
        for token in slug_name.split():
            upper_token = token.upper()
            if upper_token in {"AB", "UCITS", "ETF", "USD", "EUR", "GBP", "ACC", "DIST"}:
                words.append(upper_token if upper_token != "ACC" else "Acc")
            else:
                words.append(token.capitalize())
        return clean_text(" ".join(words))

    return cleaned_fund_name or cleaned_page_title


def fetch_shareclass_payloads(session: requests.Session, isin: str) -> dict[str, Any]:
    api_root = f"{API_BASE_URL}/v1/shareclasses/{REGION}/{LANGUAGE}/{SITE_SEGMENT}/{isin}"
    payloads = {
        "expenses": fetch_json(session, f"{api_root}/expenses?include=ShareClass"),
        "prices": fetch_json(session, f"{api_root}/prices?latestOnly=true&include=ShareClass"),
        "netassets": fetch_json(session, f"{api_root}/netassets?include=ShareClass"),
        "scopes": fetch_json(session, f"{api_root}/scopes"),
        "pvdd_info": fetch_json(
            session,
            f"{API_BASE_URL}/v2/funds/{REGION}/{LANGUAGE}/{SITE_SEGMENT}/{isin}/pvdd-info",
        ),
    }
    return payloads


def first_item(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        return first if isinstance(first, dict) else {}
    return data if isinstance(data, dict) else {}


def first_included_shareclass(payload: dict[str, Any]) -> dict[str, Any]:
    included = payload.get("included")
    if isinstance(included, list):
        for item in included:
            if isinstance(item, dict) and item.get("type") == "shareClasses":
                return item
    return {}


def matching_scope(payload: dict[str, Any], isin: str) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes", {})
            if normalize_isin((attributes or {}).get("isin")) == isin:
                return item
    return {}


def build_row(
    detail_url: str,
    page_title: str,
    payloads: dict[str, Any],
    today: datetime,
) -> dict[str, str]:
    isin = extract_isin_from_detail_url(detail_url)

    expenses_item = first_item(payloads["expenses"])
    prices_item = first_item(payloads["prices"])
    netassets_item = first_item(payloads["netassets"])
    included_shareclass = first_included_shareclass(payloads["expenses"])
    scope_item = matching_scope(payloads["scopes"], isin)

    included_attrs = included_shareclass.get("attributes", {}) if isinstance(included_shareclass, dict) else {}
    scope_attrs = scope_item.get("attributes", {}) if isinstance(scope_item, dict) else {}
    expense_attrs = expenses_item.get("attributes", {}) if isinstance(expenses_item, dict) else {}
    price_attrs = prices_item.get("attributes", {}) if isinstance(prices_item, dict) else {}
    netasset_attrs = netassets_item.get("attributes", {}) if isinstance(netassets_item, dict) else {}
    pvdd_info = payloads.get("pvdd_info", {}) if isinstance(payloads.get("pvdd_info"), dict) else {}

    fund_name = clean_text(scope_attrs.get("fundName") or included_attrs.get("fundName"))
    share_class_name = clean_text(scope_attrs.get("name") or included_attrs.get("name"))
    etf_name = normalize_etf_name(fund_name, share_class_name, detail_url, page_title)
    ccy = normalize_ccy(scope_attrs.get("currency") or included_attrs.get("currency"))
    ter_bps = percent_to_bps(expense_attrs.get("ongoingCharge") or expense_attrs.get("totalFees"))
    aum_m = (
        decimal_to_string(parse_decimal(netasset_attrs.get("value")), places=2)
        if parse_decimal(netasset_attrs.get("value")) is not None
        else ""
    )
    date = (
        parse_date(netasset_attrs.get("asOfDate"))
        or parse_date(pvdd_info.get("asOfDate"))
        or parse_date(price_attrs.get("asOfDate"))
        or today.strftime("%d/%m/%Y")
    )

    return {
        "ETF Name": etf_name,
        "Issuer": ISSUER,
        "ISIN": isin,
        "CCY": ccy,
        "TER(bps)": ter_bps,
        "AUM(M)": aum_m,
        "Date": date,
    }


def count_missing(rows: list[dict[str, str]]) -> dict[str, int]:
    return {
        column: sum(1 for row in rows if not clean_text(row.get(column)))
        for column in OUTPUT_COLUMNS
    }


def scrape_alliance_bernstein_etfs() -> Path:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)
    session = build_session()

    detail_urls = discover_etf_detail_urls(session)
    logging.info("Discovered %d AllianceBernstein UCITS ETF fund link(s).", len(detail_urls))

    rows: list[dict[str, str]] = []
    for index, detail_url in enumerate(detail_urls, start=1):
        isin = extract_isin_from_detail_url(detail_url)
        logging.info("Fetching AllianceBernstein ETF [%d/%d] %s", index, len(detail_urls), isin)

        page_title = ""
        try:
            html = fetch_text(session, detail_url)
            page_title = extract_title(html)
        except Exception as exc:  # noqa: BLE001
            logging.warning("AllianceBernstein detail page fetch failed for %s: %s", detail_url, exc)

        try:
            payloads = fetch_shareclass_payloads(session, isin)
            row = build_row(detail_url, page_title, payloads, now)
            rows.append(row)
        except Exception as exc:  # noqa: BLE001
            logging.warning("AllianceBernstein API extraction failed for %s: %s", isin, exc)
            rows.append(
                {
                    "ETF Name": normalize_etf_name("", "", detail_url, page_title),
                    "Issuer": ISSUER,
                    "ISIN": isin,
                    "CCY": "",
                    "TER(bps)": "",
                    "AUM(M)": "",
                    "Date": now.strftime("%d/%m/%Y"),
                }
            )

    rows.sort(key=lambda row: row.get("ISIN", ""))
    missing_counts = count_missing(rows)
    write_json(
        output_path,
        {
            "captured_at": now.isoformat(),
            "provider": ISSUER,
            "detail_url_count": len(detail_urls),
            "row_count": len(rows),
            "listing_rows": rows,
        },
    )

    logging.info("Extracted %d AllianceBernstein UCITS ETF row(s).", len(rows))
    for field_name in OUTPUT_COLUMNS:
        logging.info("Missing %-9s: %d", field_name, missing_counts[field_name])

    print(f"Discovered fund links : {len(detail_urls):,}")
    print(f"Extracted ETF rows    : {len(rows):,}")
    print(f"Output file           : {output_path}")
    print("Missing field counts  :")
    for field_name in OUTPUT_COLUMNS:
        print(f"  {field_name}: {missing_counts[field_name]:,}")

    return output_path


def main() -> None:
    scrape_alliance_bernstein_etfs()


if __name__ == "__main__":
    main()
