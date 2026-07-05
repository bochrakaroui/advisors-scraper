"""Scrape Alpha UCITS / Fair Oaks ETF share classes from justETF profile pages."""

from __future__ import annotations

import io
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
from pypdf import PdfReader

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - legacy official-site flow only
    sync_playwright = None  # type: ignore[assignment]

try:
    from scrapers.justetf_profile import build_session as build_justetf_session
    from scrapers.justetf_profile import fetch_profile as fetch_justetf_profile
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from justetf_profile import build_session as build_justetf_session
    from justetf_profile import fetch_profile as fetch_justetf_profile

try:
    from scrapers.tls_compat import (
        browser_launch_args,
        context_https_kwargs,
        session_get,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from tls_compat import browser_launch_args, context_https_kwargs, session_get


ISSUER = "Alpha UCITS"
MANAGER = "Fair Oaks Capital"
JUSTETF_PROFILE_URL_TEMPLATE = "https://www.justetf.com/en/etf-profile.html?isin={isin}"
CISION_PRESSROOM_URL = "https://news.cision.com/fair-oaks-capital-etfs"
STATIC_PAGE_URL = "https://www.clo-etf.com/faaa-clo-etf/"
FACTSHEET_URL = "https://www.clo-etf.com/wp-content/uploads/2026/06/FAAA-ETF-Factsheet-May-26.pdf"
REQUEST_TIMEOUT_S = 60
PAGE_TIMEOUT_MS = 120_000
PAGE_SETTLE_MS = 3_000
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
CISION_URLS_ENV_VAR = "FAIR_OAKS_CISION_URLS"
MAX_CISION_ARTICLE_URLS = 12
DEFAULT_CISION_ARTICLE_URLS = (
    "https://news.cision.com/fair-oaks-capital-etfs/r/net-asset-value-s-,c4368378",
    "https://news.cision.com/fair-oaks-capital-etfs/r/net-asset-value-s-,c4367827",
)

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Alpha_Ucits"
RAW_FILENAME = "alpha_ucits_etf_export.json"

OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    "AUM(M)",
    "AUM CCY",
    "Date",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
}

TARGET_SHARE_CLASSES = {
    "LU2785470191": {
        "fund_name": "Fair Oaks AAA CLO Fund",
        "share_class_name": "UCITS ETF EUR Dist.",
        "etf_name": "Fair Oaks AAA CLO Fund - UCITS ETF EUR Dist.",
        "official_selector_label": "EUR Dist.",
        "ccy": "EUR",
        "aum_ccy": "EUR",
        "ter_percent": "0.35",
        "factsheet_tickers": "LSEG: FAAA; Xetra: LAAA",
        "factsheet_listings": "LSEG; Xetra",
        "share_class_launch_date": "11-Sep-24",
        "fund_structure": "Luxembourg SICAV (Alpha UCITS SICAV)",
    },
    "LU2825557270": {
        "fund_name": "Fair Oaks AAA CLO Fund",
        "share_class_name": "UCITS ETF GBP Hedged Acc.",
        "etf_name": "Fair Oaks AAA CLO Fund - UCITS ETF GBP Hedged Acc.",
        "official_selector_label": "GBP Hedged Acc.",
        "ccy": "GBP",
        "aum_ccy": "EUR",
        "ter_percent": "0.35",
        "factsheet_tickers": "LSEG: XAAA",
        "factsheet_listings": "LSEG",
        "share_class_launch_date": "04-Feb-25",
        "fund_structure": "Luxembourg SICAV (Alpha UCITS SICAV)",
    },
}

SPACE_RE = re.compile(r"\s+")
ISIN_RE = re.compile(r"^[A-Z0-9]{12}$")
PHRASE_RE = re.compile(r"[^a-z0-9]+")
FACTSHEET_TER_RE = re.compile(r"Share class TER p\.a\.\d*\s+([0-9.]+%)", re.IGNORECASE)
FACTSHEET_STRUCTURE_RE = re.compile(r"Legal structure\s+([^\n]+)", re.IGNORECASE)
AJAX_OBJECT_RE = re.compile(r"var\s+ajax_object\s*=\s*(\{.*?\})\s*;", re.DOTALL)
AUM_DISPLAY_RE = re.compile(r"([0-9][0-9.,]*)\s*([mb])\b", re.IGNORECASE)


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / RAW_FILENAME


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").replace("£", "GBP ").replace("€", "EUR ").strip()
    cleaned = SPACE_RE.sub(" ", cleaned)
    return "" if cleaned.lower() in {"", "-", "--", "none", "null", "n/a", "nan"} else cleaned


def normalize_phrase(value: object | None) -> str:
    return PHRASE_RE.sub(" ", clean_text(value).lower()).strip()


def normalize_isin(value: object | None) -> str:
    cleaned = clean_text(value).upper().replace(" ", "")
    return cleaned if ISIN_RE.fullmatch(cleaned) else ""


def normalize_ccy(value: object | None) -> str:
    cleaned = clean_text(value).upper()
    return cleaned if re.fullmatch(r"[A-Z]{3}", cleaned) else ""


def currency_symbol_to_ccy(value: object | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if "GBP" in text or "£" in str(value):
        return "GBP"
    if "EUR" in text or "€" in str(value):
        return "EUR"
    if "USD" in text or "$" in str(value):
        return "USD"
    return ""


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


def percent_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    rendered = format_decimal(decimal_value * Decimal("100"), places=2)
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def amount_to_millions(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def displayed_amount_to_millions(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    match = AUM_DISPLAY_RE.search(cleaned)
    if not match:
        return ""

    numeric = parse_decimal(match.group(1))
    if numeric is None:
        return ""

    unit = match.group(2).lower()
    if unit == "b":
        numeric *= Decimal("1000")

    return format_decimal(numeric, places=2)


def parse_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    cleaned = re.sub(r"^\(?(?:as at|as of)\s+", "", cleaned, flags=re.IGNORECASE).rstrip(")")

    for candidate in [cleaned, cleaned.replace("Z", "+00:00")]:
        try:
            return datetime.fromisoformat(candidate).strftime("%d/%m/%Y")
        except ValueError:
            continue

    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d-%b-%y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%B %Y",
        "%b %Y",
    ):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


def fetch_text(session: requests.Session, url: str) -> str:
    response = session_get(session, url, logger=logging.getLogger(__name__), timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.text


def fetch_bytes(session: requests.Session, url: str) -> bytes:
    response = session_get(session, url, logger=logging.getLogger(__name__), timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.content


def dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_text(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def parse_cision_urls_env() -> list[str]:
    raw_value = clean_text(os.environ.get(CISION_URLS_ENV_VAR))
    if not raw_value:
        return []
    return dedupe_preserve_order(
        [
            candidate.strip()
            for candidate in re.split(r"[\s,;|]+", raw_value)
            if candidate.strip()
        ]
    )


def parse_cision_pressroom_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    discovered: list[str] = []

    for link in soup.find_all("a", href=True):
        href = clean_text(link.get("href"))
        if not href:
            continue
        resolved = requests.compat.urljoin(CISION_PRESSROOM_URL, href).split("#", 1)[0]
        resolved_lower = resolved.lower()
        title_text = clean_text(
            " ".join(
                filter(
                    None,
                    (
                        link.get_text(" ", strip=True),
                        clean_text(link.get("title")),
                        clean_text(link.get("aria-label")),
                    ),
                )
            )
        )
        if "/fair-oaks-capital-etfs/r/" not in resolved_lower:
            continue
        if "net-asset-value" not in resolved_lower and "net asset value" not in normalize_phrase(title_text):
            continue
        discovered.append(resolved)

    if not discovered:
        discovered.extend(
            match.group(0)
            for match in re.finditer(
                r"https://news\.cision\.com/fair-oaks-capital-etfs/r/net-asset-value[^\"'#< ]+",
                html,
                flags=re.IGNORECASE,
            )
        )

    return dedupe_preserve_order(discovered)[:MAX_CISION_ARTICLE_URLS]


def discover_cision_article_urls(session: requests.Session) -> list[str]:
    env_urls = parse_cision_urls_env()
    if env_urls:
        return env_urls

    try:
        pressroom_html = fetch_text(session, CISION_PRESSROOM_URL)
        discovered = parse_cision_pressroom_urls(pressroom_html)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not fetch Fair Oaks Cision pressroom: %s", exc)
        discovered = []

    return dedupe_preserve_order(discovered + list(DEFAULT_CISION_ARTICLE_URLS))


def resolve_nav_table_headers(cells: list[str]) -> dict[str, int]:
    header_indexes: dict[str, int] = {}
    for index, cell in enumerate(cells):
        normalized = normalize_phrase(cell)
        if normalized == "fund name":
            header_indexes["fund_name"] = index
        elif normalized == "share class name":
            header_indexes["share_class_name"] = index
        elif normalized == "date":
            header_indexes["date"] = index
        elif normalized == "isin":
            header_indexes["isin"] = index
        elif normalized == "currency":
            header_indexes["ccy"] = index
        elif normalized == "nav per share":
            header_indexes["nav_per_share"] = index
        elif normalized == "shares outstanding":
            header_indexes["shares_outstanding"] = index
        elif normalized.startswith("fund total net assets"):
            header_indexes["aum_raw"] = index
    return header_indexes


def parse_cision_nav_rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    parsed_rows: list[dict[str, str]] = []

    for table in soup.find_all("table"):
        table_rows: list[list[str]] = []
        for row in table.find_all("tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            if any(cells):
                table_rows.append(cells)

        if len(table_rows) < 2:
            continue

        header_indexes = resolve_nav_table_headers(table_rows[0])
        required_keys = {
            "fund_name",
            "share_class_name",
            "date",
            "isin",
            "ccy",
            "nav_per_share",
            "shares_outstanding",
            "aum_raw",
        }
        if set(header_indexes) != required_keys:
            continue

        for data_row in table_rows[1:]:
            isin_index = header_indexes["isin"]
            if isin_index >= len(data_row):
                continue
            isin = normalize_isin(data_row[isin_index])
            if not isin:
                continue

            parsed_rows.append(
                {
                    "Fund name": data_row[header_indexes["fund_name"]],
                    "Share class name": data_row[header_indexes["share_class_name"]],
                    "Date": data_row[header_indexes["date"]],
                    "ISIN": isin,
                    "Currency": data_row[header_indexes["ccy"]],
                    "NAV per share": data_row[header_indexes["nav_per_share"]],
                    "Shares outstanding": data_row[header_indexes["shares_outstanding"]],
                    "Fund total net assets (EUR)": data_row[header_indexes["aum_raw"]],
                }
            )

        if parsed_rows:
            break

    return parsed_rows


def fetch_latest_cision_nav_rows(session: requests.Session) -> tuple[dict[str, dict[str, str]], list[str]]:
    target_isins = set(TARGET_SHARE_CLASSES)
    rows_by_isin: dict[str, dict[str, str]] = {}
    successful_urls: list[str] = []

    for article_url in discover_cision_article_urls(session):
        try:
            article_html = fetch_text(session, article_url)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Could not fetch Fair Oaks Cision article %s: %s", article_url, exc)
            continue

        parsed_rows = parse_cision_nav_rows(article_html)
        if not parsed_rows:
            continue

        matched_any = False
        for row in parsed_rows:
            isin = normalize_isin(row.get("ISIN"))
            if isin not in target_isins:
                continue
            matched_any = True
            enriched_row = dict(row)
            enriched_row["AUM Source URL"] = article_url
            rows_by_isin[isin] = enriched_row

        if matched_any:
            successful_urls.append(article_url)
        if target_isins.issubset(rows_by_isin):
            break

    return rows_by_isin, successful_urls


def extract_ajax_config(html: str) -> tuple[str, str]:
    match = AJAX_OBJECT_RE.search(html)
    if not match:
        raise RuntimeError("Fair Oaks page did not expose the AJAX config needed for daily share-class data.")

    payload = json.loads(match.group(1))
    ajax_url = clean_text(payload.get("ajax_url"))
    nonce = clean_text(payload.get("nonce"))
    if not ajax_url or not nonce:
        raise RuntimeError("Fair Oaks AJAX config was missing the URL or nonce.")
    return ajax_url, nonce


def extract_official_page_metadata(html: str) -> tuple[dict[str, str], dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    option_map: dict[str, str] = {}
    for option in soup.select("option[data-ajax-source]"):
        label = clean_text(option.get_text(" ", strip=True))
        post_id = clean_text(option.get("data-ajax-source"))
        if label and post_id:
            option_map[label] = post_id

    assets_display = clean_text(
        soup.select_one(".total-fund-assets").get_text(" ", strip=True)
        if soup.select_one(".total-fund-assets")
        else ""
    )
    assets_date = clean_text(
        soup.select_one(".total-fund-assets-date").get_text(" ", strip=True)
        if soup.select_one(".total-fund-assets-date")
        else ""
    )

    return option_map, {
        "Fund total net assets display": assets_display,
        "Fund total net assets display date": assets_date,
    }


def fetch_official_ajax_payload(page: Any, ajax_url: str, nonce: str, post_id: str) -> dict[str, Any]:
    payload = page.evaluate(
        """
        async ({ ajaxUrl, nonce, postId }) => {
            const response = await fetch(ajaxUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                body: new URLSearchParams({
                    action: "update_acf_data",
                    post_id: postId,
                    nonce,
                }).toString(),
            });
            return await response.json();
        }
        """,
        {"ajaxUrl": ajax_url, "nonce": nonce, "postId": post_id},
    )

    if not isinstance(payload, dict) or not payload.get("success") or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"Fair Oaks AJAX payload for post_id={post_id} was not a successful JSON response.")
    return payload["data"]


def build_official_nav_row(
    isin: str,
    reference: dict[str, str],
    payload: dict[str, Any],
    page_metadata: dict[str, str],
    ajax_url: str,
    post_id: str,
) -> dict[str, str]:
    return {
        "Fund name": clean_text(reference.get("fund_name")),
        "Share class name": clean_text(reference.get("share_class_name")),
        "Date": clean_text(payload.get("net_value_as_at_date")) or clean_text(page_metadata.get("Fund total net assets display date")),
        "ISIN": normalize_isin(payload.get("isin")) or isin,
        "Currency": normalize_ccy(reference.get("ccy")) or currency_symbol_to_ccy(payload.get("currency_symbol")),
        "NAV per share": clean_text(payload.get("net_asset_value")),
        "Shares outstanding": "",
        "Fund total net assets (EUR)": "",
        "Fund total net assets display": clean_text(page_metadata.get("Fund total net assets display")),
        "Fund total net assets display date": clean_text(page_metadata.get("Fund total net assets display date")),
        "Official AJAX URL": ajax_url,
        "Official AJAX post_id": clean_text(post_id),
        "Official factsheet URL": clean_text(payload.get("factsheet")) or FACTSHEET_URL,
        "Official share class label": clean_text(payload.get("full_share_class_name")),
    }


def fetch_official_nav_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=browser_launch_args("--disable-blink-features=AutomationControlled", "--no-sandbox"),
        )
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
            timezone_id="Europe/London",
            **context_https_kwargs(),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.goto(STATIC_PAGE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(PAGE_SETTLE_MS)

        html = page.content()
        ajax_url, nonce = extract_ajax_config(html)
        option_map, page_metadata = extract_official_page_metadata(html)

        for isin, reference in TARGET_SHARE_CLASSES.items():
            selector_label = clean_text(reference.get("official_selector_label"))
            post_id = option_map.get(selector_label, "")
            if not post_id:
                logging.warning("Fair Oaks page did not expose selector metadata for %s (%s).", isin, selector_label)
                continue
            payload = fetch_official_ajax_payload(page, ajax_url, nonce, post_id)
            rows.append(build_official_nav_row(isin, reference, payload, page_metadata, ajax_url, post_id))

        context.close()
        browser.close()

    if not rows:
        raise RuntimeError("No Alpha UCITS / Fair Oaks rows were captured from the official page.")

    return rows


def merge_source_rows(
    official_rows: list[dict[str, str]],
    cision_rows_by_isin: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    merged_rows: list[dict[str, str]] = []

    for official_row in official_rows:
        isin = normalize_isin(official_row.get("ISIN"))
        merged_row = dict(official_row)
        cision_row = cision_rows_by_isin.get(isin)

        official_date = parse_date(official_row.get("Date"))
        cision_date = parse_date(cision_row.get("Date")) if cision_row else ""

        if cision_row and official_date and cision_date == official_date:
            merged_row["Shares outstanding"] = clean_text(cision_row.get("Shares outstanding"))
            merged_row["Fund total net assets (EUR)"] = clean_text(cision_row.get("Fund total net assets (EUR)"))
            merged_row["AUM Source URL"] = clean_text(cision_row.get("AUM Source URL"))
            merged_row["AUM Source Precision"] = "exact_cision_same_date"
        else:
            merged_row["Shares outstanding"] = ""
            merged_row["Fund total net assets (EUR)"] = ""
            merged_row["AUM Source URL"] = STATIC_PAGE_URL
            merged_row["AUM Source Precision"] = "rounded_official_display"
            if cision_row:
                merged_row["Stale Cision Date"] = clean_text(cision_row.get("Date"))
                merged_row["Stale Cision AUM"] = clean_text(cision_row.get("Fund total net assets (EUR)"))
                merged_row["Stale Cision Shares outstanding"] = clean_text(cision_row.get("Shares outstanding"))
                merged_row["Stale Cision Source URL"] = clean_text(cision_row.get("AUM Source URL"))

        merged_rows.append(merged_row)

    return merged_rows


def load_reference_metadata(session: requests.Session) -> dict[str, dict[str, str]]:
    metadata = {
        isin: {key: clean_text(value) for key, value in values.items()}
        for isin, values in TARGET_SHARE_CLASSES.items()
    }

    try:
        factsheet_bytes = fetch_bytes(session, FACTSHEET_URL)
        factsheet_text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(io.BytesIO(factsheet_bytes)).pages
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not load Fair Oaks factsheet reference metadata: %s", exc)
        return metadata

    ter_match = FACTSHEET_TER_RE.search(factsheet_text)
    structure_match = FACTSHEET_STRUCTURE_RE.search(factsheet_text)
    ter_percent = clean_text(ter_match.group(1)) if ter_match else ""
    fund_structure = clean_text(structure_match.group(1)) if structure_match else ""

    for values in metadata.values():
        if ter_percent:
            values["ter_percent"] = ter_percent
        if fund_structure:
            values["fund_structure"] = fund_structure

    return metadata


def log_missing_official_field(etf_name: str, field_name: str) -> None:
    logging.warning(
        "Alpha UCITS / Fair Oaks official source does not expose %s for %s; leaving blank.",
        field_name,
        etf_name,
    )


def build_output_row(
    nav_row: dict[str, str],
    reference_metadata: dict[str, dict[str, str]],
    today: datetime,
) -> dict[str, str]:
    isin = normalize_isin(nav_row.get("ISIN"))
    reference = reference_metadata.get(isin, {})
    etf_name = clean_text(reference.get("etf_name")) or clean_text(
        f"{clean_text(reference.get('fund_name') or nav_row.get('Fund name'))} - "
        f"{clean_text(reference.get('share_class_name') or nav_row.get('Share class name'))}"
    )

    aum_millions = amount_to_millions(nav_row.get("Fund total net assets (EUR)"))
    if not aum_millions:
        aum_millions = displayed_amount_to_millions(nav_row.get("Fund total net assets display"))

    aum_ccy = normalize_ccy(reference.get("aum_ccy"))
    if not aum_ccy and (aum_millions or clean_text(nav_row.get("Fund total net assets display"))):
        aum_ccy = "EUR"

    row = {
        "ETF Name": etf_name,
        "Issuer": ISSUER,
        "ISIN": isin,
        "CCY": normalize_ccy(nav_row.get("Currency")) or normalize_ccy(reference.get("ccy")),
        "TER(bps)": percent_to_bps(reference.get("ter_percent")),
        "AUM(M)": aum_millions,
        "AUM CCY": aum_ccy,
        "Date": parse_date(nav_row.get("Date"))
        or parse_date(nav_row.get("Fund total net assets display date"))
        or today.strftime("%d/%m/%Y"),
    }

    for field_name in ("ISIN", "CCY", "TER(bps)", "AUM(M)", "AUM CCY"):
        if not clean_text(row.get(field_name)):
            log_missing_official_field(etf_name, field_name)

    row.update(
        {
            "Manager": MANAGER,
            "Fund name": clean_text(reference.get("fund_name")) or clean_text(nav_row.get("Fund name")),
            "Share class name": clean_text(reference.get("share_class_name")) or clean_text(nav_row.get("Share class name")),
            "NAV per share": clean_text(nav_row.get("NAV per share")),
            "Shares outstanding": clean_text(nav_row.get("Shares outstanding")),
            "Fund total net assets (EUR)": clean_text(nav_row.get("Fund total net assets (EUR)")),
            "Fund total net assets display": clean_text(nav_row.get("Fund total net assets display")),
            "Fund total net assets display date": clean_text(nav_row.get("Fund total net assets display date")),
            "AUM Source URL": clean_text(nav_row.get("AUM Source URL")),
            "AUM Source Precision": clean_text(nav_row.get("AUM Source Precision")),
            "Official AJAX URL": clean_text(nav_row.get("Official AJAX URL")),
            "Official AJAX post_id": clean_text(nav_row.get("Official AJAX post_id")),
            "Official share class label": clean_text(nav_row.get("Official share class label")),
            "Reference Static URL": STATIC_PAGE_URL,
            "Reference Factsheet URL": clean_text(nav_row.get("Official factsheet URL")) or FACTSHEET_URL,
            "Factsheet Tickers": clean_text(reference.get("factsheet_tickers")),
            "Factsheet Listings": clean_text(reference.get("factsheet_listings")),
            "Fund Structure": clean_text(reference.get("fund_structure")),
            "Share class launch date": clean_text(reference.get("share_class_launch_date")),
            "Stale Cision Date": clean_text(nav_row.get("Stale Cision Date")),
            "Stale Cision AUM": clean_text(nav_row.get("Stale Cision AUM")),
            "Stale Cision Shares outstanding": clean_text(nav_row.get("Stale Cision Shares outstanding")),
            "Stale Cision Source URL": clean_text(nav_row.get("Stale Cision Source URL")),
        }
    )
    return row


def build_output_rows_from_justetf(today: datetime) -> list[dict[str, str]]:
    session = build_justetf_session()
    output_rows: list[dict[str, str]] = []

    for index, (isin, reference) in enumerate(TARGET_SHARE_CLASSES.items(), 1):
        logging.info("Fetching justETF profile [%d/%d] %s", index, len(TARGET_SHARE_CLASSES), isin)
        try:
            profile = fetch_justetf_profile(isin, session=session)
            aum_millions = clean_text(profile.get("aum_mn"))
            aum_ccy = normalize_ccy(profile.get("aum_ccy")) or normalize_ccy(reference.get("aum_ccy"))
            row = {
                "ETF Name": clean_text(profile.get("etf_name")) or clean_text(reference.get("etf_name")),
                "Issuer": ISSUER,
                "ISIN": normalize_isin(profile.get("isin")) or isin,
                "CCY": normalize_ccy(profile.get("ccy")) or normalize_ccy(reference.get("ccy")),
                "TER(bps)": clean_text(profile.get("ter_bps")) or percent_to_bps(reference.get("ter_percent")),
                "AUM(M)": aum_millions,
                "AUM CCY": aum_ccy,
                "Date": today.strftime("%d/%m/%Y"),
                "Manager": MANAGER,
                "Fund name": clean_text(reference.get("fund_name")) or clean_text(profile.get("index_name")),
                "Share class name": clean_text(reference.get("share_class_name")),
                "Distribution policy": clean_text(profile.get("distribution_policy")),
                "Distribution frequency": clean_text(profile.get("distribution_frequency")),
                "Replication": clean_text(profile.get("replication")),
                "Fund total net assets display": clean_text(profile.get("fund_size_raw")),
                "AUM Source URL": clean_text(profile.get("profile_url")) or JUSTETF_PROFILE_URL_TEMPLATE.format(isin=isin),
                "AUM Source Precision": "justetf_displayed_fund_size",
                "justETF Profile URL": clean_text(profile.get("profile_url")) or JUSTETF_PROFILE_URL_TEMPLATE.format(isin=isin),
                "justETF Page Title": clean_text(profile.get("page_title")),
                "Fund Provider": clean_text(profile.get("fund_provider")),
                "Fund domicile": clean_text(profile.get("fund_domicile")),
                "Fund Structure": clean_text(profile.get("fund_structure")) or clean_text(reference.get("fund_structure")),
                "Index": clean_text(profile.get("index_name")),
                "Investment focus": clean_text(profile.get("investment_focus")),
                "Investment approach": clean_text(profile.get("investment_approach")),
                "Share class launch date": clean_text(profile.get("inception_date_raw")) or clean_text(reference.get("share_class_launch_date")),
                "Factsheet Tickers": clean_text(reference.get("factsheet_tickers")),
                "Factsheet Listings": clean_text(reference.get("factsheet_listings")),
                "Source kind": "justetf_profile",
                "Fetch status": clean_text(profile.get("fetch_status")) or "ok",
            }

            if clean_text(profile.get("error")):
                row["Error"] = clean_text(profile.get("error"))
        except Exception as exc:
            logging.warning("justETF profile failed for %s: %s", isin, exc)
            row = {
                "ETF Name": clean_text(reference.get("etf_name")),
                "Issuer": ISSUER,
                "ISIN": isin,
                "CCY": normalize_ccy(reference.get("ccy")),
                "TER(bps)": percent_to_bps(reference.get("ter_percent")),
                "AUM(M)": "",
                "AUM CCY": normalize_ccy(reference.get("aum_ccy")),
                "Date": today.strftime("%d/%m/%Y"),
                "Manager": MANAGER,
                "Fund name": clean_text(reference.get("fund_name")),
                "Share class name": clean_text(reference.get("share_class_name")),
                "Distribution policy": "",
                "Distribution frequency": "",
                "Replication": "",
                "Fund total net assets display": "",
                "AUM Source URL": JUSTETF_PROFILE_URL_TEMPLATE.format(isin=isin),
                "AUM Source Precision": "",
                "justETF Profile URL": JUSTETF_PROFILE_URL_TEMPLATE.format(isin=isin),
                "justETF Page Title": "",
                "Fund Provider": "",
                "Fund domicile": "",
                "Fund Structure": clean_text(reference.get("fund_structure")),
                "Index": "",
                "Investment focus": "",
                "Investment approach": "",
                "Share class launch date": clean_text(reference.get("share_class_launch_date")),
                "Factsheet Tickers": clean_text(reference.get("factsheet_tickers")),
                "Factsheet Listings": clean_text(reference.get("factsheet_listings")),
                "Source kind": "justetf_profile",
                "Fetch status": "failed",
                "Error": str(exc),
            }

        for field_name in ("ISIN", "CCY", "TER(bps)", "AUM(M)", "AUM CCY"):
            if not clean_text(row.get(field_name)):
                logging.warning("Alpha UCITS justETF profile did not expose %s for %s.", field_name, row["ETF Name"])

        output_rows.append(row)

    return output_rows


def write_json(output_path: Path, payload: object) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def count_missing(rows: list[dict[str, str]]) -> dict[str, int]:
    return {
        column: sum(1 for row in rows if not clean_text(row.get(column)))
        for column in OUTPUT_COLUMNS
    }


def scrape_alpha_ucits() -> Path:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)
    output_rows = build_output_rows_from_justetf(now)
    missing_counts = count_missing(output_rows)

    missing_target_isins = [isin for isin in TARGET_SHARE_CLASSES if isin not in {row["ISIN"] for row in output_rows}]
    for missing_isin in missing_target_isins:
        logging.warning("Expected Fair Oaks target ISIN was not present in the justETF output: %s", missing_isin)

    write_json(
        output_path,
        {
            "captured_at": now.isoformat(),
            "provider": ISSUER,
            "manager": MANAGER,
            "method": (
                "Target ISIN fetch from public justETF ETF profile pages."
            ),
            "profile_url_template": JUSTETF_PROFILE_URL_TEMPLATE,
            "target_isins": list(TARGET_SHARE_CLASSES),
            "row_count": len(output_rows),
            "listing_rows": output_rows,
        },
    )

    logging.info("Extracted %d Alpha UCITS / Fair Oaks ETF row(s) from justETF.", len(output_rows))
    for field_name in OUTPUT_COLUMNS:
        logging.info("Missing %-9s: %d", field_name, missing_counts[field_name])

    print(f"Extracted ETF rows         : {len(output_rows):,}")
    print(f"Output file                : {output_path}")
    print("Missing field counts       :")
    for field_name in OUTPUT_COLUMNS:
        print(f"  {field_name}: {missing_counts[field_name]:,}")

    return output_path


def main() -> None:
    scrape_alpha_ucits()


if __name__ == "__main__":
    main()
