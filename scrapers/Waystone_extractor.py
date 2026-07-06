from __future__ import annotations
 
import asyncio
import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Locator, Page, Response, async_playwright
 
try:
    from scrapers.tls_compat import browser_launch_args, context_https_kwargs
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    from tls_compat import browser_launch_args, context_https_kwargs
 
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LISTING_URL = "https://etfs.waystone.com/fund-listing/"
ISSUER   = ""          # Not exposed as a column on this listing page.
PROVIDER = "Waystone"
 
# The ETFs we care about — everything else returned by the site is ignored.
TARGET_ISINS = [
    "IE00073MUWT4",
    "IE0008ZGI5C1",
    "IE000DHZXD61",
    "IE000QF8TEK7",
    "IE000ZDPZL69",
]
 
BASE_DIR   = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Waystone"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
TIMEOUT_MS = 60_000
 
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
 
ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
FUND_URL_HINTS = ("fund", "listing", "etf", "product", "table")
 
# Best-effort selectors — see module docstring.
OVERLAY_BUTTON_TEXTS = [
    "Accept all cookies", "Accept All", "Accept all", "Accept",
    "I Accept", "I agree", "Agree", "Allow all", "Allow All",
    "Continue", "Confirm", "Submit", "OK", "Got it",
]
OVERLAY_BUTTON_SELECTORS = (
    ["#onetrust-accept-btn-handler"]
    + [f"button:has-text('{t}')" for t in OVERLAY_BUTTON_TEXTS]
    + ["button[aria-label*='Accept' i]"]
)
OVERLAY_CONTAINER_SELECTORS = [
    "#onetrust-banner-sdk",
    "[role='dialog']",
    "[aria-modal='true']",
    "[class*='cookie' i][class*='banner' i]",
    "[class*='consent' i]",
]
 
# NOTE: an earlier version had a SEARCH_INPUT_SELECTORS constant + a filter-
# box shortcut here. Removed — a live run showed the page also has an
# unrelated global site-nav search box with no reliable way to distinguish
# it from the real DataTables filter via selector alone, and the shortcut
# never cleared the box afterward, risking a permanently-filtered table for
# the rest of the run. The full scan below doesn't need it.
TABLE_SELECTOR = "table.dataTable"
ROW_SELECTOR = "tbody tr"
EMPTY_ROW_CLASS_HINT = "datatables_empty"
 
# Page-length dropdown ("Show N entries") — bumped to its largest option
# where possible to minimize how many "next" clicks the full scan needs.
PAGE_LENGTH_SELECT_SELECTORS = [
    ".tab-content.active div.dataTables_length select",
    "div.dataTables_length select",
    "select[name$='_length']",
]
# "Next page" control for DataTables' default pagination markup.
NEXT_PAGE_BUTTON_SELECTORS = [
    ".tab-content.active .paginate_button.next",
    ".paginate_button.next",
    "a.paginate_button.next",
    "li.paginate_button.next a",
    "[aria-label='Next']",
]
MAX_PAGES_PER_TAB = 50  # safety cap against an unexpected infinite loop
 
# Tab controls grouping funds by category. Best-effort — if none of these
# match, the scraper just treats the page as single-tab.
TAB_BUTTON_SELECTORS = [
    ".custom-tabs .custom-tab",
    ".custom-tabs [role='tab']",
    ".custom-tabs button",
    ".custom-tabs a",
]

WAYSTONE_COUNTRY_OPTIONS = [
    "United Kingdom",
    "Ireland",
    "Luxembourg",
    "Germany",
    "Switzerland",
    "Singapore",
]
WAYSTONE_INVESTOR_TYPES = [
    "Institutional Investor",
    "Individual Investor",
]
WAYSTONE_ACCEPT_SELECTORS = [
    "#acceptTermsCondition .accept-button-pop",
    "#region-investor-type-modal .accept-button-pop",
    "#acceptTermsCondition",
]
WAYSTONE_COUNTRY_SELECTORS = [
    "#region-investor-type-modal select.country-list",
    "select.country-list",
]
WAYSTONE_INVESTOR_SELECTORS = [
    "#region-investor-type-modal select.investor-type-list",
    "select.investor-type-list",
]
EMPTY_DISPLAY_RE = re.compile(r"Displaying:?\s*0\s*-\s*0\b", re.IGNORECASE)
WAYSTONE_DETAIL_AS_OF_RE = re.compile(
    r"\bAs of\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b",
    re.IGNORECASE,
)
WAYSTONE_FUND_LEVEL_AUM_LABELS = (
    "Total Net Assets",
    "Fund AUM",
    "Fund Assets",
    "Assets Under Management",
    "AUM",
)
WAYSTONE_SHARE_CLASS_AUM_LABELS = (
    "Share Class Assets",
)

WAYSTONE_EXTERNAL_FALLBACKS: dict[str, dict[str, str]] = {
    "IE000DHZXD61": {
        "issuer": "Calamos",
        "etf_name": "Calamos Autocallable Income UCITS ETF",
        "ticker": "CAKE",
        "ccy": "USD",
        "aum_currency": "USD",
        "aum_reference_isin": "IE000ZDPZL69",
        "sfdr_classification": "",
        "ongoing_charges_raw": "0.74%",
        "ter_bps": "74.00",
        "inception": "27/04/2026",
        "inception_norm": "27/04/2026",
        "factsheet_url": "https://www.calamos.com/resources/#ucitsfunds",
        "source_url": "https://www.calamos.com/about/news/press-releases/2026/worlds-first-autocallable-ucits-etf/",
        "fallback_origin_url": "https://www.calamos.com",
        "fallback_notes": (
            "Recovered from Calamos' official UCITS ETF launch release. "
            "The release identifies IE000DHZXD61 as the accumulating share class of "
            "Calamos Autocallable Income UCITS ETF, ticker CAKE, with a 0.74% expense ratio."
        ),
        "aum_source_note": (
            "AUM is inferred from sibling share class IE000ZDPZL69 on the same Waystone listing snapshot, "
            "because the launch release and the listed distributing share class refer to the same fund."
        ),
    },
    "IE0008ZGI5C1": {
        "issuer": "Northern Trust",
        "etf_name": "Northern Trust Listed Private Equity UCITS ETF",
        "ticker": "FLPE",
        "ccy": "USD",
        "net_assets_raw": "USD 320.20 m",
        "aum_numeric": "320.20",
        "aum_m": "320.20",
        "aum_currency": "USD",
        "aum_as_of_date": "31/03/2025",
        "sfdr_classification": "Article 6",
        "ongoing_charges_raw": "0.40%",
        "ter_bps": "40.00",
        "inception": "09/12/2021",
        "inception_norm": "09/12/2021",
        "factsheet_url": "https://www.flexshares.com/content/dam/ntflexshares/eu-common/images/funds/flpe/kid-flpe-en.pdf",
        "source_url": "https://www.flexshares.com/kiids",
        "fallback_origin_url": "https://etfs.ntam.northerntrust.com/gb",
        "aum_source_url": "https://www.flexshares.com/content/dam/ntflexshares/eu-common/images/funds/kiids/icav/icav-annual-accounts-2025.pdf",
        "fallback_notes": (
            "Recovered from FlexShares' official UCITS documents. "
            "The KIID and supplement identify IE0008ZGI5C1 as Northern Trust Listed Private Equity UCITS ETF (FLPE), "
            "USD accumulating, with ongoing charges of 0.40%."
        ),
        "aum_source_note": (
            "AUM comes from the official Waystone ETF ICAV annual report for the year ended 31 March 2025. "
            "The report shows net assets attributable to holders of redeemable shares of approximately USD 320.20m "
            "for FlexShares Listed Private Equity UCITS ETF."
        ),
        "index_name": "MSCI World IMI Listed Private Equity Select (USD Net Total Return) Index",
    },
}
 
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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "waystone_etf_export.json"
 
# ---------------------------------------------------------------------------
# Normalisation (mirrors schroders_extractor.py / globalx_extractor.py conventions)
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
 
def parse_millions_value(raw: str) -> tuple[str, str]:
    """Parse a value from a column that's already expressed in millions
    (e.g. the "AUM(M)" column header confirms this — a cell reading
    "€114.72" means €114.72m, not €114.72 outright), unlike Schroders' raw
    fund-size figures which needed /1e6 scaling. Returns (numeric_str, ccy).
    """
    s = clean(raw)
    if not s:
        return "", ""
    ccy = detect_ccy(s)
    n = re.sub(r"[$€£,]", "", s).strip()
    n = re.sub(r"[^0-9.\-]", "", n)
    if not n:
        return "", ccy
    try:
        return fmt_dec(Decimal(n)), ccy
    except InvalidOperation:
        return "", ccy


def parse_raw_money_to_millions(raw: str) -> tuple[str, str, Decimal | None]:
    s = clean(raw)
    if not s:
        return "", "", None

    ccy = detect_ccy(s)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return "", ccy, None

    try:
        amount = Decimal(m.group(0).replace(",", ""))
    except InvalidOperation:
        return "", ccy, None

    lowered = s.casefold()
    if re.search(r"\b(billion|bn)\b", lowered):
        amount_m = amount * Decimal("1000")
    elif re.search(r"\b(million|mn)\b", lowered) or re.search(r"\d(?:[\d,]*)(?:\.\d+)?\s*m\b", lowered):
        amount_m = amount
    elif re.search(r"\b(thousand|k)\b", lowered):
        amount_m = amount / Decimal("1000")
    else:
        amount_m = amount / Decimal("1000000")

    return fmt_dec(amount_m), ccy, amount_m


def parse_decimal(raw: str) -> Decimal | None:
    s = re.sub(r"[^0-9.\-]", "", clean(raw))
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def same_two_dp(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    q = Decimal("1.00")
    return (
        left.quantize(q, rounding=ROUND_HALF_UP)
        == right.quantize(q, rounding=ROUND_HALF_UP)
    )

def norm_date(raw: str) -> str:
    s = clean(raw)
    if not s: return ""
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try: return datetime.strptime(s, fmt).strftime("%d/%m/%Y 00:00:00")
        except ValueError: pass
    return s


def normalize_waystone_detail_url(raw: str) -> str:
    url = clean(raw)
    if not url:
        return ""
    absolute = urljoin(LISTING_URL, url)
    parsed = urlparse(absolute)
    if parsed.netloc.lower() != "etfs.waystone.com":
        return ""
    if "/fund/" not in parsed.path.lower():
        return ""
    return absolute


def normalize_waystone_field_label(raw: str) -> str:
    return re.sub(r"\s+", " ", clean(raw)).strip(" :").casefold()


def find_waystone_field_value(pairs: list[tuple[str, str]], labels: tuple[str, ...]) -> tuple[str, str]:
    wanted = {normalize_waystone_field_label(label): label for label in labels}
    for label, value in pairs:
        if normalize_waystone_field_label(label) in wanted and value:
            return label, value
    return "", ""


def extract_waystone_detail_aum(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    pairs: list[tuple[str, str]] = []
    key_info_table = None

    for table in soup.select("table.fund-details-table, table#key_information"):
        if key_info_table is None:
            key_info_table = table
        for row in table.select("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = clean(cells[0].get_text(" ", strip=True))
            value = clean(cells[1].get_text(" ", strip=True))
            if label and value:
                pairs.append((label, value))

    if not pairs:
        for block in soup.select(".fund-banner-block"):
            label_node = block.select_one(".label-banner")
            value_node = block.select_one(".value-banner")
            if label_node is None or value_node is None:
                continue
            label = clean(label_node.get_text(" ", strip=True))
            value = clean(value_node.get_text(" ", strip=True))
            if label and value:
                pairs.append((label, value))

    as_of_date = ""
    if key_info_table is not None:
        for node in key_info_table.next_elements:
            if not isinstance(node, str):
                continue
            match = WAYSTONE_DETAIL_AS_OF_RE.search(clean(node))
            if match:
                as_of_date = match.group(1)
                break
    if not as_of_date:
        for text in soup.stripped_strings:
            match = WAYSTONE_DETAIL_AS_OF_RE.search(clean(text))
            if match:
                as_of_date = match.group(1)
        if as_of_date:
            as_of_date = clean(as_of_date)

    fund_label, fund_raw = find_waystone_field_value(pairs, WAYSTONE_FUND_LEVEL_AUM_LABELS)
    share_class_label, share_class_raw = find_waystone_field_value(pairs, WAYSTONE_SHARE_CLASS_AUM_LABELS)
    fund_m, fund_ccy, fund_decimal_m = parse_raw_money_to_millions(fund_raw)
    share_class_m, share_class_ccy, share_class_decimal_m = parse_raw_money_to_millions(share_class_raw)

    return {
        "as_of_date": as_of_date,
        "fund_label": fund_label,
        "fund_raw": fund_raw,
        "fund_aum_m": fund_m,
        "fund_currency": fund_ccy,
        "fund_decimal_m": fund_decimal_m,
        "share_class_label": share_class_label,
        "share_class_raw": share_class_raw,
        "share_class_aum_m": share_class_m,
        "share_class_currency": share_class_ccy,
        "share_class_decimal_m": share_class_decimal_m,
    }
 
PAGE_DATE_RE = re.compile(r"Displaying:?\s*\d+\s*-\s*\d+\s+(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)
 
def extract_page_snapshot_date(page_text: str) -> str:
    """The listing shows a page-wide 'Displaying: 6-6 30/06/2026' label —
    a single as-of date for the whole table, not per-fund. Applied to every
    row rather than looked up per-ISIN. Confirmed live that this can be
    briefly absent from body innerText on the very first read (likely still
    rendering), so the caller retries a couple of times rather than treating
    one miss as final."""
    m = PAGE_DATE_RE.search(page_text)
    return m.group(1) if m else ""
 
# ---------------------------------------------------------------------------
# JSON interception helpers (in case the table is backed by an API)
# ---------------------------------------------------------------------------
def _find_isin_records(obj: Any, target_isins: set[str], found: dict[str, dict]) -> None:
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
 
def map_json_row(raw: dict, isin: str, scraped_at: str, page_date: str) -> dict[str, str]:
    ticker  = _g(raw, "ticker", "tickerSymbol", "symbol")
    name    = _g(raw, "name", "fundName", "shareClassName", "displayName", "title")
    aum_raw = _g(raw, "aum", "aumM", "fundSize", "fund_size", "netAssets", "net_assets", "totalNetAssets")
    nav_raw = _g(raw, "nav", "NAV", "navPerShare", "price")
    ter_raw = _g(raw, "ongoingCharge", "ongoing_charge", "ocf", "ter", "TER", "expenseRatio")
    sfdr    = _g(raw, "sfdr", "sfdrClassification", "article")
    ccy     = _g(raw, "baseCurrency", "base_currency", "currency", "ccy")
    href    = _g(raw, "url", "link", "factsheetUrl", "detailUrl")
    # AUM here has no confirmed live sample, so both scalings are plausible:
    # the JSON payload might mirror the DOM's already-in-millions figure, or
    # (more typically for an API) report the raw currency amount. Treat a
    # very large bare number (>100,000) as raw units needing /1e6; anything
    # smaller is assumed to already be expressed in millions, matching the
    # DOM's "AUM(M)" column semantics.
    aum_num_str, ccy_from_aum = parse_millions_value(aum_raw)
    if not ccy:
        ccy = ccy_from_aum
    try:
        aum_dec = Decimal(re.sub(r"[^0-9.\-]", "", aum_raw)) if aum_raw else None
    except InvalidOperation:
        aum_dec = None
    if aum_dec is not None and aum_dec > 100_000:
        aum_num_str = fmt_dec(aum_dec / Decimal("1e6"))
    return {
        "provider": PROVIDER, "issuer": ISSUER,
        "etf_name": name, "ticker": ticker,
        "isin": isin,
        "net_assets_raw": aum_raw, "aum_numeric": aum_num_str, "aum_m": aum_num_str,
        "aum_currency": ccy, "ccy": ccy,
        "nav_raw": nav_raw,
        "as_of_date": page_date, "date": norm_date(page_date),
        "sfdr_classification": sfdr,
        "ongoing_charges_raw": ter_raw, "ter_bps": ter_bps(ter_raw),
        "inception": "", "inception_norm": "",
        "factsheet_url": href,
        "detail_url": normalize_waystone_detail_url(href),
        "source_url": LISTING_URL, "scraped_at": scraped_at,
        "extraction_method": "api_intercept",
    }
 
# ---------------------------------------------------------------------------
# DOM extraction — real <table> rows, read via Playwright locators rather
# than text-blob parsing (this is a plain HTML table, not an SPA text dump).
# ---------------------------------------------------------------------------
async def get_visible_table(page: Page) -> Locator | None:
    tables = page.locator(TABLE_SELECTOR)
    count = await tables.count()
    for i in range(count):
        t = tables.nth(i)
        try:
            if await t.is_visible(timeout=500):
                return t
        except Exception:
            continue
    if count == 0:
        # "table.dataTable" matched nothing at all — the class-name guess
        # may simply be wrong. Fall back to any <table> on the page rather
        # than giving up outright, and log both counts so a persistent
        # miss is diagnosable from stdout instead of a silent dead end.
        any_tables = page.locator("table")
        any_count = await any_tables.count()
        print(f"    [diag] 'table.dataTable' matched 0 elements; generic 'table' matched {any_count}")
        for i in range(any_count):
            t = any_tables.nth(i)
            try:
                if await t.is_visible(timeout=500):
                    cls = await t.get_attribute("class") or "(no class)"
                    print(f"    [diag] using generic <table> #{i} as fallback (class='{cls}')")
                    return t
            except Exception:
                continue
    return None
 
async def wait_for_any_table(page: Page, timeout_ms: int = 20_000) -> bool:
    """SPA tables often aren't in the DOM yet right after 'domcontentloaded'
    — a fixed short sleep isn't enough to know when they show up. Actively
    wait for either the guessed selector or a generic <table> to attach,
    up to timeout_ms, instead of giving up after one fixed-length pause."""
    try:
        await page.wait_for_selector(f"{TABLE_SELECTOR}, table", state="attached", timeout=timeout_ms)
        return True
    except Exception as e:
        print(f"    [warn] no <table> element ever attached to the page within {timeout_ms}ms: {e}")
        return False
 
async def get_table_headers(table: Locator) -> list[str]:
    ths = table.locator("thead th")
    n = await ths.count()
    headers: list[str] = []
    for i in range(n):
        headers.append(clean(await ths.nth(i).inner_text()).lower())
    return headers
 
# Canonical field <- header-text aliases. Matched by substring so minor
# header wording differences ("AUM (M)" vs "AUM(M)") still resolve.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name",),
    "ticker": ("ticker",),
    "isin": ("isin",),
    "sfdr": ("sfdr",),
    "aum": ("aum",),
    "nav": ("nav",),
    "ter": ("ongoing charge", "ter", "ocf", "fee"),
}
 
def map_dom_row(headers: list[str], cells: list[str], href: str, isin: str,
                 scraped_at: str, page_date: str) -> dict[str, str]:
    # Build header->value by matching each header text against the alias
    # table; falls back to positional guess (name, ticker, isin, sfdr, aum,
    # nav, ter — the order confirmed live) if header/cell counts mismatch.
    field_values: dict[str, str] = {}
    n = min(len(headers), len(cells))
    for i in range(n):
        h = headers[i]
        for field, aliases in HEADER_ALIASES.items():
            if field in field_values:
                continue
            if any(a in h for a in aliases):
                field_values[field] = cells[i]
 
    fallback_order = ["name", "ticker", "isin", "sfdr", "aum", "nav", "ter"]
    if len(field_values) < len(fallback_order) and len(cells) >= len(fallback_order):
        for idx, field in enumerate(fallback_order):
            field_values.setdefault(field, cells[idx])
 
    name_raw = field_values.get("name", "")
    # Strip badge text like "*NEW" that rides along in the name cell.
    name_clean = re.sub(r"\*?\bNEW\b\*?", "", name_raw, flags=re.IGNORECASE).strip()
 
    aum_num_str, aum_ccy = parse_millions_value(field_values.get("aum", ""))
    nav_raw = field_values.get("nav", "")
    if not aum_ccy:
        aum_ccy = detect_ccy(nav_raw)
 
    ter_raw = field_values.get("ter", "")
 
    return {
        "provider": PROVIDER, "issuer": ISSUER,
        "etf_name": name_clean, "ticker": field_values.get("ticker", ""),
        "isin": isin,
        "net_assets_raw": field_values.get("aum", ""), "aum_numeric": aum_num_str, "aum_m": aum_num_str,
        "aum_currency": aum_ccy, "ccy": aum_ccy,
        "nav_raw": nav_raw,
        "as_of_date": page_date, "date": norm_date(page_date),
        "sfdr_classification": field_values.get("sfdr", ""),
        "ongoing_charges_raw": ter_raw, "ter_bps": ter_bps(ter_raw),
        "inception": "", "inception_norm": "",
        "factsheet_url": href,
        "detail_url": normalize_waystone_detail_url(href),
        "source_url": LISTING_URL, "scraped_at": scraped_at,
        "extraction_method": "dom_table",
    }
 
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
        "factsheet_url": "",
        "source_url": LISTING_URL, "scraped_at": scraped_at,
        "extraction_method": "failed", "failure_reason": reason,
        "diagnostic_snippet": "", "diagnostic_collected_isins_sample": "",
    }


def build_row_from_available_source(
    isin: str,
    intercepted: dict[str, dict],
    collected: dict[str, dict[str, str]],
    scraped_at: str,
    page_date: str,
) -> dict[str, str] | None:
    if isin in intercepted:
        return map_json_row(intercepted[isin], isin, scraped_at, page_date)
    if isin in collected:
        match = collected[isin]
        return map_dom_row(match["headers"], match["cells"], match["href"], isin, scraped_at, page_date)
    return None


def resolve_manual_fallback_aum(
    fallback: dict[str, str],
    intercepted: dict[str, dict],
    collected: dict[str, dict[str, str]],
    scraped_at: str,
    page_date: str,
) -> tuple[str, str, str, str, str]:
    direct_raw = clean(fallback.get("net_assets_raw", ""))
    direct_numeric = clean(fallback.get("aum_numeric", "")) or clean(fallback.get("aum_m", ""))
    direct_currency = clean(fallback.get("aum_currency", ""))
    direct_as_of_date = clean(fallback.get("aum_as_of_date", ""))
    if direct_raw or direct_numeric:
        return direct_raw, direct_numeric, direct_numeric, direct_currency, direct_as_of_date

    return "", "", "", direct_currency, direct_as_of_date


def resolve_manual_fallback_detail_url(
    fallback: dict[str, str],
    intercepted: dict[str, dict],
    collected: dict[str, dict[str, str]],
    scraped_at: str,
    page_date: str,
) -> str:
    direct_url = normalize_waystone_detail_url(
        fallback.get("detail_url", "")
        or fallback.get("factsheet_url", "")
        or fallback.get("source_url", "")
    )
    if direct_url:
        return direct_url

    reference_isin = clean(fallback.get("aum_reference_isin", "")).upper()
    if not reference_isin:
        return ""

    reference_row = build_row_from_available_source(reference_isin, intercepted, collected, scraped_at, page_date)
    if reference_row is None:
        return ""

    return normalize_waystone_detail_url(
        reference_row.get("detail_url", "")
        or reference_row.get("factsheet_url", "")
        or reference_row.get("source_url", "")
    )


def manual_official_fallback_row(
    isin: str,
    scraped_at: str,
    page_date: str,
    intercepted: dict[str, dict],
    collected: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    fallback = WAYSTONE_EXTERNAL_FALLBACKS.get(isin)
    if fallback is None:
        return None

    net_assets_raw, aum_numeric, aum_m, aum_currency, aum_as_of_date = resolve_manual_fallback_aum(
        fallback,
        intercepted,
        collected,
        scraped_at,
        page_date,
    )
    detail_url = resolve_manual_fallback_detail_url(
        fallback,
        intercepted,
        collected,
        scraped_at,
        page_date,
    )
    as_of_date = aum_as_of_date or page_date or clean(fallback.get("inception", ""))
    return {
        "provider": PROVIDER,
        "issuer": clean(fallback.get("issuer", "")),
        "etf_name": clean(fallback.get("etf_name", "")),
        "ticker": clean(fallback.get("ticker", "")),
        "isin": isin,
        "net_assets_raw": net_assets_raw,
        "aum_numeric": aum_numeric,
        "aum_m": aum_m,
        "aum_currency": aum_currency or clean(fallback.get("aum_currency", "")),
        "ccy": clean(fallback.get("ccy", "")),
        "nav_raw": "",
        "as_of_date": as_of_date,
        "date": norm_date(as_of_date),
        "sfdr_classification": clean(fallback.get("sfdr_classification", "")),
        "ongoing_charges_raw": clean(fallback.get("ongoing_charges_raw", "")),
        "ter_bps": clean(fallback.get("ter_bps", "")),
        "inception": clean(fallback.get("inception", "")),
        "inception_norm": clean(fallback.get("inception_norm", "")),
        "factsheet_url": clean(fallback.get("factsheet_url", "")),
        "detail_url": detail_url,
        "source_url": clean(fallback.get("source_url", "")),
        "scraped_at": scraped_at,
        "extraction_method": "official_manual_fallback",
        "listing_source_url": LISTING_URL,
        "fallback_origin_url": clean(fallback.get("fallback_origin_url", "")),
        "fallback_notes": clean(fallback.get("fallback_notes", "")),
        "aum_as_of_date": aum_as_of_date,
        "aum_source_note": clean(fallback.get("aum_source_note", "")),
        "aum_source_url": clean(fallback.get("aum_source_url", "")),
        "index_name": clean(fallback.get("index_name", "")),
    }
 
# ---------------------------------------------------------------------------
# Page interaction helpers
# ---------------------------------------------------------------------------
async def dismiss_all_overlays(page: Page, max_rounds: int = 5) -> None:
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

async def get_page_text(page: Page) -> str:
    try:
        return await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""


async def fetch_waystone_detail_metrics(context, url: str) -> dict[str, Any]:
    detail_page = await context.new_page()
    try:
        await detail_page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await accept_waystone_cookie_banner(detail_page)
        await complete_country_investor_gate(detail_page)
        await accept_waystone_cookie_banner(detail_page)
        try:
            await detail_page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        html = await detail_page.content()
        return extract_waystone_detail_aum(html)
    finally:
        await detail_page.close()


def should_apply_waystone_detail_aum(row: dict[str, str], detail_metrics: dict[str, Any]) -> bool:
    fund_decimal_m = detail_metrics.get("fund_decimal_m")
    if fund_decimal_m is None:
        return False

    row_decimal_m = parse_decimal(row.get("aum_m", "") or row.get("aum_numeric", ""))
    if row_decimal_m is None:
        return True

    share_class_decimal_m = detail_metrics.get("share_class_decimal_m")
    if share_class_decimal_m is None:
        return False

    return (
        same_two_dp(row_decimal_m, share_class_decimal_m)
        and not same_two_dp(fund_decimal_m, share_class_decimal_m)
    )


def apply_waystone_detail_aum(row: dict[str, str], detail_url: str, detail_metrics: dict[str, Any]) -> None:
    row["net_assets_raw"] = clean(detail_metrics.get("fund_raw", ""))
    row["aum_numeric"] = clean(detail_metrics.get("fund_aum_m", ""))
    row["aum_m"] = clean(detail_metrics.get("fund_aum_m", ""))
    row["aum_currency"] = clean(detail_metrics.get("fund_currency", "")) or clean(row.get("aum_currency", ""))
    as_of_date = clean(detail_metrics.get("as_of_date", ""))
    if as_of_date:
        row["as_of_date"] = as_of_date
        row["date"] = norm_date(as_of_date)
    row["aum_source_url"] = detail_url
    label = clean(detail_metrics.get("fund_label", ""))
    if label:
        row["aum_source_note"] = f"Fund-level {label} extracted from the official Waystone product page."


async def enrich_rows_with_waystone_detail_aum(context, rows: list[dict[str, str]]) -> None:
    detail_cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        detail_url = normalize_waystone_detail_url(row.get("detail_url", "") or row.get("factsheet_url", ""))
        if not detail_url:
            continue

        if detail_url not in detail_cache:
            detail_cache[detail_url] = await fetch_waystone_detail_metrics(context, detail_url)
        detail_metrics = detail_cache[detail_url]

        fund_decimal_m = detail_metrics.get("fund_decimal_m")
        share_class_decimal_m = detail_metrics.get("share_class_decimal_m")
        row_decimal_m = parse_decimal(row.get("aum_m", "") or row.get("aum_numeric", ""))
        if should_apply_waystone_detail_aum(row, detail_metrics):
            apply_waystone_detail_aum(row, detail_url, detail_metrics)
            continue

        if fund_decimal_m is None and (
            row_decimal_m is None
            or (share_class_decimal_m is not None and same_two_dp(row_decimal_m, share_class_decimal_m))
        ):
            print(
                f"[warn] Waystone fund-level AUM not found | provider={PROVIDER} | "
                f"isin={row.get('isin', '')} | url={detail_url}"
            )

async def click_first_visible_text(page: Page, labels: list[str], timeout_ms: int = 1_500) -> str:
    for label in labels:
        try:
            loc = page.get_by_text(label, exact=True).first
            if await loc.is_visible(timeout=timeout_ms):
                await loc.click(timeout=3_000)
                return label
        except Exception:
            continue
    return ""

async def select_first_matching_option(page: Page, selectors: list[str], labels: list[str]) -> str:
    for selector in selectors:
        select = page.locator(selector).first
        try:
            if not await select.is_visible(timeout=1_500):
                continue
        except Exception:
            continue

        for label in labels:
            try:
                await select.select_option(label=label, timeout=3_000)
                return label
            except Exception:
                continue
    return ""

async def accept_waystone_cookie_banner(page: Page) -> bool:
    cookie_selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept All')",
    ]
    button = await find_first_visible(page, cookie_selectors, timeout_ms=1_500)
    if button is None:
        return False
    try:
        await button.click(timeout=3_000)
        await page.wait_for_timeout(800)
        print("    accepted cookie banner")
        return True
    except Exception as e:
        print(f"    [warn] failed clicking cookie banner accept button: {e}")
        return False

async def complete_country_investor_gate(page: Page) -> bool:
    page_text = await get_page_text(page)
    if "Select Your Country" not in page_text or "Select Your Investment Type" not in page_text:
        return False

    print("    detected country/investor gate; selecting profile")
    selected_country = await select_first_matching_option(page, WAYSTONE_COUNTRY_SELECTORS, WAYSTONE_COUNTRY_OPTIONS)
    if not selected_country:
        selected_country = await click_first_visible_text(page, WAYSTONE_COUNTRY_OPTIONS)

    selected_investor_type = await select_first_matching_option(
        page,
        WAYSTONE_INVESTOR_SELECTORS,
        WAYSTONE_INVESTOR_TYPES,
    )
    if not selected_investor_type:
        selected_investor_type = await click_first_visible_text(page, WAYSTONE_INVESTOR_TYPES)

    if selected_country:
        print(f"    selected country: {selected_country}")
        await page.wait_for_timeout(500)
    else:
        print("    [warn] could not find a preferred country option in the gate")

    if selected_investor_type:
        print(f"    selected investor type: {selected_investor_type}")
        await page.wait_for_timeout(500)
    else:
        print("    [warn] could not find a preferred investor type in the gate")

    accept_button = await find_first_visible(page, WAYSTONE_ACCEPT_SELECTORS, timeout_ms=2_000)
    if accept_button is None:
        print("    [warn] could not find the Waystone gate accept button")
        return True

    try:
        await accept_button.click(timeout=3_000)
        await page.wait_for_timeout(1_500)
    except Exception as e:
        print(f"    [warn] failed clicking Waystone gate accept button: {e}")
        return True

    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    try:
        await page.wait_for_function(
            """() => {
                const modal = document.querySelector('#region-investor-type-modal');
                if (!modal) return true;
                const style = window.getComputedStyle(modal);
                return style.display === 'none' || modal.hidden === true;
            }""",
            timeout=8_000,
        )
    except Exception:
        pass

    try:
        await page.wait_for_function(
            r"""() => {
                const text = document.body ? document.body.innerText : '';
                const matches = Array.from(text.matchAll(/Displaying:?\s*(\d+)\s*-\s*(\d+)/gi));
                if (matches.length === 0) return false;
                return matches.some(match => !(match[1] === '0' && match[2] === '0'));
            }""",
            timeout=15_000,
        )
    except Exception:
        pass

    return True
 
async def get_tab_buttons(page: Page) -> list[Locator]:
    for sel in TAB_BUTTON_SELECTORS:
        loc = page.locator(sel)
        try:
            count = await loc.count()
        except Exception:
            continue
        if count > 0:
            return [loc.nth(i) for i in range(count)]
    return []
 
async def try_max_page_length(page: Page) -> None:
    """Best-effort: bump the 'Show N entries' dropdown to its largest option
    so the full scan needs fewer 'next' clicks. Harmless no-op if the
    control doesn't exist or doesn't match — the pagination loop in
    scan_all_rows_in_current_tab() works either way, just with more pages."""
    for sel in PAGE_LENGTH_SELECT_SELECTORS:
        try:
            select = page.locator(sel).first
            if not await select.is_visible(timeout=1_000):
                continue
            options = await select.locator("option").all()
            if not options:
                continue
            best_value, best_num = None, -1
            for opt in options:
                val = await opt.get_attribute("value")
                text = clean(await opt.inner_text())
                if val == "-1" or text.lower() == "all":
                    best_value, best_num = val, float("inf")
                    break
                try:
                    num = int(re.sub(r"[^0-9]", "", val or text))
                except ValueError:
                    continue
                if num > best_num:
                    best_value, best_num = val, num
            if best_value is not None:
                await select.select_option(value=best_value)
                print(f"    set page length to '{best_value}'")
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue
 
async def scan_all_rows_in_current_tab(page: Page) -> dict[str, dict[str, str]]:
    """Primary DOM method: read every row on every page of the currently
    active tab's table, keyed by ISIN. Depends only on the table + (optional)
    pagination controls existing — not on a filter/search box, since that
    selector guess was confirmed live not to match anything on the page."""
    collected: dict[str, dict[str, str]] = {}
 
    await wait_for_any_table(page, timeout_ms=10_000)
    table = await get_visible_table(page)
    if table is None:
        print("    [warn] no visible table found in this tab")
        return collected
 
    await try_max_page_length(page)
 
    headers = await get_table_headers(table)
    if not headers:
        print("    [warn] table has no <thead th> — header mapping will rely on positional fallback")
 
    for page_num in range(1, MAX_PAGES_PER_TAB + 1):
        rows = table.locator(ROW_SELECTOR)
        row_count = await rows.count()
        page_rows = 0
        for i in range(row_count):
            row = rows.nth(i)
            cls = (await row.get_attribute("class") or "").lower()
            if EMPTY_ROW_CLASS_HINT in cls.replace("_", "").replace(" ", ""):
                continue
            cells = row.locator("td")
            cell_count = await cells.count()
            if cell_count == 0:
                continue
            texts = [clean(await cells.nth(j).inner_text()) for j in range(cell_count)]
            isin_match = next((t.upper() for t in texts if ISIN_RE.fullmatch(t.upper())), None)
            if not isin_match:
                continue
            href = ""
            try:
                href = await cells.nth(0).locator("a").first.get_attribute("href", timeout=500) or ""
            except Exception:
                pass
            collected[isin_match] = {"headers": headers, "cells": texts, "href": href}
            page_rows += 1
 
        print(f"    page {page_num}: read {page_rows} fund row(s), {len(collected)} total so far")
 
        next_btn = await find_first_visible(page, NEXT_PAGE_BUTTON_SELECTORS, timeout_ms=1_000)
        if next_btn is None:
            break
        try:
            aria_disabled = await next_btn.get_attribute("aria-disabled")
            cls = (await next_btn.get_attribute("class") or "").lower()
            if aria_disabled == "true" or "disabled" in cls:
                break
            await next_btn.click(timeout=2_000)
            await page.wait_for_timeout(700)
        except Exception:
            break
    else:
        print(f"    [warn] hit MAX_PAGES_PER_TAB={MAX_PAGES_PER_TAB} safety cap — some rows may be missing")
 
    return collected
 
async def scan_all_tabs(page: Page) -> dict[str, dict[str, str]]:
    """Runs the full-table scan once per tab (if tabs exist) and merges the
    results. Cheap optional filter-box shortcut is tried first per tab in
    case it does work with one of the guessed selectors — if so it narrows
    the table before the scan runs, which is faster but not required."""
    collected: dict[str, dict[str, str]] = {}
 
    tabs = await get_tab_buttons(page)
    tab_count = max(1, len(tabs))
    for i in range(tab_count):
        if tabs:
            print(f"  [tab {i + 1}/{tab_count}]")
            if i > 0:
                try:
                    await tabs[i].click(timeout=3_000)
                    await page.wait_for_timeout(800)
                    await accept_waystone_cookie_banner(page)
                except Exception:
                    continue
        tab_rows = await scan_all_rows_in_current_tab(page)
        collected.update(tab_rows)

    return collected

async def ensure_waystone_listing_ready(page: Page) -> None:
    await accept_waystone_cookie_banner(page)
    gate_handled = await complete_country_investor_gate(page)
    await accept_waystone_cookie_banner(page)

    page_text = await get_page_text(page)
    if EMPTY_DISPLAY_RE.search(page_text):
        print("    listing still shows 0-0 after initial load; retrying gate handling once")
        gate_handled = await complete_country_investor_gate(page)
        await accept_waystone_cookie_banner(page)
        if gate_handled:
            await page.wait_for_timeout(1_200)
 
def build_diagnostic_snippet(page_text: str) -> str:
    """Flattened, capped excerpt of the page text — attached to a row only
    when it genuinely can't be found, so a real miss can be debugged from
    the JSON output alone instead of guessing selectors again blind."""
    flat = " | ".join(clean(l) for l in page_text.splitlines() if clean(l))
    return flat[:1500]
 
# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------
async def scrape(scraped_at: str) -> list[dict]:
    target_isins = set(TARGET_ISINS)
    intercepted: dict[str, dict] = {}
    rows: list[dict] = []
 
    async with async_playwright() as p:
        # A response consisting of nothing but a bare "<!DOCTYPE html>" after
        # 30+ seconds of waiting (confirmed on a live run) isn't a slow-render
        # issue — that rules itself out by the wait time alone. It's the
        # signature of anti-bot/WAF detection (Cloudflare, Akamai, DataDome,
        # etc.) serving a near-blank stub to a detected headless browser
        # rather than a real error page. Try launching a real installed
        # Chrome (channel="chrome") first — its fingerprint differs from
        # bundled Chromium and is less commonly flagged — falling back to
        # bundled Chromium if Chrome isn't installed on this machine.
        launch_kwargs = dict(
            headless=True,
            args=browser_launch_args(
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ),
        )
        try:
            browser = await p.chromium.launch(channel="chrome", **launch_kwargs)
            print("    launched real Chrome (channel='chrome')")
        except Exception as e:
            print(f"    [info] channel='chrome' unavailable ({e}) — falling back to bundled Chromium")
            browser = await p.chromium.launch(**launch_kwargs)
 
        context = await browser.new_context(**context_https_kwargs())
        page = await context.new_page()

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
            before = set(intercepted.keys())
            _find_isin_records(body, target_isins, intercepted)
            newly_matched = set(intercepted.keys()) - before
            for m in newly_matched:
                print(f"    [intercept] matched {m} via {resp.url[:90]}")
                print(f"    [intercept] record keys: {sorted(intercepted[m].keys())}")
 
        page.on("response", on_response)
 
        print(f"[load] {LISTING_URL}")
        main_response = await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        if main_response is not None:
            hdrs = await main_response.all_headers()
            waf_hints = {k: v for k, v in hdrs.items() if k.lower() in (
                "server", "cf-ray", "cf-mitigated", "x-datadome", "x-akamai-transformed",
                "x-frame-options", "content-length", "content-type",
            )}
            print(f"    HTTP status: {main_response.status} {main_response.status_text} | "
                  f"headers of interest: {waf_hints}")
        else:
            print("    [warn] page.goto() returned no response object (unusual)")
        await ensure_waystone_listing_ready(page)
 
        # Wait for network activity to settle (best-effort — some sites never
        # go fully idle due to analytics polling, so don't let this block
        # forever) before actively waiting for a <table> to attach. A fixed
        # 1.5s sleep (the previous approach) isn't a real wait condition for
        # an SPA that renders its table client-side after initial load —
        # confirmed live: it produced "0 elements matched" for the table
        # selector, which more likely means "hadn't rendered yet" than "wrong
        # selector", since a generic <table> fallback (added below) gives us
        # a second chance to tell those two cases apart.
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            print("    [info] page never reached networkidle within 10s — continuing anyway")
        table_attached = await wait_for_any_table(page, timeout_ms=20_000)
 
        # Confirmed live: even after 30+s of waiting, the page can come back
        # as a literal bare "<!DOCTYPE html>" (15 chars, no head/body at
        # all) — a render-timing problem would still leave *some* HTML
        # skeleton behind, so this specifically looks like anti-bot/WAF
        # detection serving a stub to a flagged headless browser rather than
        # a slow load. One reload sometimes clears this (e.g. if a
        # JS-challenge "clearance" cookie gets set on the first hit and only
        # takes effect from the second navigation onward).
        if not table_attached:
            html_len = len(await page.content())
            if html_len < 500:
                print(f"    [warn] page HTML is suspiciously small ({html_len} chars) — "
                      f"looks like anti-bot detection, not a slow render. Retrying with a reload.")
                try:
                    reload_response = await page.reload(wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    if reload_response is not None:
                        print(f"    reload HTTP status: {reload_response.status} {reload_response.status_text}")
                except Exception as e:
                    print(f"    [warn] reload failed: {e}")
                await ensure_waystone_listing_ready(page)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                table_attached = await wait_for_any_table(page, timeout_ms=20_000)
                html_len = len(await page.content())
                print(f"    after reload — table attached: {table_attached}, HTML length: {html_len}")
 
        print(f"    page title: {await page.title()!r} | url: {page.url}")
        print(f"    table attached before scan: {table_attached}")
 
        # Page-wide snapshot date: retry a couple of times since it was
        # confirmed live to sometimes be absent from body innerText on the
        # very first read (likely still rendering at that point). Use
        # document.body.innerText via evaluate rather than
        # locator("body").inner_text() — the latter failed silently on a
        # live run (returned "" with no visible error because the exception
        # was being swallowed); evaluate() failures surface directly instead
        # of being caught-and-blanked, so a real problem shows up in stdout
        # this time instead of disappearing into an empty string.
        page_date = ""
        page_text = ""
        for attempt in range(3):
            page_text = await get_page_text(page)
            page_date = extract_page_snapshot_date(page_text)
            if page_date:
                break
            await page.wait_for_timeout(1_000)
        print(f"    body text length: {len(page_text)} chars")
        if page_date:
            print(f"    page snapshot date: {page_date}")
        else:
            print("    [warn] could not find a page-wide 'Displaying: X-Y DD/MM/YYYY' snapshot date after retries")
 
        # Give any lazy initial-load API calls a moment to land before the
        # full scan, so api_intercept can win over dom_table where available.
        for _ in range(3):
            if target_isins <= set(intercepted.keys()):
                break
            await page.wait_for_timeout(500)
 
        # NOTE: a previous version tried a "cheap shortcut" here — typing an
        # ISIN into whatever matched a guessed search-box selector before
        # the full scan. Removed after a live run showed the page also has
        # an unrelated global site-nav search box ("Search | Menu Menu"),
        # with no way to tell from the selector alone which one got hit —
        # and the shortcut never cleared the box afterward, so if it landed
        # on the real DataTables filter it could leave the table narrowed
        # to zero rows for the rest of the run (that run's diagnostic text
        # showed "Displaying: 0-0" immediately after the shortcut fired).
        # The full scan below doesn't need this shortcut to work, so it's
        # simply not worth the risk.
 
        print("[scan] reading full fund-listing table (all tabs, all pages)")
        collected = await scan_all_tabs(page)
        print(f"[scan] collected {len(collected)} fund row(s) total across the listing")
 
        missing_isins = [i for i in TARGET_ISINS if i not in intercepted and i not in collected]
        diagnostic_text = ""
        if missing_isins:
            # Re-capture fresh, right before building failure diagnostics
            # (post-scan state), rather than reusing the possibly-stale/empty
            # page_text from the pre-scan date extraction. If body text is
            # still empty, fall back to raw HTML length/prefix so the
            # diagnostic is never silently blank again.
            diagnostic_text = await get_page_text(page)
            if not diagnostic_text:
                try:
                    html = await page.content()
                    diagnostic_text = f"[body innerText was empty; raw HTML length={len(html)}] {html[:1500]}"
                except Exception as e:
                    diagnostic_text = f"[body innerText and page.content() both failed: {e}]"
 
        for isin in TARGET_ISINS:
            print(f"[scrape] {isin}")
            if isin in intercepted:
                rows.append(map_json_row(intercepted[isin], isin, scraped_at, page_date))
            elif isin in collected:
                match = collected[isin]
                rows.append(map_dom_row(match["headers"], match["cells"], match["href"], isin, scraped_at, page_date))
            else:
                manual_row = manual_official_fallback_row(isin, scraped_at, page_date, intercepted, collected)
                if manual_row is not None:
                    rows.append(manual_row)
                    continue
                row = empty_row(isin, scraped_at, "ISIN not found via API interception or full-table DOM scan")
                row["diagnostic_snippet"] = build_diagnostic_snippet(diagnostic_text)
                row["diagnostic_collected_isins_sample"] = ", ".join(sorted(collected.keys())[:15])
                rows.append(row)

        await enrich_rows_with_waystone_detail_aum(context, rows)
 
        page.remove_listener("response", on_response)
        await browser.close()
 
    return rows
 
# ---------------------------------------------------------------------------
# Snapshot + I/O
# ---------------------------------------------------------------------------
def print_summary(rows: list[dict]) -> None:
    ok = sum(1 for r in rows if r.get("extraction_method") != "failed")
    print(f"Source URL used:    {LISTING_URL}")
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
        "source_url": LISTING_URL,
        "method": "Waystone ETFs fund listing — DataTables filter-by-ISIN, API interception + DOM table extraction",
        "captured_at": scraped_at,
        "row_count": len(rows),
        "rows": rows,
    }
 
def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
 
async def download_waystone_file() -> Path:
    now = datetime.now()
    output_path = build_output_path(now)
    snapshot = await build_snapshot(now)
    write_json(output_path, snapshot)
    return output_path
 
def main() -> None:
    output_path = asyncio.run(download_waystone_file())
    print(f"Raw snapshot saved: {output_path}")
    print(f"Done! Open your file at: {output_path.resolve()}")
 
if __name__ == "__main__":
    main()
