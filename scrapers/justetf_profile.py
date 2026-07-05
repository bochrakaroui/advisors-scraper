from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import requests
from bs4 import BeautifulSoup


PROFILE_URL_TEMPLATE = "https://www.justetf.com/en/etf-profile.html?isin={isin}"
REQUEST_TIMEOUT_S = 60
SPACE_RE = re.compile(r"\s+")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
FUND_SIZE_RE = re.compile(r"\b([A-Z]{3})\s+([0-9][0-9., ]*)\s*(m|bn|b)\b", re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SKIP_LINE_NORMALIZED = {
    "",
    "|",
    "compare",
    "watchlist",
    "portfolio",
    "overview",
    "chart",
    "basics",
    "performance",
    "risk",
    "stock exchange",
    "savings plan",
    "order fees",
    "dividends",
    "quote",
    "data",
}

KNOWN_LABELS = {
    "isin",
    "ticker",
    "wkn",
    "ter",
    "distribution policy",
    "distribution frequency",
    "replication",
    "fund size",
    "inception date",
    "inception/ listing date",
    "index",
    "investment focus",
    "total expense ratio",
    "legal structure",
    "investment approach",
    "sustainability",
    "fund currency",
    "currency risk",
    "volatility 1 year (in eur)",
    "fund domicile",
    "fund provider",
    "fund structure",
    "ucits compliance",
    "administrator",
    "investment advisor",
    "custodian bank",
}


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = (
        str(value)
        .replace("\u00ad", "")
        .replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("\u2009", " ")
        .strip()
    )
    cleaned = SPACE_RE.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "None", "null", "N/A"} else cleaned


def normalize_isin(value: object | None) -> str:
    cleaned = clean_text(value).upper().replace(" ", "")
    return cleaned if ISIN_RE.fullmatch(cleaned) else ""


def parse_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"(?i)p\.?\s*a\.?", "", cleaned)
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


def parse_percent_to_bps(value: object | None) -> str:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value * Decimal("100"), places=2)


def parse_fund_size(value: object | None) -> tuple[str, str]:
    cleaned = clean_text(value)
    if not cleaned:
        return "", ""
    match = FUND_SIZE_RE.search(cleaned)
    if not match:
        return "", ""
    currency = clean_text(match.group(1)).upper()
    amount = parse_decimal(match.group(2))
    if amount is None:
        return "", currency
    unit = clean_text(match.group(3)).lower()
    if unit in {"b", "bn"}:
        amount *= Decimal("1000")
    return format_decimal(amount, places=2), currency


def parse_date(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in (
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def match_group(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return clean_text(match.group(1)) if match else ""


def extract_lines(soup: BeautifulSoup) -> list[str]:
    container = soup.find("main") or soup.body or soup
    lines = [clean_text(text) for text in container.stripped_strings]
    return [line for line in lines if line]


def find_label_value(lines: list[str], label: str, *, max_lookahead: int = 6) -> str:
    target = clean_text(label).casefold()
    for index, line in enumerate(lines):
        normalized = line.casefold()
        if normalized == target:
            for candidate in lines[index + 1 : index + 1 + max_lookahead]:
                candidate_normalized = candidate.casefold()
                if candidate_normalized in SKIP_LINE_NORMALIZED:
                    continue
                if candidate_normalized == target:
                    continue
                if candidate_normalized in KNOWN_LABELS:
                    return ""
                return candidate
        if normalized.startswith(target + " "):
            remainder = clean_text(line[len(label) :])
            if remainder:
                return remainder
    return ""


def fetch_profile(
    isin: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    normalized_isin = normalize_isin(isin) or clean_text(isin).upper().replace(" ", "")
    profile_url = PROFILE_URL_TEMPLATE.format(isin=normalized_isin)
    active_session = session or build_session()
    response = active_session.get(profile_url, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_title = clean_text(soup.title.string if soup.title else "")
    heading = clean_text((soup.find("h1") or soup.find("title")).get_text(" ", strip=True) if (soup.find("h1") or soup.find("title")) else "")
    lines = extract_lines(soup)
    page_text = " ".join(lines)

    parsed_isin = normalize_isin(find_label_value(lines, "ISIN")) or normalize_isin(normalized_isin)
    if parsed_isin != normalized_isin and normalized_isin not in page_text:
        return {
            "profile_url": profile_url,
            "page_title": page_title,
            "etf_name": heading,
            "isin": normalized_isin,
            "fetch_status": "not_found",
            "error": f"Requested ISIN {normalized_isin} did not resolve to a matching justETF profile.",
        }

    fund_size_raw = match_group(page_text, r"\bFund size\s+([A-Z]{3}\s+[0-9][0-9., ]*\s*(?:m|bn|b))\b") or find_label_value(lines, "Fund size")
    aum_mn, aum_ccy = parse_fund_size(fund_size_raw)
    ter_raw = (
        match_group(page_text, r"\bTER\s+([0-9]+(?:[.,][0-9]+)?%\s*p\.a\.)")
        or match_group(page_text, r"\bTotal expense ratio\s+([0-9]+(?:[.,][0-9]+)?%\s*p\.a\.)")
        or match_group(page_text, r"\bamounts to\s+([0-9]+(?:[.,][0-9]+)?%\s*p\.a\.)")
        or find_label_value(lines, "TER")
        or find_label_value(lines, "Total expense ratio")
    )
    inception_raw = (
        match_group(page_text, r"\bInception(?:/\s*Listing)? Date\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})")
        or find_label_value(lines, "Inception Date")
        or find_label_value(lines, "Inception/ Listing Date")
    )
    distribution_frequency = find_label_value(lines, "Distribution frequency")

    return {
        "profile_url": profile_url,
        "page_title": page_title,
        "etf_name": heading,
        "isin": parsed_isin or normalized_isin,
        "ticker": match_group(page_text, r"\bTicker\s+([A-Z0-9.-]+)\b") or find_label_value(lines, "Ticker"),
        "wkn": find_label_value(lines, "WKN"),
        "ter_raw": ter_raw,
        "ter_bps": parse_percent_to_bps(ter_raw),
        "distribution_policy": match_group(page_text, r"\bDistribution policy\s+(Accumulating|Distributing)\b") or find_label_value(lines, "Distribution policy"),
        "distribution_frequency": distribution_frequency,
        "replication": match_group(page_text, r"\bReplication\s+([A-Za-z]+(?:\s*\([^)]+\))?)\b") or find_label_value(lines, "Replication"),
        "fund_size_raw": fund_size_raw,
        "aum_mn": aum_mn,
        "aum_ccy": aum_ccy,
        "inception_date_raw": inception_raw,
        "inception_date": parse_date(inception_raw),
        "ccy": (match_group(page_text, r"\bFund currency\s+([A-Z]{3})\b") or find_label_value(lines, "Fund currency")).upper(),
        "currency_risk": match_group(page_text, r"\bCurrency risk\s+([A-Za-z ]+)\b") or find_label_value(lines, "Currency risk"),
        "fund_domicile": find_label_value(lines, "Fund domicile"),
        "fund_provider": find_label_value(lines, "Fund Provider"),
        "index_name": find_label_value(lines, "Index"),
        "investment_focus": find_label_value(lines, "Investment focus"),
        "investment_approach": find_label_value(lines, "Investment approach"),
        "fund_structure": match_group(page_text, r"\bFund Structure\s+([A-Za-z0-9 ,.&()'/-]+?)\s+UCITS compliance\b") or find_label_value(lines, "Fund Structure"),
        "ucits_compliance": match_group(page_text, r"\bUCITS compliance\s+(Yes|No)\b") or find_label_value(lines, "UCITS compliance"),
        "administrator": find_label_value(lines, "Administrator"),
        "investment_advisor": find_label_value(lines, "Investment Advisor"),
        "custodian_bank": find_label_value(lines, "Custodian Bank"),
        "fetch_status": "ok",
    }
