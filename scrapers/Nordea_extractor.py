"""Nordea UCITS ETF scraper using Nordea's official professional fund centre."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from scrapers.tls_compat import session_get
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from tls_compat import session_get


FUNDS_URL = "https://www.nordea.co.uk/en/professional/funds/"
FUNDS_SHARE_CLASSES_URL = f"{FUNDS_URL}?tab=share-classes"
APP_ID = "2717fe1e-9849-4d97-84ea-fccd8e78a145"
SEARCH_ENTITY_URL = (
    f"https://api-eu.kurtosys.app/applicationManager/apps/{APP_ID}/services/fund/searchEntity"
)
DATASET_EXECUTE_URL = "https://api-eu.kurtosys.app/dataset/execute"
ISSUER = "Nordea"
OUTPUT_COLUMNS = ["ETF Name", "Issuer", "ISIN", "CCY", "TER(bps)", "AUM(M)", "AUM CCY", "Date"]
REQUEST_TIMEOUT_S = 45
PAGE_TIMEOUT_MS = 60_000
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "providers" / "Nordea"
RAW_FILENAME = "nordea_etf_export.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
DISCLOSURE_URLS_ENV_VAR = "NORDEA_DISCLOSURE_URLS"
CISION_PRESSROOM_URL = "https://news.cision.com/nordea-icav-etf"
MAX_CISION_DISCLOSURE_URLS = 12
DEFAULT_DISCLOSURE_URLS = (
    "https://news.cision.com/nordea-icav-etf/r/net-asset-value-s-,c4368991",
    "https://uk.finance.yahoo.com/news/nordea-icav-etf-net-asset-060000904.html",
    "https://finance.yahoo.com/news/nordea-icav-etf-net-asset-060000904.html",
)
DISCLOSURE_SEARCH_URLS = (
    "https://uk.search.yahoo.com/search?p=Nordea+ICAV+ETF+net+asset+value",
    "https://search.yahoo.com/search?p=Nordea+ICAV+ETF+net+asset+value",
)
DISCLOSURE_ARTICLE_PATH_RE = re.compile(r"/news/nordea-icav-etf-[^\"'#? ]+\.html", re.IGNORECASE)
KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
PHRASE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
INLINE_SPACE_RE = re.compile(r"[ \t]+")
ISIN_PATTERN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
INLINE_AUM_PATTERNS = (
    re.compile(
        r"Shareholder\s+Equity(?:\s+Base|\s+Attributable\s+to\s+Shareholders)?(?:\s*\(AUM\))?\s*[:\-]?\s*([^\n\r]+)",
        re.IGNORECASE,
    ),
)
INLINE_NAV_PATTERNS = (
    re.compile(r"NAV\s*(?:per\s*Share|/Share)?\s*[:\-]?\s*([^\n\r]+)", re.IGNORECASE),
)
INLINE_UNITS_PATTERNS = (
    re.compile(r"(?:Units|Shares)\s+Outstanding\s*[:\-]?\s*([^\n\r]+)", re.IGNORECASE),
)
INLINE_CCY_PATTERNS = (
    re.compile(r"(?:Local|Base)\s+Currency\s*[:\-]?\s*([^\n\r]+)", re.IGNORECASE),
)
INLINE_DATE_PATTERNS = (
    re.compile(r"\bAs\s+at\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\b", re.IGNORECASE),
    re.compile(r"\bAs\s+of\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\b", re.IGNORECASE),
    re.compile(r"\bDate\s*[:\-]?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\b", re.IGNORECASE),
    re.compile(r"\b([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})\b"),
)
DISCLOSURE_LABEL_KEYS = {
    "aum_raw": {
        "shareholder equity",
        "shareholder equity attributable to shareholders",
        "shareholder equity base",
        "shareholder equity base aum",
    },
    "nav_per_share": {"nav", "nav per share", "nav share"},
    "units_outstanding": {"shares outstanding", "units outstanding"},
    "aum_ccy": {"base currency", "currency", "local currency"},
}
ALL_DISCLOSURE_LABEL_KEYS = {
    key for keys in DISCLOSURE_LABEL_KEYS.values() for key in keys
}
INVALID_DISCLOSURE_CURRENCY_CODES = {"ETF", "NAV", "AUM"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\u00ad", "").strip()
    return "" if text in {"", "-", "--", "None", "null"} else " ".join(text.split())


def clean_multiline_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\u00ad", "")
    lines = [clean_text(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_isin(value: Any) -> str:
    isin = clean_text(value).upper().replace(" ", "")
    return isin if len(isin) == 12 else ""


def normalize_ccy(value: Any) -> str:
    ccy = clean_text(value).upper()
    return ccy if len(ccy) == 3 and ccy.isalpha() else ""


def extract_currency_code(value: Any) -> str:
    direct = normalize_ccy(value)
    if direct:
        return direct
    match = re.search(r"\b([A-Z]{3})\b", clean_text(value).upper())
    return match.group(1) if match else ""


def parse_date(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    candidates = (
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y",
        "%m/%d/%Y",
    )
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def parse_percentage_to_bps(value: Any) -> str:
    if value is None:
        return ""
    text = clean_text(value).replace("%", "").replace(",", ".")
    if not text:
        return ""
    try:
        numeric = float(text)
    except ValueError:
        return ""
    if math.isnan(numeric) or math.isinf(numeric):
        return ""
    bps = numeric * 100
    formatted = f"{bps:.2f}".rstrip("0").rstrip(".")
    return formatted


def normalize_number_text(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""

    filtered = "".join(ch for ch in text if ch.isdigit() or ch in ",.-")
    if not filtered:
        return ""

    if "," in filtered and "." in filtered:
        if filtered.rfind(",") > filtered.rfind("."):
            filtered = filtered.replace(".", "").replace(",", ".")
        else:
            filtered = filtered.replace(",", "")
    elif filtered.count(",") == 1 and "." not in filtered:
        filtered = filtered.replace(",", ".")
    else:
        filtered = filtered.replace(",", "")

    if filtered.count(".") > 1:
        head, tail = filtered.rsplit(".", 1)
        filtered = head.replace(".", "") + "." + tail

    return filtered


def parse_numeric_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(value)

    normalized = normalize_number_text(value)
    if not normalized:
        return None

    try:
        return float(normalized)
    except ValueError:
        return None


def format_millions(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def parse_money_to_millions(value: Any) -> str:
    numeric = parse_numeric_value(value) if isinstance(value, (int, float)) else None
    if numeric is not None:
        return format_millions(numeric / 1_000_000)

    text = clean_text(value).upper()
    if not text:
        return ""

    multiplier = 1.0
    compact = text.replace(" ", "")
    if compact.endswith("BN") or compact.endswith("B"):
        multiplier = 1000.0
        compact = compact.removesuffix("BN").removesuffix("B")
    elif compact.endswith("MN") or compact.endswith("M"):
        compact = compact.removesuffix("MN").removesuffix("M")
    elif compact.endswith("K"):
        multiplier = 0.001
        compact = compact.removesuffix("K")

    parsed = parse_numeric_value(compact)
    if parsed is None:
        return ""
    if multiplier != 1.0:
        return format_millions(parsed * multiplier)
    if abs(parsed) >= 100_000:
        return format_millions(parsed / 1_000_000)
    return format_millions(parsed)


def compute_aum_from_nav(units_outstanding: Any, nav_per_share: Any) -> str:
    units = parse_numeric_value(units_outstanding)
    nav = parse_numeric_value(nav_per_share)
    if units is None or nav is None:
        return ""
    return parse_money_to_millions(units * nav)


def canonicalize_key(value: Any) -> str:
    return KEY_NORMALIZE_RE.sub("", clean_text(value).lower())


def normalize_phrase(value: Any) -> str:
    return PHRASE_NORMALIZE_RE.sub(" ", clean_text(value).lower()).strip()


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if isinstance(value, str):
            if clean_text(value):
                return value
            continue
        if value not in (None, "", [], {}, ()):
            return value
    return ""


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


def build_output_path(now: datetime) -> Path:
    output_dir = OUTPUT_DIR / now.strftime("%Y-%m-%d")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / RAW_FILENAME


def format_scalar(value: Any) -> str:
    if isinstance(value, list):
        return " | ".join(clean_text(item) for item in value if clean_text(item))
    return clean_text(value)


def get_property_value(record: dict[str, Any], key: str) -> Any:
    props = record.get("properties_pub")
    if not isinstance(props, dict):
        return None
    wrapper = props.get(key)
    if not isinstance(wrapper, dict):
        return None
    return wrapper.get("value")


def build_fund_detail_url(fund_names: list[str]) -> str:
    encoded_names = "|".join(quote(name) for name in fund_names if clean_text(name))
    return f"{FUNDS_URL}?tab=share-classes&_fund_name={encoded_names}"


def build_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nordea.co.uk",
            "Referer": FUNDS_SHARE_CLASSES_URL,
        }
    )
    return session


def collect_mapping_values(
    mapping: dict[str, Any] | None,
    exact_keys: tuple[str, ...],
    key_fragments: tuple[str, ...] = (),
) -> list[Any]:
    if not isinstance(mapping, dict):
        return []

    values: list[Any] = []
    seen_canonical_keys: set[str] = set()
    exact_canonical_keys = {canonicalize_key(key) for key in exact_keys}
    fragment_keys = tuple(canonicalize_key(fragment) for fragment in key_fragments)

    for key in exact_keys:
        if key not in mapping:
            continue
        value = mapping.get(key)
        if value in (None, "", [], {}, ()):
            continue
        values.append(value)
        seen_canonical_keys.add(canonicalize_key(key))

    for actual_key, value in mapping.items():
        if value in (None, "", [], {}, ()):
            continue
        canonical_key = canonicalize_key(actual_key)
        if canonical_key in seen_canonical_keys:
            continue
        if canonical_key in exact_canonical_keys or any(
            fragment in canonical_key for fragment in fragment_keys
        ):
            values.append(value)
            seen_canonical_keys.add(canonical_key)

    return values


def collect_detail_property_values(
    record: dict[str, Any] | None,
    exact_keys: tuple[str, ...],
    key_fragments: tuple[str, ...] = (),
) -> list[Any]:
    props = record.get("properties_pub") if isinstance(record, dict) else None
    if not isinstance(props, dict):
        return []

    values: list[Any] = []
    seen_canonical_keys: set[str] = set()
    exact_canonical_keys = {canonicalize_key(key) for key in exact_keys}
    fragment_keys = tuple(canonicalize_key(fragment) for fragment in key_fragments)

    for key in exact_keys:
        value = get_property_value(record, key)
        if value in (None, "", [], {}, ()):
            continue
        values.append(value)
        seen_canonical_keys.add(canonicalize_key(key))

    for actual_key, wrapper in props.items():
        if not isinstance(wrapper, dict):
            continue
        value = wrapper.get("value")
        if value in (None, "", [], {}, ()):
            continue
        canonical_key = canonicalize_key(actual_key)
        if canonical_key in seen_canonical_keys:
            continue
        if canonical_key in exact_canonical_keys or any(
            fragment in canonical_key for fragment in fragment_keys
        ):
            values.append(value)
            seen_canonical_keys.add(canonical_key)

    return values


def parse_first_money_to_millions(values: list[Any]) -> str:
    for value in values:
        parsed = parse_money_to_millions(value)
        if parsed:
            return parsed
    return ""


def parse_first_currency(values: list[Any]) -> str:
    for value in values:
        parsed = extract_currency_code(value)
        if parsed:
            return parsed
    return ""


def parse_first_date(values: list[Any]) -> str:
    for value in values:
        parsed = parse_date(value)
        if parsed:
            return parsed
    return ""


def parse_export_data_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("value")
    if not isinstance(value, dict):
        return []
    queries = value.get("queries")
    if not isinstance(queries, list):
        return []
    for query in queries:
        if isinstance(query, dict) and query.get("key") == "export_data":
            results = query.get("results")
            return results if isinstance(results, list) else []
    return []


def fetch_dataset_rows_via_requests() -> list[dict[str, Any]]:
    session = build_requests_session()
    session.get(FUNDS_SHARE_CLASSES_URL, timeout=REQUEST_TIMEOUT_S)
    response = session.post(
        DATASET_EXECUTE_URL,
        json={"code": "professional", "inputs": {"country": "gb", "culture": "en-GB"}},
        timeout=REQUEST_TIMEOUT_S,
    )
    if response.status_code != 200:
        return []
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return []
    return parse_export_data_response(payload)


def filter_etf_dataset_rows(dataset_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    seen_isins: set[str] = set()
    for row in dataset_rows:
        if not isinstance(row, dict):
            continue
        isin = normalize_isin(row.get("isin"))
        share_class_type = clean_text(row.get("shareClassType")).upper()
        fund_name = clean_text(row.get("fund"))
        umbrella = clean_text(row.get("umbrella"))
        if not isin or isin in seen_isins:
            continue
        is_etf = share_class_type == "ETF" or "ETF" in fund_name.upper() or "ETF" in umbrella.upper()
        is_ucits = "UCITS ETF" in fund_name.upper() or "UCITS ETF" in umbrella.upper()
        if is_etf and is_ucits:
            filtered.append(row)
            seen_isins.add(isin)
    filtered.sort(key=lambda row: normalize_isin(row.get("isin")))
    return filtered


def parse_disclosure_url_env() -> list[str]:
    raw_value = clean_text(os.environ.get(DISCLOSURE_URLS_ENV_VAR))
    if not raw_value:
        return []
    return dedupe_preserve_order([
        candidate.strip()
        for candidate in re.split(r"[\s,;|]+", raw_value)
        if candidate.strip()
    ])


def parse_cision_pressroom_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    discovered: list[str] = []

    for link in soup.find_all("a", href=True):
        href = clean_text(link.get("href"))
        if not href:
            continue

        resolved = urljoin(CISION_PRESSROOM_URL, href).split("#", 1)[0]
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
        title_normalized = normalize_phrase(title_text)

        if "/nordea-icav-etf/r/" not in resolved_lower:
            continue
        if "net-asset-value" not in resolved_lower and "net asset value" not in title_normalized:
            continue

        discovered.append(resolved)

    if not discovered:
        discovered.extend(
            match.group(0)
            for match in re.finditer(
                r"https://news\.cision\.com/nordea-icav-etf/r/net-asset-value[^\"'#< ]+",
                html,
                flags=re.IGNORECASE,
            )
        )

    return dedupe_preserve_order(discovered)[:MAX_CISION_DISCLOSURE_URLS]


def discover_cision_disclosure_urls(session: requests.Session) -> list[str]:
    try:
        response = session_get(
            session,
            CISION_PRESSROOM_URL,
            logger=logging.getLogger(__name__),
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"WARNING: Could not fetch Nordea Cision pressroom: {exc}")
        return []

    return parse_cision_pressroom_urls(response.text)


def discover_yahoo_disclosure_urls(session: requests.Session) -> list[str]:
    discovered: list[str] = []
    for search_url in DISCLOSURE_SEARCH_URLS:
        try:
            response = session_get(
                session,
                search_url,
                logger=logging.getLogger(__name__),
                timeout=REQUEST_TIMEOUT_S,
            )
            response.raise_for_status()
        except Exception as exc:
            print(f"WARNING: Could not search Yahoo for Nordea disclosure URLs: {exc}")
            continue

        html = response.text
        href_candidates = set(DISCLOSURE_ARTICLE_PATH_RE.findall(html))
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = clean_text(link.get("href"))
            if not href:
                continue
            if "nordea-icav-etf" in href.lower():
                href_candidates.add(href)

        for href in sorted(href_candidates):
            if href.startswith("http"):
                discovered.append(href)
            else:
                discovered.append(urljoin(search_url, href))
    return dedupe_preserve_order(discovered)


def build_disclosure_url_candidates(session: requests.Session) -> list[str]:
    env_urls = parse_disclosure_url_env()
    if env_urls:
        return env_urls

    return dedupe_preserve_order(
        discover_cision_disclosure_urls(session)
        + discover_yahoo_disclosure_urls(session)
        + list(DEFAULT_DISCLOSURE_URLS)
    )


def extract_inline_value(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return clean_text(match.group(1))
    return ""


def extract_labelled_line_value(
    text: str,
    label_keys: set[str],
) -> str:
    lines = [clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        normalized_line = normalize_phrase(line)
        if not any(
            normalized_line == label_key or label_key in normalized_line
            for label_key in label_keys
        ):
            continue
        for candidate in lines[index + 1 : index + 4]:
            normalized_candidate = normalize_phrase(candidate)
            if not candidate or normalized_candidate in ALL_DISCLOSURE_LABEL_KEYS:
                continue
            return candidate
    return ""


def matches_header_variant(normalized_cell: str, variants: set[str]) -> bool:
    return any(
        normalized_cell == variant
        or variant in normalized_cell
        or normalized_cell in variant
        for variant in variants
    )


def sanitize_numeric_capture(value: Any) -> str:
    cleaned = clean_text(value)
    return cleaned if parse_numeric_value(cleaned) is not None else ""


def sanitize_currency_capture(value: Any) -> str:
    text = clean_text(value).upper()
    if not text:
        return ""

    direct = normalize_ccy(text)
    if direct and direct not in INVALID_DISCLOSURE_CURRENCY_CODES:
        return direct

    for match in re.finditer(r"\b([A-Z]{3})\b", text):
        candidate = match.group(1)
        if candidate not in INVALID_DISCLOSURE_CURRENCY_CODES:
            return candidate
    return ""


def disclosure_record_identity(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        normalize_isin(record.get("ISIN")),
        clean_text(record.get("aum_raw")),
        clean_text(record.get("nav_per_share")),
        clean_text(record.get("units_outstanding")),
        extract_currency_code(record.get("aum_ccy")),
    )


def append_disclosure_record(
    records_by_isin: dict[str, list[dict[str, str]]],
    record: dict[str, str],
) -> None:
    isin = normalize_isin(record.get("ISIN"))
    if not isin:
        return

    record = {key: clean_text(value) for key, value in record.items()}
    identity = disclosure_record_identity(record)
    current_records = records_by_isin.setdefault(isin, [])

    for index, existing in enumerate(current_records):
        if disclosure_record_identity(existing) != identity:
            continue
        current_records[index] = merge_prefer_nonempty(existing, record)
        return

    current_records.append(record)


def select_disclosure_record(
    records: list[dict[str, str]] | None,
    expected_ccy: str,
) -> dict[str, str]:
    if not records:
        return {}

    def score(record: dict[str, str]) -> tuple[int, int, int, int, int]:
        record_ccy = extract_currency_code(record.get("aum_ccy"))
        return (
            1 if expected_ccy and record_ccy == expected_ccy else 0,
            1 if clean_text(record.get("AUM(M)")) else 0,
            1 if parse_numeric_value(record.get("nav_per_share")) is not None else 0,
            1 if parse_numeric_value(record.get("units_outstanding")) is not None else 0,
            1 if clean_text(record.get("source_url")) else 0,
        )

    return max(records, key=score)


def merge_prefer_nonempty(*records: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for record in records:
        for key, value in record.items():
            if key not in merged or not clean_text(merged.get(key)):
                merged[key] = value
    return merged


def extract_article_text_candidates(html: str) -> list[str]:
    candidates: list[str] = []
    soup = BeautifulSoup(html, "html.parser")

    body = soup.body or soup
    for tag in body(["script", "style", "noscript"]):
        tag.decompose()
    body_text = clean_multiline_text(body.get_text("\n", strip=True))
    if body_text:
        candidates.append(body_text)

    for script in soup.find_all("script", type="application/ld+json"):
        raw_json = clean_text(script.string or script.get_text(" ", strip=True))
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        payloads = payload if isinstance(payload, list) else [payload]
        for item in payloads:
            if not isinstance(item, dict):
                continue
            article_body = clean_multiline_text(item.get("articleBody"))
            if article_body:
                candidates.append(article_body)

    deduped: list[str] = []
    seen: set[str] = set()
    for text in candidates:
        normalized = INLINE_SPACE_RE.sub(" ", text)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(text)
    return deduped


def extract_article_date(html: str, text_candidates: list[str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    selectors = (
        ("meta", "property", "article:published_time"),
        ("meta", "name", "article:published_time"),
        ("meta", "property", "og:updated_time"),
        ("time", "datetime", ""),
    )
    for tag_name, attribute, expected in selectors:
        for tag in soup.find_all(tag_name):
            if expected and clean_text(tag.get(attribute)) != expected:
                continue
            candidate = clean_text(tag.get("content") or tag.get("datetime") or tag.get_text(" ", strip=True))
            parsed = parse_date(candidate)
            if parsed:
                return parsed

    for text in text_candidates:
        for pattern in INLINE_DATE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed
    return ""


def build_text_windows(text: str, known_isins: set[str]) -> dict[str, list[str]]:
    matches: list[tuple[int, str]] = []
    for isin in known_isins:
        for match in re.finditer(re.escape(isin), text, flags=re.IGNORECASE):
            matches.append((match.start(), isin))

    matches.sort()
    windows: dict[str, list[str]] = {}
    for index, (start, isin) in enumerate(matches):
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        window_start = max(0, start - 800)
        window_end = min(len(text), next_start + 800)
        windows.setdefault(isin, []).append(text[window_start:window_end])
    return windows


def resolve_table_headers(cells: list[str]) -> dict[str, int]:
    header_indexes: dict[str, int] = {}
    for index, cell in enumerate(cells):
        normalized = normalize_phrase(cell)
        if "isin" in normalized and "isin" not in header_indexes:
            header_indexes["isin"] = index
        if matches_header_variant(normalized, DISCLOSURE_LABEL_KEYS["aum_ccy"]) and "aum_ccy" not in header_indexes:
            header_indexes["aum_ccy"] = index
        if matches_header_variant(normalized, DISCLOSURE_LABEL_KEYS["nav_per_share"]) and "nav_per_share" not in header_indexes:
            header_indexes["nav_per_share"] = index
        if matches_header_variant(normalized, DISCLOSURE_LABEL_KEYS["units_outstanding"]) and "units_outstanding" not in header_indexes:
            header_indexes["units_outstanding"] = index
        if matches_header_variant(normalized, DISCLOSURE_LABEL_KEYS["aum_raw"]) and "aum_raw" not in header_indexes:
            header_indexes["aum_raw"] = index
        if normalized in {"date", "as at", "as of"} or "valuation date" in normalized:
            header_indexes.setdefault("date", index)
        if "local currency" in normalized and "aum_ccy" not in header_indexes:
            header_indexes["aum_ccy"] = index
        if "nav per share" in normalized and "nav_per_share" not in header_indexes:
            header_indexes["nav_per_share"] = index
        if "units outstanding" in normalized and "units_outstanding" not in header_indexes:
            header_indexes["units_outstanding"] = index
        if "shareholder equity" in normalized and "aum_raw" not in header_indexes:
            header_indexes["aum_raw"] = index
        if "currency" == normalized and "aum_ccy" not in header_indexes:
            header_indexes["aum_ccy"] = index
        if "date" in normalized and "date" not in header_indexes:
            header_indexes["date"] = index
    return header_indexes


def parse_disclosure_tables(html: str, known_isins: set[str]) -> dict[str, list[dict[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")
    parsed_rows: dict[str, list[dict[str, str]]] = {}

    for table in soup.find_all("table"):
        table_rows: list[list[str]] = []
        for row in table.find_all("tr"):
            cells = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in row.find_all(["th", "td"])
            ]
            if any(cells):
                table_rows.append(cells)

        if len(table_rows) < 2:
            continue

        for row_index, header_row in enumerate(table_rows[:-1]):
            header_indexes = resolve_table_headers(header_row)
            if "isin" not in header_indexes:
                continue
            if not any(key in header_indexes for key in ("aum_raw", "nav_per_share", "units_outstanding")):
                continue

            for data_row in table_rows[row_index + 1 :]:
                isin_index = header_indexes["isin"]
                if isin_index >= len(data_row):
                    continue
                isin = normalize_isin(data_row[isin_index])
                if not isin or (known_isins and isin not in known_isins):
                    continue

                record = {
                    "ISIN": isin,
                    "aum_raw": clean_text(data_row[header_indexes["aum_raw"]])
                    if "aum_raw" in header_indexes and header_indexes["aum_raw"] < len(data_row)
                    else "",
                    "nav_per_share": clean_text(data_row[header_indexes["nav_per_share"]])
                    if "nav_per_share" in header_indexes and header_indexes["nav_per_share"] < len(data_row)
                    else "",
                    "units_outstanding": clean_text(data_row[header_indexes["units_outstanding"]])
                    if "units_outstanding" in header_indexes and header_indexes["units_outstanding"] < len(data_row)
                    else "",
                    "aum_ccy": clean_text(data_row[header_indexes["aum_ccy"]])
                    if "aum_ccy" in header_indexes and header_indexes["aum_ccy"] < len(data_row)
                    else "",
                    "date": clean_text(data_row[header_indexes["date"]])
                    if "date" in header_indexes and header_indexes["date"] < len(data_row)
                    else "",
                }
                record["AUM(M)"] = first_nonempty(
                    parse_money_to_millions(record["aum_raw"]),
                    compute_aum_from_nav(record["units_outstanding"], record["nav_per_share"]),
                )
                append_disclosure_record(parsed_rows, record)

    return parsed_rows


def parse_disclosure_text_windows(
    text_candidates: list[str],
    known_isins: set[str],
    article_date: str,
) -> dict[str, list[dict[str, str]]]:
    parsed_rows: dict[str, list[dict[str, str]]] = {}

    for text in text_candidates:
        windows = build_text_windows(text, known_isins)
        for isin, segments in windows.items():
            for segment in segments:
                record = {
                    "ISIN": isin,
                    "aum_raw": sanitize_numeric_capture(first_nonempty(
                        extract_inline_value(segment, INLINE_AUM_PATTERNS),
                        extract_labelled_line_value(segment, DISCLOSURE_LABEL_KEYS["aum_raw"]),
                    )),
                    "nav_per_share": sanitize_numeric_capture(first_nonempty(
                        extract_inline_value(segment, INLINE_NAV_PATTERNS),
                        extract_labelled_line_value(segment, DISCLOSURE_LABEL_KEYS["nav_per_share"]),
                    )),
                    "units_outstanding": sanitize_numeric_capture(first_nonempty(
                        extract_inline_value(segment, INLINE_UNITS_PATTERNS),
                        extract_labelled_line_value(segment, DISCLOSURE_LABEL_KEYS["units_outstanding"]),
                    )),
                    "aum_ccy": sanitize_currency_capture(first_nonempty(
                        extract_inline_value(segment, INLINE_CCY_PATTERNS),
                        extract_labelled_line_value(segment, DISCLOSURE_LABEL_KEYS["aum_ccy"]),
                    )),
                    "date": article_date,
                }
                record["AUM(M)"] = first_nonempty(
                    parse_money_to_millions(record["aum_raw"]),
                    compute_aum_from_nav(record["units_outstanding"], record["nav_per_share"]),
                )
                append_disclosure_record(parsed_rows, record)

    return parsed_rows


def fetch_disclosure_rows(known_isins: set[str]) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    if not known_isins:
        return {}, []

    session = build_requests_session()
    url_candidates = build_disclosure_url_candidates(session)
    discovered_rows: dict[str, list[dict[str, str]]] = {}
    successful_urls: list[str] = []

    for url in url_candidates:
        try:
            response = session_get(
                session,
                url,
                logger=logging.getLogger(__name__),
                timeout=REQUEST_TIMEOUT_S,
            )
            response.raise_for_status()
        except Exception as exc:
            print(f"WARNING: Could not fetch Nordea disclosure page {url}: {exc}")
            continue

        html = response.text
        text_candidates = extract_article_text_candidates(html)
        article_date = extract_article_date(html, text_candidates)
        table_rows = parse_disclosure_tables(html, known_isins)
        text_rows = parse_disclosure_text_windows(text_candidates, known_isins, article_date)

        merged_rows: dict[str, list[dict[str, str]]] = {}
        for isin in known_isins:
            candidates = [
                *table_rows.get(isin, []),
                *text_rows.get(isin, []),
            ]
            if not candidates:
                continue
            for candidate in candidates:
                candidate["source_url"] = url
                append_disclosure_record(discovered_rows, candidate)
                append_disclosure_record(merged_rows, candidate)

        if merged_rows:
            successful_urls.append(url)

        covered_isins = {
            isin
            for isin, rows in discovered_rows.items()
            if any(clean_text(row.get("AUM(M)")) for row in rows)
        }
        if covered_isins >= known_isins:
            break

    return discovered_rows, successful_urls


def capture_nordea_payloads_via_playwright() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dataset_rows: list[dict[str, Any]] = []
    detailed_by_isin: dict[str, dict[str, Any]] = {}
    target_isins: set[str] = set()

    def handle_response(response: Any) -> None:
        nonlocal dataset_rows, detailed_by_isin
        try:
            payload = response.json()
        except Exception:
            return

        if "dataset/execute" in response.url:
            results = parse_export_data_response(payload)
            if results:
                dataset_rows = results
            return

        if not response.url.endswith("/fund/searchEntity"):
            return

        values = payload.get("values")
        if not isinstance(values, list):
            return

        for record in values:
            if not isinstance(record, dict):
                continue
            isin = normalize_isin(get_property_value(record, "shareclass_isin"))
            if not isin:
                continue
            if target_isins and isin not in target_isins:
                continue
            detailed_by_isin[isin] = record

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
            timezone_id="Europe/London",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.on("response", handle_response)

        page.goto(FUNDS_SHARE_CLASSES_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        for _ in range(24):
            if dataset_rows:
                break
            page.wait_for_timeout(500)
        if not dataset_rows:
            raise RuntimeError("Nordea dataset response was not captured from the official fund centre.")

        etf_rows = filter_etf_dataset_rows(dataset_rows)
        if not etf_rows:
            return [], []

        target_isins = {normalize_isin(row.get("isin")) for row in etf_rows if normalize_isin(row.get("isin"))}
        detailed_by_isin = {}
        detail_url = build_fund_detail_url([clean_text(row.get("fund")) for row in etf_rows])
        page.goto(detail_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        for _ in range(24):
            if len(detailed_by_isin) >= len(target_isins):
                break
            page.wait_for_timeout(500)

        context.close()
        browser.close()

    detail_rows = [detailed_by_isin[isin] for isin in sorted(detailed_by_isin)]
    return etf_rows, detail_rows


def discover_official_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dataset_rows = fetch_dataset_rows_via_requests()
    if dataset_rows:
        print("INFO: Nordea dataset API responded directly via requests.")
        etf_rows = filter_etf_dataset_rows(dataset_rows)
        return etf_rows, []

    print("INFO: Nordea dataset API requires browser-backed session; using Playwright fallback.")
    return capture_nordea_payloads_via_playwright()


def build_detail_map(detail_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    detail_map: dict[str, dict[str, Any]] = {}
    for record in detail_rows:
        isin = normalize_isin(get_property_value(record, "shareclass_isin"))
        if isin:
            detail_map[isin] = record
    return detail_map


def build_output_row(
    dataset_row: dict[str, Any],
    detail_record: dict[str, Any] | None,
    disclosure_records: list[dict[str, str]] | None,
    today: datetime,
) -> dict[str, str]:
    detail_record = detail_record or {}
    fund_name = clean_text(dataset_row.get("fund"))
    share_class_name = clean_text(get_property_value(detail_record, "shareclass_name"))
    ter_source = dataset_row.get("ter")
    if ter_source in (None, "", "-", "--"):
        ter_source = dataset_row.get("ocf")

    dataset_aum_values = collect_mapping_values(
        dataset_row,
        exact_keys=(
            "aum",
            "netAssets",
            "netassets",
            "fundSize",
            "assetsUnderManagement",
            "shareholderEquity",
            "shareholderEquityBase",
        ),
        key_fragments=("aum", "netasset", "fundsize", "assetsundermanagement", "shareholderequity"),
    )
    detail_aum_values = collect_detail_property_values(
        detail_record,
        exact_keys=(
            "fund_size",
            "net_assets",
            "shareclass_net_assets",
            "shareclass_fund_size",
            "assets_under_management",
            "shareholder_equity",
            "shareholder_equity_base",
        ),
        key_fragments=("aum", "netasset", "fundsize", "assetsundermanagement", "shareholderequity"),
    )
    detail_ccy_values = collect_detail_property_values(
        detail_record,
        exact_keys=("shareclass_currency", "local_currency", "base_currency"),
        key_fragments=("currency",),
    )
    dataset_ccy_values = collect_mapping_values(
        dataset_row,
        exact_keys=("shareClassCurrency", "currency", "baseCurrency", "localCurrency"),
        key_fragments=("currency",),
    )
    fund_ccy = first_nonempty(
        parse_first_currency(detail_ccy_values),
        parse_first_currency(dataset_ccy_values),
    )
    disclosure_record = select_disclosure_record(disclosure_records, fund_ccy)

    date_value = (
        parse_first_date(
            [
                disclosure_record.get("date"),
                dataset_row.get("priceDate"),
                dataset_row.get("terDate"),
                get_property_value(detail_record, "shareclass_launch_date"),
            ]
        )
        or parse_date(dataset_row.get("priceDate"))
        or parse_date(dataset_row.get("terDate"))
        or parse_date(get_property_value(detail_record, "shareclass_launch_date"))
        or today.strftime("%d/%m/%Y")
    )

    ccy = first_nonempty(
        fund_ccy,
        extract_currency_code(disclosure_record.get("aum_ccy")),
    )
    aum_m = first_nonempty(
        clean_text(disclosure_record.get("AUM(M)")),
        parse_first_money_to_millions(dataset_aum_values),
        parse_first_money_to_millions(detail_aum_values),
        compute_aum_from_nav(
            disclosure_record.get("units_outstanding"),
            disclosure_record.get("nav_per_share"),
        ),
    )
    aum_ccy = first_nonempty(
        ccy,
        extract_currency_code(disclosure_record.get("aum_ccy")),
    )

    row = {
        "ETF Name": share_class_name or fund_name,
        "Issuer": ISSUER,
        "ISIN": normalize_isin(dataset_row.get("isin"))
        or normalize_isin(get_property_value(detail_record, "shareclass_isin")),
        "CCY": ccy,
        "TER(bps)": parse_percentage_to_bps(ter_source),
        "AUM(M)": clean_text(aum_m),
        "AUM CCY": clean_text(aum_ccy),
        "Date": date_value,
    }
    if disclosure_record:
        row["AUM Raw"] = clean_text(disclosure_record.get("aum_raw"))
        row["NAV per Share"] = clean_text(disclosure_record.get("nav_per_share"))
        row["Units Outstanding"] = clean_text(disclosure_record.get("units_outstanding"))
        row["AUM Source URL"] = clean_text(disclosure_record.get("source_url"))
    return row


def write_json(output_path: Path, payload: object) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def scrape_nordea() -> Path:
    now = datetime.now()
    output_path = build_output_path(now)
    dataset_etf_rows, detail_rows = discover_official_rows()
    detail_map = build_detail_map(detail_rows)
    known_isins = {
        normalize_isin(row.get("isin"))
        for row in dataset_etf_rows
        if normalize_isin(row.get("isin"))
    }
    disclosure_map, disclosure_urls = fetch_disclosure_rows(known_isins)

    discovered_fund_pages = len({clean_text(row.get("fund")) for row in dataset_etf_rows if clean_text(row.get("fund"))})
    print(f"INFO: Discovered {discovered_fund_pages} official Nordea ETF fund page(s).")
    print(f"INFO: Matched Nordea disclosure rows for {len(disclosure_map)} ETF(s).")

    rows: list[dict[str, str]] = []
    missing_counts: Counter[str] = Counter()
    for dataset_row in dataset_etf_rows:
        isin = normalize_isin(dataset_row.get("isin"))
        row = build_output_row(dataset_row, detail_map.get(isin), disclosure_map.get(isin), now)
        rows.append(row)

        for column in OUTPUT_COLUMNS:
            if row.get(column):
                continue
            missing_counts[column] += 1
            print(
                f"WARNING: Nordea official source does not expose {column} for "
                f"{row.get('ETF Name') or isin or 'unknown ETF'}; leaving blank."
            )

    rows.sort(key=lambda row: row["ISIN"])
    write_json(
        output_path,
        {
            "captured_at": now.isoformat(),
            "provider": ISSUER,
            "discovered_fund_pages": discovered_fund_pages,
            "disclosure_source_urls": disclosure_urls,
            "listing_rows": rows,
        },
    )

    print(f"INFO: Extracted {len(rows)} Nordea ETF row(s).")
    for column in OUTPUT_COLUMNS:
        print(f"INFO: Missing {column:<9}: {missing_counts[column]}")

    print(f"Discovered fund pages : {discovered_fund_pages}")
    print(f"Extracted ETF rows    : {len(rows)}")
    print(f"Output file           : {output_path}")
    print("Missing field counts  :")
    for column in OUTPUT_COLUMNS:
        print(f"  {column}: {missing_counts[column]}")

    return output_path


def main() -> None:
    try:
        scrape_nordea()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Timed out while waiting for Nordea's official fund centre.") from exc


if __name__ == "__main__":
    main()
