"""Download Schroders Fund Centre ETF data for a fixed set of target ISINs.

Strategy
--------
The Schroders Fund Centre (https://www.schroders.com/.../fund-centre/#/fund/search/filter)
is an Angular SPA with hash-based routing. There is no documented public API, so this
scraper, like globalx_extractor.py, takes a dual approach per ISIN:

  1. API interception — listen for any XHR/fetch JSON response fired while the search
     page loads/filters and pull out any record whose ISIN matches a target.
  2. DOM fallback — if no matching JSON is intercepted, search for the ISIN via the
     on-page search box, open the matching fund's detail page, and parse the
     label/value pairs ("ISIN", "Base currency", "Ongoing charge", "Launch date", ...)
     straight out of the rendered text.

IMPORTANT — selectors are best-effort
--------------------------------------
schroders.com was not reachable from the sandbox this script was written in (it isn't on
the environment's network allow-list), so the CSS/role selectors below could not be
verified against the live DOM and are inferred from how the site behaves in general
(OneTrust cookie banner, a search input on the filter page, a results list of fund
links). Run this once with `headless=False` (see `main()`) and watch it work; if a
selector misses, the script prints which step failed and falls back gracefully, but you
may need to adjust `SEARCH_INPUT_SELECTORS` / `RESULT_LINK_SELECTORS` after inspecting
the real page in devtools.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Response, async_playwright

try:
    from scrapers.tls_compat import browser_launch_args, context_https_kwargs
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from tls_compat import browser_launch_args, context_https_kwargs

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_FUND_CENTRE_URL = "https://www.schroders.com/en-gb/uk/institutional/fund-centre/"
SEARCH_URL = BASE_FUND_CENTRE_URL + "#/fund/search/filter"
ISSUER   = "Schroders"
PROVIDER = "Schroders"

# The ETFs we care about — everything else returned by the site is ignored.
TARGET_ISINS = [
    "IE0003OZJ573",
    "IE000AVUROO8",
    "IE000BNLRWE6",
    "IE000FGFJT15",
    "IE000GML9HQ4",
]

BASE_DIR   = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Schroders"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
TIMEOUT_MS = 60_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
DATE_RE = re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
FUND_URL_HINTS = ("fund", "search", "filter", "etf", "product")

# Best-effort selectors — see module docstring. Tried in order, first match wins.
# NOTE: Schroders stacks at least two separate overlays on first load — a cookie
# consent banner AND a professional/institutional investor declaration gate.
# OVERLAY_BUTTON_TEXTS lists affirmative-action words for both; the buster loop
# (see dismiss_all_overlays) keeps clicking until no dialog/mask is left, rather
# than assuming there's exactly one banner.
OVERLAY_BUTTON_TEXTS = [
    "Accept all cookies", "Accept All", "Accept all", "Accept",
    "I Accept", "I agree", "Agree", "Allow all", "Allow All",
    "Continue", "Confirm", "Submit", "OK", "Got it",
    "I am a professional investor", "Professional investor",
    "Institutional", "Yes, continue", "Enter site", "Proceed",
]
OVERLAY_BUTTON_SELECTORS = (
    ["#onetrust-accept-btn-handler"]
    + [f"button:has-text('{t}')" for t in OVERLAY_BUTTON_TEXTS]
    + ["[id*='accept' i]", "button[aria-label*='Accept' i]"]
)
# Generic dialog/mask containers, tried loosely since styled-components hash
# suffixes (e.g. "ModalLayoutstyled__ModalBGMask-sc-...") can change between
# deploys.
OVERLAY_CONTAINER_SELECTORS = [
    "[role='dialog']",
    "[aria-modal='true']",
    "[class*='ModalBGMask']",
    "[class*='Modal'][class*='Mask']",
    "[class*='cookie' i][class*='banner' i]",
    "[class*='consent' i]",
]
SEARCH_INPUT_SELECTORS = [
    "input[type='search']",
    "input[placeholder*='Search' i]",
    "input[aria-label*='Search' i]",
    "[role='searchbox']",
]
RESULT_LINK_SELECTORS = [
    "a[href*='/fund/']",
    "[class*='result'] a",
    "[class*='fund-list'] a",
    "table a",
]

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def build_run_output_dir(base: Path, run_date: str) -> Path:
    name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if name:
        d = base / name
    else:
        d = base / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = d.name
    d.mkdir(parents=True, exist_ok=True)
    return d

def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "schroders_etf_export.json"

# ---------------------------------------------------------------------------
# Normalisation (kept self-contained / mirrors globalx_extractor.py conventions)
# ---------------------------------------------------------------------------
def clean(v: Any) -> str:
    if v is None: return ""
    s = re.sub(r"\s+", " ", str(v).replace("\u00ad", "").replace("\u00a0", " ").replace("Â", "").strip())
    return "" if s in {"", "-", "--", "- ", " -", "None"} else s

def fmt_dec(v: Decimal, places: int = 2) -> str:
    return format(v.quantize(Decimal("1." + "0" * places), rounding=ROUND_HALF_UP), f".{places}f")

def ter_bps(raw: str) -> str:
    s = re.sub(r"[^0-9,.\-]", "", clean(raw).replace("%", ""))
    if not s: return ""
    if "," in s and "." not in s: s = s.replace(",", ".")
    try: return fmt_dec(Decimal(s) * 100)
    except InvalidOperation: return ""

def detect_ccy(raw: str) -> str:
    t = clean(raw).upper()
    if "$" in t: return "USD"
    if "€" in t: return "EUR"
    if "£" in t: return "GBP"
    for c in ("USD", "EUR", "GBP", "CHF"):
        if c in t: return c
    return ""

def aum_millions(raw: str) -> tuple[str, str]:
    s = clean(raw)
    if not s: return "", ""
    ccy = detect_ccy(s)
    n = re.sub(r"[$€£,]|USD|EUR|GBP|CHF", "", s).strip()
    mul = Decimal(1)
    u = n.upper()
    if u.endswith("BN") or u.endswith("B"):
        mul = Decimal("1e9"); n = re.sub(r"(BN|B)$", "", n, flags=re.I).strip()
    elif u.endswith("MN") or u.endswith("M"):
        mul = Decimal("1e6"); n = re.sub(r"(MN|M)$", "", n, flags=re.I).strip()
    n = re.sub(r"[^0-9.\-]", "", n)
    if not n: return "", ccy
    try:
        m = Decimal(n) * mul / Decimal("1e6")
        return (format(m, "f").rstrip("0").rstrip(".") or "0"), ccy
    except InvalidOperation:
        return "", ccy

def norm_date(raw: str) -> str:
    s = clean(raw)
    if not s: return ""
    m = DATE_RE.search(s)
    if m: s = m.group(0)
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d/%m/%y"):
        try: return datetime.strptime(s, fmt).strftime("%d/%m/%Y 00:00:00")
        except ValueError: pass
    return s

# ---------------------------------------------------------------------------
# JSON interception helpers
# ---------------------------------------------------------------------------
def _find_isin_records(obj: Any, target_isins: set[str], found: dict[str, dict]) -> None:
    """Recursively walk a parsed JSON body, collecting any dict that contains
    one of our target ISINs under a key that looks like an ISIN field."""
    if isinstance(obj, dict):
        isin_val = ""
        for k, v in obj.items():
            if isinstance(v, str) and k.lower() in {"isin", "primaryisin", "primary_isin", "shareclassisin"}:
                cand = v.strip().upper()
                if cand in target_isins:
                    isin_val = cand
        if isin_val and isin_val not in found:
            found[isin_val] = obj
        for v in obj.values():
            _find_isin_records(v, target_isins, found)
    elif isinstance(obj, list):
        for item in obj:
            _find_isin_records(item, target_isins, found)

def _g(d: dict, *keys: str) -> str:
    for k in keys:
        for c in (k, k.lower(), k.upper(), k[0].upper() + k[1:], k.replace("_", ""), k.replace("-", "")):
            if c in d: return clean(d[c])
    return ""

# Matches a currency-amount-with-magnitude string regardless of what key/label
# it sits under, e.g. "GBP 43.23 m", "USD 1,394.15 m", "EUR 1.2 bn". Used as a
# last-resort fallback once both the named-key lookup (_g) and the labeled
# detail-page parsing have failed to find AUM under any alias we know about —
# rather than keep guessing field/label names one at a time, search for the
# *shape* of the value itself. The trailing magnitude suffix (m/mn/bn) is what
# distinguishes a fund-size figure from a per-share NAV/price, which is never
# expressed in millions/billions.
CURRENCY_AMOUNT_RE = re.compile(r"^([A-Z]{3})\s+([\d,]+(?:\.\d+)?)\s*(m|mn|bn)$", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
AUM_SUFFIX_RE = re.compile(r"\b(?:m|mn|bn|b)\b", re.IGNORECASE)

def _deep_find_amount(obj: Any, exclude: set[str]) -> str:
    """Recursively walk a parsed JSON value looking for any string matching
    CURRENCY_AMOUNT_RE that isn't one of the values already accounted for
    (e.g. the per-share NAV), so a fund-size figure nested under an
    unanticipated key name still gets picked up."""
    if isinstance(obj, str):
        s = clean(obj)
        if s and s not in exclude and CURRENCY_AMOUNT_RE.match(s):
            return s
        return ""
    if isinstance(obj, dict):
        for v in obj.values():
            found = _deep_find_amount(v, exclude)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_amount(item, exclude)
            if found:
                return found
    return ""

def html_to_text(raw: Any) -> str:
    return clean(HTML_TAG_RE.sub(" ", html.unescape(str(raw or ""))))

def normalize_aum_value(label: str, value: Any) -> str:
    text = html_to_text(value)
    if not text:
        return ""
    ll = clean(label).lower()
    if not AUM_SUFFIX_RE.search(text):
        if "million" in ll:
            return f"{text} m"
        if "billion" in ll:
            return f"{text} bn"
    return text

def extract_structured_fields(obj: Any) -> dict[str, str]:
    """Recursively mine the richer page-context JSON for fields that don't
    always show up on the first matched ISIN dict or the default Overview tab."""
    fields: dict[str, str] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            share_class = node.get("shareClass")
            if isinstance(share_class, dict):
                if share_class.get("name") and not fields.get("etf_name"):
                    fields["etf_name"] = clean(share_class.get("name"))
                if share_class.get("baseCurrency") and not fields.get("ccy"):
                    fields["ccy"] = clean(share_class.get("baseCurrency"))
                if share_class.get("isin") and not fields.get("isin_confirm"):
                    fields["isin_confirm"] = clean(share_class.get("isin")).upper()

            if (
                clean(node.get("identifierLabel")).lower() == "isin"
                and node.get("identifier")
                and not fields.get("isin_confirm")
            ):
                fields["isin_confirm"] = clean(node.get("identifier")).upper()
            if (
                clean(node.get("dateLabel")).lower() == "inception"
                and node.get("date")
                and not fields.get("inception")
            ):
                fields["inception"] = clean(node.get("date"))
            if (
                node.get("heading")
                and node.get("identifier")
                and clean(node.get("identifier")).upper().startswith("IE")
                and not fields.get("etf_name")
            ):
                fields["etf_name"] = clean(node.get("heading"))

            if clean(node.get("managerLabel")).lower() == "fund manager" and isinstance(node.get("items"), list):
                names = [
                    clean(item.get("name"))
                    for item in node["items"]
                    if isinstance(item, dict) and clean(item.get("name"))
                ]
                if names and not fields.get("fund_manager"):
                    fields["fund_manager"] = ", ".join(names)

            if "value" in node:
                label = clean(node.get("title") or node.get("label") or node.get("id"))
                key_text = f"{clean(node.get('id'))} {label}".lower()
                value = html_to_text(node.get("value"))
                as_of = clean(node.get("asOf") or node.get("date"))
                if value:
                    if (
                        any(token in key_text for token in ("fundsize", "fund size", "total nav", "net assets"))
                        and not fields.get("net_assets_raw")
                    ):
                        fields["net_assets_raw"] = normalize_aum_value(label, node.get("value"))
                        if as_of:
                            fields["aum_as_of"] = as_of
                    elif ("priceendvalue" in key_text or "unit nav" in key_text) and not fields.get("nav_raw"):
                        fields["nav_raw"] = value
                        if as_of:
                            fields["nav_as_of"] = as_of
                    elif (
                        any(token in key_text for token in ("ongoingcharge", "ongoing charge", "expense ratio", "ocf", " ter"))
                        and not fields.get("ongoing_charges_raw")
                    ):
                        fields["ongoing_charges_raw"] = value

            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(obj)
    return fields

def map_json_row(raw: dict, isin: str, scraped_at: str, payload: Any | None = None) -> dict[str, str]:
    structured = extract_structured_fields(payload) if payload else {}
    ticker  = _g(raw, "ticker", "tickerSymbol", "symbol")
    name    = _g(raw, "name", "fundName", "shareClassName", "displayName", "title") or structured.get("etf_name", "")
    aum_raw = _g(
        raw,
        "fundSize", "fund_size", "netAssets", "net_assets", "aum", "totalNetAssets",
        "totalNav", "total_nav", "navTotal", "totalNetAssetValue",
        "marketValue", "market_value", "totalMarketValue", "fundSizeLocal",
    )
    if not aum_raw:
        aum_raw = structured.get("net_assets_raw", "")
    nav_raw = _g(raw, "nav", "NAV", "navPerShare", "price")
    if not nav_raw:
        nav_raw = structured.get("nav_raw", "")
    if not aum_raw:
        # Named-key lookup found nothing — fall back to scanning the whole
        # matched record for anything shaped like a fund-size figure. This
        # is what actually catches cases where the field is simply absent
        # from the API payload for this share class under any key we'd
        # recognize (confirmed: IE000AVUROO8 still came back blank after
        # adding more key aliases, so the value — if present at all — isn't
        # reachable by key-name guessing alone).
        aum_raw = _deep_find_amount(raw, exclude={nav_raw} if nav_raw else set())
    ter_raw = _g(raw, "ongoingCharge", "ongoing_charge", "ocf", "ter", "TER", "expenseRatio")
    if not ter_raw:
        ter_raw = structured.get("ongoing_charges_raw", "")
    sfdr    = _g(raw, "sfdr", "sfdrClassification", "article")
    inc     = _g(raw, "launchDate", "launch_date", "inceptionDate", "inception") or structured.get("inception", "")
    as_of   = _g(raw, "asOf", "as_of", "navDate", "date", "priceDate") or structured.get("nav_as_of", "")
    ccy     = _g(raw, "baseCurrency", "base_currency", "currency", "ccy") or structured.get("ccy", "")
    aum_m, ccy_from_aum = aum_millions(aum_raw)
    if not ccy:
        ccy = ccy_from_aum
    row = {
        "provider": PROVIDER, "issuer": ISSUER,
        "etf_name": name, "ticker": ticker,
        "isin": isin,
        "net_assets_raw": aum_raw, "aum_numeric": aum_m, "aum_m": aum_m,
        "aum_currency": ccy, "ccy": ccy,
        "nav_raw": nav_raw,
        "as_of_date": as_of, "date": norm_date(as_of),
        "sfdr_classification": sfdr,
        "ongoing_charges_raw": ter_raw, "ter_bps": ter_bps(ter_raw),
        "inception": inc, "inception_norm": norm_date(inc),
        "source_url": SEARCH_URL, "scraped_at": scraped_at,
        "extraction_method": "api_intercept",
    }
    if structured.get("aum_as_of"):
        row["aum_as_of_date"] = structured["aum_as_of"]
    if structured.get("fund_manager"):
        row["fund_manager"] = structured["fund_manager"]
    return row

# ---------------------------------------------------------------------------
# DOM fallback — parses the search-RESULTS-row structure confirmed live:
#
#   SFDR(1)                              <- facet panel: "<Label>(<count>)"
#   Article 8                               followed by the value, then a
#   (1)                                     standalone "(<count>)" line
#   ASSET CLASS(1)
#   Equity
#   (1)
#   ...
#   Name / Unit NAV / Actions            <- table headers
#   Schroder ETFs (ICAV) - Schroder ... UCITS ETF   <- fund name
#   ISIN
#   IE0003OZJ573
#   Share class launch date
#   21.04.2026
#   Share class currency
#   USD
#   USD 10.5973                          <- "<CCY> <NAV>" combined
#   Date
#   29.06.2026                           <- NAV as-of date
#
# IMPORTANT: net assets / fund size / ticker / ongoing charge (TER) do NOT
# appear anywhere on this results view based on the confirmed sample — they
# likely only exist on a deeper per-fund detail page, which
# search_and_open_fund additionally tries to click into (see
# DETAIL_PAGE_LABELS below) so those fields can be filled in when reachable.
# ---------------------------------------------------------------------------
FACET_LINE_RE = re.compile(r"^(.*?)\((\d+)\)$")
COUNT_ONLY_RE = re.compile(r"^\(\d+\)$")

def _parse_facets(lines: list[str]) -> dict[str, str]:
    facets: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = FACET_LINE_RE.match(lines[i])
        if m and i + 1 < len(lines):
            label = m.group(1).strip().lower()
            value = lines[i + 1]
            facets[label] = value
            i += 2
            if i < len(lines) and COUNT_ONLY_RE.match(lines[i]):
                i += 1
            continue
        i += 1
    return facets

def _parse_result_row(lines: list[str], isin: str) -> dict[str, str]:
    """Walk the table header ('... Actions') through to the NAV-date line."""
    row: dict[str, str] = {}
    header_idx = next((i for i, l in enumerate(lines) if l.lower() == "actions"), -1)
    if header_idx == -1 or header_idx + 1 >= len(lines):
        return row

    j = header_idx + 1
    row["etf_name"] = lines[j]; j += 1
    if j < len(lines) and lines[j].lower() == "isin":
        j += 1
        if j < len(lines):
            row["isin"] = lines[j]; j += 1
    if j < len(lines) and "launch date" in lines[j].lower():
        j += 1
        if j < len(lines):
            row["inception"] = lines[j]; j += 1
    if j < len(lines) and "currency" in lines[j].lower():
        j += 1
        if j < len(lines):
            row["ccy"] = lines[j]; j += 1
    if j < len(lines):
        # combined "<CCY> <NAV>" line, e.g. "USD 10.5973"
        m = re.match(r"^([A-Z]{3})\s+([\d.,]+)$", lines[j])
        if m:
            row["ccy"] = row.get("ccy") or m.group(1)
            row["nav_raw"] = lines[j]
        else:
            row["nav_raw"] = lines[j]
        j += 1
    if j < len(lines) and lines[j].lower() == "date":
        j += 1
        if j < len(lines):
            row["as_of_date"] = lines[j]; j += 1
    return row

def parse_dom_fields(text: str, isin: str, scraped_at: str) -> dict[str, str]:
    lines = [clean(l) for l in text.splitlines() if clean(l)]

    facets = _parse_facets(lines)
    row = _parse_result_row(lines, isin)

    sfdr = facets.get("sfdr", "")
    ccy = row.get("ccy", "") or facets.get("currency", "")
    nav_raw = row.get("nav_raw", "")

    return {
        "provider": PROVIDER, "issuer": ISSUER,
        "etf_name": row.get("etf_name", ""), "ticker": "",
        "isin": isin,
        # Net assets / fund size were not present anywhere on the search
        # results view in the confirmed sample — left blank here on purpose
        # rather than guessed. Filled in by merge_detail_fields() if the
        # detail-page click-through succeeds and finds it.
        "net_assets_raw": "", "aum_numeric": "", "aum_m": "",
        "aum_currency": ccy, "ccy": ccy,
        "nav_raw": nav_raw,
        "as_of_date": row.get("as_of_date", ""), "date": norm_date(row.get("as_of_date", "")),
        "sfdr_classification": sfdr,
        "ongoing_charges_raw": "", "ter_bps": "",
        "inception": row.get("inception", ""), "inception_norm": norm_date(row.get("inception", "")),
        "asset_class": facets.get("asset class", ""),
        "distribution_status": facets.get("distribution status", ""),
        "fund_manager": facets.get("fund manager", ""),
        "source_url": SEARCH_URL, "scraped_at": scraped_at,
        "extraction_method": "dom_fallback",
    }

# Anchors confirmed live on the fund detail page's "Overview" section:
#
#   Unit NAV (USD)          <- per-share NAV header, currency in parens
#   USD 10.5973
#   As of 29.06.2026
#   Total NAV (USD)         <- THIS is the AUM/fund-size field — labeled
#   USD 29.31 m                "Total NAV", not "Fund size"/"Net assets"/"AUM"
#   As of 26.06.2026
#   Fund objectives and investment policy
#   ...
#   Fund manager
#   Lukas Kamblevicius
#   Managed fund since: 21.04.2026
#   ...
#   Fees & expenses
#   Ongoing charge
#   0.20%
#
# Near the very top of the page: "ISIN: <isin>" and "Inception: <date>" also
# appear as single "Label: value" lines.
#
# NOTE: confirmed against the USD/standard share classes. The GBP-hedged
# share class of the Global IG Corporate Bond fund (IE000AVUROO8) came back
# from a live scrape with every other detail-page field populated except
# this one, which points at either (a) the label rendering differently for
# that share class ("Net Assets" / "Total Net Assets" / "Fund Size" instead
# of "Total NAV"), or (b) the widget loading asynchronously after the first
# read of body text. Both are now handled: the label match below accepts
# several aliases, and search_and_open_fund() polls body text instead of
# reading it once.
DETAIL_LINE_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z /]+):\s*(.+)$")
TOTAL_NAV_LABEL_PREFIXES = ("total nav", "net assets", "total net assets", "fund size", "fund assets")

def parse_detail_page_fields(text: str) -> dict[str, str]:
    lines = [clean(l) for l in text.splitlines() if clean(l)]
    n = len(lines)
    fields: dict[str, str] = {}

    for i, l in enumerate(lines):
        ll = l.lower()

        if ll.startswith("unit nav") and "nav_raw" not in fields and i + 1 < n:
            fields["nav_raw"] = lines[i + 1]
            if i + 2 < n and lines[i + 2].lower().startswith("as of"):
                fields["nav_as_of"] = lines[i + 2][5:].strip()

        elif any(ll.startswith(p) for p in TOTAL_NAV_LABEL_PREFIXES) and "net_assets_raw" not in fields and i + 1 < n:
            fields["net_assets_raw"] = normalize_aum_value(l, lines[i + 1])
            if i + 2 < n and lines[i + 2].lower().startswith("as of"):
                fields["aum_as_of"] = lines[i + 2][5:].strip()

        elif ll == "ongoing charge" and "ongoing_charges_raw" not in fields and i + 1 < n:
            fields["ongoing_charges_raw"] = lines[i + 1]

        elif ll == "fund manager" and "fund_manager" not in fields and i + 1 < n:
            fields["fund_manager"] = lines[i + 1]

        else:
            m = DETAIL_LINE_LABEL_RE.match(l)
            if m:
                label, value = m.group(1).strip().lower(), m.group(2).strip()
                if label == "isin" and "isin_confirm" not in fields:
                    fields["isin_confirm"] = value
                elif label == "inception" and "inception" not in fields:
                    fields["inception"] = value

    if "net_assets_raw" not in fields:
        # No label we recognize matched anywhere on the page — fall back to
        # scanning every line for the *shape* of a fund-size figure
        # (currency code + amount + m/mn/bn suffix), skipping the per-share
        # NAV line we already captured. Confirmed necessary: even after
        # adding more label aliases, IE000AVUROO8's detail page still didn't
        # match any known label, so the figure (if shown at all) must be
        # under different wording entirely.
        exclude = {fields["nav_raw"]} if "nav_raw" in fields else set()
        for l in lines:
            if l in exclude:
                continue
            if CURRENCY_AMOUNT_RE.match(l):
                fields["net_assets_raw"] = l
                fields["net_assets_source"] = "heuristic_amount_scan"
                break

    return fields

def merge_detail_fields(row: dict[str, str], detail: dict[str, str]) -> dict[str, str]:
    if detail.get("ticker"):
        row["ticker"] = detail["ticker"]
    if detail.get("ongoing_charges_raw"):
        row["ongoing_charges_raw"] = detail["ongoing_charges_raw"]
        row["ter_bps"] = ter_bps(detail["ongoing_charges_raw"])
    if detail.get("net_assets_raw"):
        row["net_assets_raw"] = detail["net_assets_raw"]
        aum_m, ccy_from_aum = aum_millions(detail["net_assets_raw"])
        row["aum_numeric"] = aum_m
        row["aum_m"] = aum_m
        if not row.get("aum_currency"):
            row["aum_currency"] = ccy_from_aum
            row["ccy"] = row["ccy"] or ccy_from_aum
        if detail.get("aum_as_of"):
            row["aum_as_of_date"] = detail["aum_as_of"]
        if detail.get("net_assets_source"):
            # Flags that this value came from the value-shape heuristic scan
            # rather than a recognized "Total NAV"/"Net assets"/etc. label —
            # still real data scraped off the page, just worth a second look
            # since it wasn't found under an expected label.
            row["net_assets_source"] = detail["net_assets_source"]
    if detail.get("nav_raw") and not row.get("nav_raw"):
        row["nav_raw"] = detail["nav_raw"]
    if detail.get("nav_as_of") and not row.get("as_of_date"):
        row["as_of_date"] = detail["nav_as_of"]
        row["date"] = norm_date(detail["nav_as_of"])
    if detail.get("inception") and not row.get("inception"):
        row["inception"] = detail["inception"]
        row["inception_norm"] = norm_date(detail["inception"])
    if detail.get("fund_manager"):
        row["fund_manager"] = detail["fund_manager"]
    return row

def empty_row(isin: str, scraped_at: str, reason: str) -> dict[str, str]:
    return {
        "provider": PROVIDER, "issuer": ISSUER,
        "etf_name": "", "ticker": "", "isin": isin,
        "net_assets_raw": "", "aum_numeric": "", "aum_m": "",
        "aum_currency": "", "ccy": "", "nav_raw": "",
        "as_of_date": "", "date": "",
        "sfdr_classification": "",
        "ongoing_charges_raw": "", "ter_bps": "",
        "inception": "", "inception_norm": "",
        "source_url": SEARCH_URL, "scraped_at": scraped_at,
        "extraction_method": "failed", "failure_reason": reason,
    }

# ---------------------------------------------------------------------------
# Page interaction helpers
# ---------------------------------------------------------------------------
async def dismiss_all_overlays(page: Page, max_rounds: int = 5) -> None:
    """Schroders stacks multiple blocking overlays on first load (cookie
    consent, then a separate professional/institutional investor gate, and
    sometimes a third "manage cookies" panel). Rather than assume there's
    exactly one banner, keep looking for any visible dialog/mask + an
    affirmative button and clicking through until nothing is left, or we
    run out of rounds."""
    for round_num in range(1, max_rounds + 1):
        any_container_visible = False
        for sel in OVERLAY_CONTAINER_SELECTORS:
            try:
                if await page.locator(sel).first.is_visible(timeout=500):
                    any_container_visible = True
                    break
            except Exception:
                continue
        if not any_container_visible:
            if round_num > 1:
                print(f"    All overlays cleared after {round_num - 1} round(s)")
            return

        clicked = False
        for sel in OVERLAY_BUTTON_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click(timeout=3_000)
                    print(f"    [round {round_num}] dismissed overlay via '{sel}'")
                    clicked = True
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                continue

        if not clicked:
            # No known button matched but a container is still visible —
            # force-remove known mask/dialog containers as a last resort so
            # they stop intercepting clicks. Doesn't register consent, but
            # unblocks scraping when button text/selectors don't match.
            try:
                removed = await page.evaluate(
                    """(sels) => {
                        let n = 0;
                        for (const sel of sels) {
                            document.querySelectorAll(sel).forEach(el => { el.remove(); n++; });
                        }
                        return n;
                    }""",
                    OVERLAY_CONTAINER_SELECTORS,
                )
                print(f"    [warn] round {round_num}: no overlay button matched — force-removed {removed} container element(s)")
            except Exception as e:
                print(f"    [warn] round {round_num}: overlay present and could not be cleared: {e}")
            return

    print(f"    [warn] overlays may still be present after {max_rounds} rounds")

async def find_first_visible(page: Page, selectors: list[str], timeout_ms: int = 5_000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None

async def click_into_fund_detail(page: Page, isin: str, fund_name: str) -> bool:
    """Best-effort click into the fund's own detail/factsheet page from the
    results row — try the fund name text first (likely the 'Name' column
    link), then fall back to clicking near the ISIN text itself."""
    candidates = []
    if fund_name:
        candidates.append(("fund name", page.get_by_text(fund_name, exact=True).first))
    candidates.append(("isin", page.get_by_text(isin, exact=True).first))
    for label, cand in candidates:
        try:
            await cand.wait_for(state="visible", timeout=3_000)
            await cand.click(timeout=3_000)
            await page.wait_for_timeout(2_000)
            print(f"    clicked into detail page via {label} text")
            return True
        except Exception:
            continue
    return False

async def try_click_tab(page: Page, tab_label: str) -> str:
    """Best-effort tab switch on the SPA detail page, returning the updated
    body text if the click succeeds."""
    candidates = [
        page.get_by_role("tab", name=tab_label, exact=True).first,
        page.locator(f"[role='tab']:has-text('{tab_label}')").first,
        page.locator(f"button:has-text('{tab_label}')").first,
        page.locator(f"a:has-text('{tab_label}')").first,
        page.get_by_text(tab_label, exact=True).first,
    ]
    for candidate in candidates:
        try:
            await candidate.wait_for(state="visible", timeout=3_000)
            await candidate.scroll_into_view_if_needed(timeout=1_000)
            await candidate.click(timeout=3_000)
            await page.wait_for_timeout(1_500)
            try:
                await page.wait_for_load_state("networkidle", timeout=2_000)
            except Exception:
                pass
            return await page.locator("body").inner_text(timeout=5_000)
        except Exception:
            continue
    return ""

async def search_and_open_fund(page: Page, isin: str) -> tuple[str, str | None]:
    """Use the on-page search box to look up the ISIN, capture the results
    row text, and try to click through to a richer fund detail page.
    Returns (results_page_text, detail_page_text_or_None). results_page_text
    is "" if the ISIN was never found at all."""
    # Overlays can re-appear or finish rendering late; sweep once more right
    # before we need to interact with the page.
    await dismiss_all_overlays(page)

    search_box = await find_first_visible(page, SEARCH_INPUT_SELECTORS, timeout_ms=10_000)
    if search_box is None:
        print("    [warn] could not locate a search input on the page")
        return "", None
    print("    search box located")
    try:
        await search_box.click()
        await search_box.fill(isin)
        await page.keyboard.press("Enter")
        print(f"    typed '{isin}' into search box and pressed Enter")
    except Exception as e:
        print(f"    [warn] typing into search box failed: {e}")
        return "", None

    await page.wait_for_timeout(2_000)

    try:
        results_text = await page.locator("body").inner_text(timeout=3_000)
    except Exception as e:
        print(f"    [warn] could not read results page text: {e}")
        return "", None

    found = isin in results_text.upper()
    print(f"    ISIN {'found' if found else 'NOT found'} in results page text "
          f"(length={len(results_text)} chars)")

    if not found:
        return "", None

    # Net assets / ticker / TER were not present on the results row in the
    # confirmed sample — try clicking into the fund's own page for them.
    lines = [clean(l) for l in results_text.splitlines() if clean(l)]
    fund_name = _parse_result_row(lines, isin).get("etf_name", "")
    clicked = await click_into_fund_detail(page, isin, fund_name)
    if not clicked:
        print("    [warn] could not click into a fund detail page — only results-row fields available")
        return results_text, None

    try:
        detail_text = await page.locator("body").inner_text(timeout=5_000)
    except Exception as e:
        print(f"    [warn] could not read detail page text: {e}")
        return results_text, None

    if detail_text.strip() == results_text.strip():
        print("    click-through did not change page content — staying on results-row data only")
        return results_text, None

    # Polling for a late-loading widget (handles genuine async-render races).
    # Confirmed via a live diagnostic capture that this was NOT what caused
    # IE000AVUROO8's AUM gap — its Overview tab simply never shows a Total
    # NAV block at all (it goes straight from "Unit NAV (GBP)" to "Fund
    # objectives and investment policy", skipping the slot where the working
    # ISINs show "Total NAV (USD) / USD 29.31 m"). Kept anyway since it's
    # cheap and harmless for the cases it does help.
    has_aum_signal = lambda text: any(p in text.lower() for p in TOTAL_NAV_LABEL_PREFIXES) or any(
        CURRENCY_AMOUNT_RE.match(clean(l)) for l in text.splitlines() if clean(l)
    )
    for _ in range(4):
        if has_aum_signal(detail_text):
            break
        await page.wait_for_timeout(1_500)
        try:
            refreshed = await page.locator("body").inner_text(timeout=3_000)
        except Exception:
            break
        if refreshed.strip() != detail_text.strip():
            print("    [info] detail page content changed after initial read — re-captured")
        detail_text = refreshed

    # The detail page exposes separate tabs (Overview, Risk Considerations,
    # Performance, Asset Allocation, Listings, Authorised Participants,
    # *Fund Facts*, Documents, Insights, ...). Confirmed live: the "Fund
    # Facts" tab exists in the tab list even on pages where AUM is missing
    # from Overview, and an Angular SPA typically doesn't render a tab's
    # content into the DOM until it's selected — so inner_text() on the
    # Overview tab alone can structurally never see data that only lives
    # under Fund Facts. Click it and fold its text in before giving up.
    if not has_aum_signal(detail_text):
        fund_facts_text = await try_click_tab(page, "Fund Facts")
        if fund_facts_text:
            print("    [info] checked 'Fund Facts' tab for AUM data")
            detail_text = f"{detail_text}\n{fund_facts_text}"
        else:
            print("    [warn] could not open 'Fund Facts' tab (selector miss or tab not present)")

    return results_text, detail_text

# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------
def _aum_diagnostic(record: dict | None, detail_text: str | None) -> dict[str, str]:
    """Build a small, inline diagnostic payload for the (now rare) case where
    AUM still can't be found after the named-key lookup, the labeled
    detail-page parse, and the value-shape heuristic scan have all missed.
    No _debug folder/file is written (removed per project convention) — this
    is just enough context attached directly to the row so a human can see
    *why* it failed from the JSON output alone, without re-running headed.
    """
    diag: dict[str, str] = {}
    if record:
        diag["aum_diagnostic_record_keys"] = ", ".join(sorted(record.keys()))
    if detail_text:
        # Capped, single-line snippet — enough to spot the actual label/value
        # wording used on the page without dumping the whole DOM text.
        flat = " | ".join(clean(l) for l in detail_text.splitlines() if clean(l))
        diag["aum_diagnostic_detail_snippet"] = flat[:1500]
    return diag

async def scrape_one_isin(page: Page, isin: str, target_isins: set[str], scraped_at: str) -> dict[str, str]:
    intercepted: dict[str, dict] = {}
    intercepted_payloads: dict[str, Any] = {}

    def candidate_score(obj: Any) -> int:
        try:
            return len(json.dumps(obj, ensure_ascii=False))
        except Exception:
            return len(str(obj))

    async def on_response(resp: Response) -> None:
        url = resp.url.lower()
        if not any(h in url for h in FUND_URL_HINTS):
            return
        ct = resp.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            body = await resp.json()
        except Exception:
            return
        response_matches: dict[str, dict] = {}
        _find_isin_records(body, target_isins, response_matches)
        for matched_isin, record in response_matches.items():
            prev = intercepted.get(matched_isin)
            if prev is None or candidate_score(record) > candidate_score(prev):
                intercepted[matched_isin] = record
            prev_payload = intercepted_payloads.get(matched_isin)
            if prev_payload is None or candidate_score(body) > candidate_score(prev_payload):
                intercepted_payloads[matched_isin] = body
            if matched_isin == isin and prev is None:
                print(f"    [intercept] matched {isin} via {resp.url[:90]}")
                print(f"    [intercept] record keys: {sorted(intercepted[isin].keys())}")

    page.on("response", on_response)
    try:
        print(f"[scrape] {isin}")
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await dismiss_all_overlays(page)
        results_text, detail_text = await search_and_open_fund(page, isin)

        # Give any lazy API calls a moment to land after navigation/search.
        for _ in range(5):
            if isin in intercepted:
                break
            await page.wait_for_timeout(1_000)

        if isin in intercepted:
            row = map_json_row(intercepted[isin], isin, scraped_at, payload=intercepted_payloads.get(isin))
            if detail_text:
                row = merge_detail_fields(row, parse_detail_page_fields(detail_text))
            if not row.get("net_assets_raw"):
                row["aum_note"] = "AUM not found via known field names, labels, or value-pattern scan"
                row.update(_aum_diagnostic(intercepted.get(isin), detail_text))
            return row

        if results_text:
            row = parse_dom_fields(results_text, isin, scraped_at)
            if detail_text:
                detail_fields = parse_detail_page_fields(detail_text)
                row = merge_detail_fields(row, detail_fields)
                if any(detail_fields.values()):
                    row["extraction_method"] = "dom_fallback+detail"
            if not row.get("net_assets_raw"):
                row["aum_note"] = "AUM not found via known field names, labels, or value-pattern scan"
                row.update(_aum_diagnostic(None, detail_text))
            return row

        return empty_row(isin, scraped_at, "no API match and no DOM match — fund page may not have opened")
    except Exception as e:
        print(f"    [error] {isin}: {e}")
        return empty_row(isin, scraped_at, str(e))
    finally:
        page.remove_listener("response", on_response)

async def scrape(scraped_at: str) -> list[dict]:
    target_isins = set(TARGET_ISINS)
    rows: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=browser_launch_args(
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ),
        )
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            **context_https_kwargs(),
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

        for isin in TARGET_ISINS:
            row = await scrape_one_isin(page, isin, target_isins, scraped_at)
            rows.append(row)

        await browser.close()

    return rows

# ---------------------------------------------------------------------------
# Snapshot + I/O
# ---------------------------------------------------------------------------
def print_summary(rows: list[dict]) -> None:
    ok = sum(1 for r in rows if r.get("extraction_method") != "failed")
    print(f"Source URL used:    {SEARCH_URL}")
    print(f"Target ISINs:       {len(TARGET_ISINS)}")
    print(f"Rows extracted OK:  {ok}/{len(rows)}")
    for r in rows:
        status = r.get("extraction_method", "")
        print(f"  {r['isin']}: {status}" + (f" ({r.get('failure_reason','')})" if status == "failed" else ""))

async def build_snapshot(now: datetime) -> dict:
    scraped_at = now.isoformat()
    rows = await scrape(scraped_at)
    print_summary(rows)
    return {
        "source_url": SEARCH_URL,
        "method": "Schroders Fund Centre — search-by-ISIN, API interception + DOM fallback",
        "captured_at": scraped_at,
        "row_count": len(rows),
        "rows": rows,
    }

def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

async def download_schroders_file() -> Path:
    now = datetime.now()
    output_path = build_output_path(now)
    snapshot = await build_snapshot(now)
    write_json(output_path, snapshot)
    return output_path

def main() -> None:
    output_path = asyncio.run(download_schroders_file())
    print(f"Raw snapshot saved: {output_path}")
    print(f"Done! Open your file at: {output_path.resolve()}")

if __name__ == "__main__":
    main()
