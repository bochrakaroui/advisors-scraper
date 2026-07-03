"""Shared ETF output schema and AUM currency helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping


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

SPACE_PATTERN = re.compile(r"\s+")
HEADER_NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")
ISO_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")
EMBEDDED_CURRENCY_RE = re.compile(r"(?<![A-Z])([A-Z]{3})(?![A-Z])")

KNOWN_CURRENCY_CODES = {
    "AED",
    "AUD",
    "BRL",
    "CAD",
    "CHF",
    "CNH",
    "CNY",
    "CZK",
    "DKK",
    "EUR",
    "GBP",
    "GBX",
    "HKD",
    "HUF",
    "ILS",
    "INR",
    "JPY",
    "KRW",
    "KWD",
    "KZT",
    "MXN",
    "NOK",
    "NZD",
    "PLN",
    "QAR",
    "RUB",
    "SAR",
    "SEK",
    "SGD",
    "TRY",
    "TWD",
    "USD",
    "ZAR",
}

CURRENCY_NAME_MAP = {
    "US DOLLAR": "USD",
    "EURO": "EUR",
    "POUND STERLING": "GBP",
    "BRITISH POUND": "GBP",
    "SWISS FRANC": "CHF",
    "JAPANESE YEN": "JPY",
    "CANADIAN DOLLAR": "CAD",
    "AUSTRALIAN DOLLAR": "AUD",
}

CURRENCY_SYMBOL_MAP = {
    "€": "EUR",
    "£": "GBP",
    "$": "USD",
    "¥": "JPY",
}

DIRECT_AUM_CURRENCY_KEYS = {
    "aumcurrency",
    "aumccy",
    "assetscurrency",
    "netassetscurrency",
    "fundsizecurrency",
    "totalnetassetscurrency",
}

DIRECT_AUM_VALUE_KEYS = {
    "aum",
    "aumraw",
    "assetundermanagement",
    "assetsundermanagement",
    "fundsize",
    "fundsizeraw",
    "netassets",
    "netassetsraw",
    "totalfundassets",
    "totalfundassetsraw",
    "totalnetassets",
    "totalnetassetsdaily",
}

FUND_CURRENCY_KEYS = {
    "basecurrency",
    "currency",
    "currencycode",
    "fundbasecurrency",
    "fundcurrency",
    "fundcurrencycode",
    "fundvaluationcurrency",
    "navcurrency",
    "pricecurrency",
    "shareclasscurrency",
    "shareclasscurrencycode",
}

ISIN_KEYS = {
    "isin",
    "isincode",
    "shareclassisin",
    "identifier",
}


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -", "None", "null", "nan", "NaN"} else SPACE_PATTERN.sub(" ", cleaned)


def canonicalize_header(value: object | None) -> str:
    return HEADER_NORMALIZE_PATTERN.sub("", clean_text(value).lower())


def normalize_currency_code(value: object | None) -> str:
    cleaned = clean_text(value).upper()
    if not cleaned:
        return ""

    if cleaned in KNOWN_CURRENCY_CODES:
        return cleaned
    if cleaned in CURRENCY_NAME_MAP:
        return CURRENCY_NAME_MAP[cleaned]
    if cleaned in CURRENCY_SYMBOL_MAP:
        return CURRENCY_SYMBOL_MAP[cleaned]

    compact = cleaned.replace("US$", "$").replace("A$", "$").replace("C$", "$")
    if compact in CURRENCY_SYMBOL_MAP:
        return CURRENCY_SYMBOL_MAP[compact]

    match = ISO_CURRENCY_RE.search(cleaned)
    if match and match.group(1) in KNOWN_CURRENCY_CODES:
        return match.group(1)

    return ""


def extract_currency_from_text(value: object | None) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""

    code = normalize_currency_code(cleaned)
    if code:
        return code

    for symbol, currency_code in CURRENCY_SYMBOL_MAP.items():
        if symbol in cleaned:
            return currency_code

    upper_cleaned = cleaned.upper()
    separated_cleaned = re.sub(r"[^A-Z]+", " ", upper_cleaned)
    embedded_match = EMBEDDED_CURRENCY_RE.search(separated_cleaned)
    if embedded_match and embedded_match.group(1) in KNOWN_CURRENCY_CODES:
        return embedded_match.group(1)

    compact_cleaned = re.sub(r"[^A-Z]", "", upper_cleaned)
    if any(keyword in compact_cleaned for keyword in ("AUM", "NETASSET", "FUNDSIZE", "ASSETSUNDERMANAGEMENT")):
        for currency_code in KNOWN_CURRENCY_CODES:
            if compact_cleaned.endswith(currency_code):
                return currency_code

    for name, currency_code in CURRENCY_NAME_MAP.items():
        if name in upper_cleaned:
            return currency_code

    return ""


def infer_aum_currency_from_row(
    source_row: Mapping[str, Any] | None,
    *,
    allow_fund_currency_fallback: bool = True,
) -> str:
    if not isinstance(source_row, Mapping):
        return ""

    normalized_items = [(str(key), canonicalize_header(key), value) for key, value in source_row.items()]

    for _original_key, normalized_key, value in normalized_items:
        if normalized_key in DIRECT_AUM_CURRENCY_KEYS:
            currency_code = normalize_currency_code(value)
            if currency_code:
                return currency_code

    for original_key, normalized_key, value in normalized_items:
        if (
            normalized_key in DIRECT_AUM_VALUE_KEYS
            or "aum" in normalized_key
            or "netasset" in normalized_key
            or "fundsize" in normalized_key
            or "assetsundermanagement" in normalized_key
        ):
            currency_code = extract_currency_from_text(original_key)
            if currency_code:
                return currency_code
            currency_code = extract_currency_from_text(value)
            if currency_code:
                return currency_code

    if allow_fund_currency_fallback:
        for _original_key, normalized_key, value in normalized_items:
            if normalized_key in FUND_CURRENCY_KEYS:
                currency_code = normalize_currency_code(value)
                if currency_code:
                    return currency_code

    return ""


def extract_row_isin(source_row: Mapping[str, Any] | None) -> str:
    if not isinstance(source_row, Mapping):
        return ""

    for key, value in source_row.items():
        if canonicalize_header(key) in ISIN_KEYS:
            return clean_text(value).upper().replace(" ", "")
    return ""


def infer_consistent_row_currency(source_rows: list[Mapping[str, Any]]) -> str:
    seen_currencies: set[str] = set()

    for source_row in source_rows:
        if not isinstance(source_row, Mapping):
            continue

        for key, value in source_row.items():
            normalized_key = canonicalize_header(key)
            if normalized_key in {"ccy", "currencycode", "pricecurrency", "shareclasscurrencycode", "shareclasscurrency"}:
                currency_code = normalize_currency_code(value)
                if currency_code:
                    seen_currencies.add(currency_code)

    return seen_currencies.pop() if len(seen_currencies) == 1 else ""
