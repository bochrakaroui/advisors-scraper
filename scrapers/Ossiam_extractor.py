"""Scrape Ossiam ETFs (name, ISIN, ccy, AUM, TER, etc.) from the official website.

SITE STRUCTURE (confirmed from a real rendered-page dump on 2026-07-01)
=========================================================================
ossiam.com/EN/fund is an Angular app built around a PrimeNG data table, NOT
a grid of links to per-fund pages. Two things must happen before any data
appears:

  1. A GDPR cookie bar ("We use cookies...") with a `button.gdpr-accept`
     ("Accept & Close" / "Refuse & Close").
  2. A blocking modal ("Select your profile") that requires:
       a. choosing a client profile: "Professional client" or "Retail client"
       b. choosing a country (clickable country-flag icons)
       c. clicking "I Agree to the terms & conditions"
     Until this is submitted, the fund table shows a placeholder row
     ("There are no share Classes for this fund yet.") instead of real data.

Once the gate is cleared, the fund table (`#pn_id_1`) lists one row per
fund with columns: [expand toggle] | Fund | Expertise | Strategy |
Asset Class | Investment Vehicle | AUM | SFDR Classification.

ISIN / currency / TER / NAV / launch date etc. live in a PER-FUND
EXPANDABLE ROW, revealed by clicking the toggle in the first column
(PrimeNG row-expansion pattern -- confirmed by `.p-row-toggler` CSS
present in the page, though the actual expanded markup was not captured
live, since the sandbox this script was written in has no network path to
ossiam.com). So: extraction of the *expanded* content uses generic
table/label-value heuristics (same approach as the fund-level fields),
not fixed CSS classes, and every row is kept in `raw_labels` for tuning.

Also present: a "Key Information" / "Performance" / "Documents" tab
switcher above the table (Key Information is the default/active tab and
is what this script scrapes; Performance/Documents are left as an
extension point -- see SCRAPE_DOCUMENTS_TAB below).

CONFIG YOU MAY NEED TO ADJUST (top of file)
--------------------------------------------
- CLIENT_PROFILE / COUNTRY_ID: which gate option to submit. Only funds
  visible for that combination are captured. If your fund count looks
  low, try the other profile (see CLIENT_PROFILES_TO_TRY) or another
  country.
- SCRAPE_DOCUMENTS_TAB: if True, also click the "Documents" tab per fund
  to try to pick up factsheet/KIID PDF links (best-effort, off by default
  to keep the first run fast/simple).

USAGE
-----
    pip install playwright --break-system-packages
    playwright install chromium
    python Ossiam_extractor.py

Set OSSIAM_DEBUG=1 to dump the rendered listing HTML and a screenshot of
the first couple of expanded fund rows into a `debug/` folder, which is
the fastest way to tighten the extraction once you've seen real output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import async_playwright, Locator, Page
except ModuleNotFoundError as exc:  # pragma: no cover - runtime guidance for local usage
    raise ModuleNotFoundError(
        "playwright is required for the Ossiam scraper (the site is a JS app, "
        "not static HTML). Install it with 'pip install playwright' then "
        "'playwright install chromium'."
    ) from exc


ISSUER = "Ossiam"
BASE_URL = "https://www.ossiam.com"
LISTING_URL = f"{BASE_URL}/EN/fund"
NAV_TIMEOUT_MS = 45_000
GATE_TIMEOUT_MS = 8_000
EXPAND_WAIT_MS = 900

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Ossiam"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
DEBUG = os.environ.get("OSSIAM_DEBUG") == "1"

# --- TUNE ME: which gate option(s) to submit --------------------------
# Only one profile/country combo is scraped per run by default. If the
# fund range looks partial, add the other profile here and re-run --
# results get merged by ISIN (falling back to fund name) automatically.
CLIENT_PROFILES_TO_TRY = ["Professional client"]  # or add "Retail client"
COUNTRY_ID = "united kingdom"  # must match a country tile's id in the modal

# --- TUNE ME: also try to pull factsheet/KIID links from the "Documents"
# tab (adds one extra pass per fund; off by default to keep first runs fast)
SCRAPE_DOCUMENTS_TAB = False

ISIN_PATTERN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
SPACE_PATTERN = re.compile(r"\s+")

MAIN_TABLE_SELECTOR = "#pn_id_1"
MAIN_TABLE_ROW_SELECTOR = f"{MAIN_TABLE_SELECTOR} tbody > tr"

# --- TUNE ME: visible column-header / label text (lowercased) ->
# canonical field name, used both for the main table header row and for
# whatever the expanded row turns out to contain -----------------------
LABEL_ALIASES: dict[str, str] = {
    "fund": "fund_name",
    "expertise": "expertise",
    "strategy": "strategy",
    "asset class": "asset_class",
    "investment vehicle": "investment_vehicle",
    "aum": "aum_mn_raw",
    "assets under management": "aum_mn_raw",
    "fund size": "aum_mn_raw",
    "net assets": "aum_mn_raw",
    "sfdr classification": "sfdr_classification",
    "isin": "isin",
    "isin code": "isin",
    "currency": "ccy",
    "fund currency": "ccy",
    "trading currency": "ccy",
    "share class currency": "ccy",
    "ter": "ter_bps_raw",
    "total expense ratio": "ter_bps_raw",
    "ongoing charge": "ter_bps_raw",
    "ongoing charges": "ter_bps_raw",
    "nav": "nav",
    "nav per share": "nav",
    "net asset value": "nav",
    "nav date": "nav_date_raw",
    "launch date": "launch_date_raw",
    "inception date": "launch_date_raw",
    "listing date": "launch_date_raw",
    "domicile": "fund_domicile",
    "fund domicile": "fund_domicile",
    "index": "index_name",
    "underlying index": "index_name",
    "benchmark index": "index_name",
    "replication method": "replication_method",
    "replication": "replication_method",
    "distribution policy": "distribution_type",
    "dividend policy": "distribution_type",
    "income treatment": "distribution_type",
    "bloomberg": "bloomberg",
    "bloomberg ticker": "bloomberg",
    "reuters": "reuters",
    "reuters ticker": "reuters",
    "ticker": "primary_ticker",
    "primary ticker": "primary_ticker",
    "share class": "share_class",
}


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now()


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "ossiam_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "None", "null", "N/A"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def normalize_label(value: object | None) -> str:
    return clean_text(value).casefold().rstrip(":").strip()


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def percent_to_bps(value: str) -> str:
    cleaned = clean_text(value).replace("%", "").replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    if not cleaned:
        return ""
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return ""
    return format_decimal(amount * Decimal("100"), places=2)


def amount_to_millions(value: str) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    multiplier = Decimal("1")
    lowered = cleaned.lower()
    if "bn" in lowered or "billion" in lowered or "milliard" in lowered:
        multiplier = Decimal("1000")
    elif re.search(r"\bk\b", lowered) and "m" not in lowered:
        multiplier = Decimal("0.001")
    digits = re.sub(r"[^\d.,]", "", cleaned).replace(",", "")
    if not digits:
        return ""
    try:
        amount = Decimal(digits)
    except InvalidOperation:
        return ""
    if multiplier == Decimal("1") and amount > Decimal("100000"):
        # Raw units (e.g. "155,000,000") rather than millions -- scale down.
        amount = amount / Decimal("1000000")
    else:
        amount = amount * multiplier
    return format_decimal(amount, places=2)


def parse_date(value: str) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def map_known_fields(raw_labels: dict[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for label, value in raw_labels.items():
        key = LABEL_ALIASES.get(normalize_label(label))
        if key and key not in mapped:
            mapped[key] = value
    return mapped


# --------------------------------------------------------------------------
# Gate handling (cookie banner + investor profile / country modal)
# --------------------------------------------------------------------------


async def dismiss_cookie_banner(page: Page) -> None:
    try:
        button = page.locator("button.gdpr-accept", has_text="Accept").first
        if await button.is_visible(timeout=GATE_TIMEOUT_MS):
            await button.click(timeout=3_000)
            await page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001 - best effort only
        logging.info("Cookie banner not found/clickable (may already be dismissed).")


async def complete_investor_gate(page: Page, profile_label: str, country_id: str) -> bool:
    """Submit the 'Select your profile' modal. Returns True if it looked
    like the modal was present and submitted, False if no modal was found
    (e.g. it was already dismissed for this session)."""
    modal_heading = page.locator("h4", has_text="Select your profile").first
    try:
        if not await modal_heading.is_visible(timeout=GATE_TIMEOUT_MS):
            return False
    except Exception:  # noqa: BLE001
        return False

    # 1) client profile radio (rendered as a styled <label> wrapping the input)
    try:
        profile_option = page.locator("#clientProfileForm label", has_text=profile_label).first
        await profile_option.click(timeout=3_000)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not select client profile %r: %s", profile_label, exc)

    # 2) country tile -- rendered as <div class="fbox-icon" id="{country}"><a>...
    try:
        country_option = page.locator(f'[id="{country_id}"] a').first
        await country_option.click(timeout=3_000)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not select country %r: %s", country_id, exc)

    await page.wait_for_timeout(300)

    # 3) submit
    try:
        agree_button = page.locator("button.tab-action-btn-next", has_text="Agree").first
        await agree_button.click(timeout=5_000)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not click the agree/submit button: %s", exc)
        return True  # modal was present even if submit failed

    # Wait for the modal to actually go away.
    try:
        await modal_heading.wait_for(state="hidden", timeout=GATE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        pass
    return True


async def wait_for_fund_table_ready(page: Page) -> None:
    """Wait until the table has at least one real fund row (i.e. the
    'There are no share Classes for this fund yet.' placeholder is gone)."""
    try:
        await page.wait_for_function(
            """(sel) => {
                const rows = Array.from(document.querySelectorAll(sel));
                return rows.some(r => !r.querySelector('td[colspan]'));
            }""",
            arg=MAIN_TABLE_ROW_SELECTOR,
            timeout=NAV_TIMEOUT_MS,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "The fund table never populated with real rows after completing "
            "the investor-profile gate. Either the gate wasn't actually "
            "submitted (check CLIENT_PROFILES_TO_TRY / COUNTRY_ID match a "
            "real option) or ossiam.com changed its flow -- check the "
            "OSSIAM_DEBUG=1 diagnostics dump."
        ) from exc


# --------------------------------------------------------------------------
# Fund table parsing
# --------------------------------------------------------------------------


async def get_main_table_headers(page: Page) -> list[str]:
    header_texts = await page.locator(f"{MAIN_TABLE_SELECTOR} thead th").all_inner_texts()
    return [clean_text(text) for text in header_texts]


async def extract_generic_table_pairs(scope: Locator) -> list[dict[str, str]]:
    """Within `scope`, find any <table> and return one dict per body row,
    keyed by the header text of each column. Falls back to an empty list
    if no table is present."""
    results: list[dict[str, str]] = []
    tables = scope.locator("table")
    table_count = await tables.count()
    for t in range(table_count):
        table = tables.nth(t)
        headers = [clean_text(h) for h in await table.locator("thead th").all_inner_texts()]
        headers = [h for h in headers if h]
        body_rows = table.locator("tbody tr")
        row_count = await body_rows.count()
        for r in range(row_count):
            row = body_rows.nth(r)
            cells = [clean_text(c) for c in await row.locator("td").all_inner_texts()]
            if not any(cells):
                continue
            if headers and len(cells) >= len(headers):
                results.append(dict(zip(headers, cells)))
            elif headers:
                padded = cells + [""] * (len(headers) - len(cells))
                results.append(dict(zip(headers, padded)))
            else:
                # No header row available -- keep positional keys so the
                # data isn't silently dropped; raw_labels will still show it.
                results.append({f"col_{i}": v for i, v in enumerate(cells)})
    return results


async def extract_generic_label_value_pairs(scope: Locator) -> dict[str, str]:
    """Within `scope`, collect dt/dd pairs and generic two-child divs, as a
    fallback for when the expanded content isn't a plain <table>."""
    pairs: dict[str, str] = {}

    dt_texts = await scope.locator("dt").all_inner_texts()
    dd_texts = await scope.locator("dd").all_inner_texts()
    for label, value in zip(dt_texts, dd_texts):
        label, value = clean_text(label), clean_text(value)
        if label and value:
            pairs.setdefault(label, value)

    try:
        generic_rows = await scope.evaluate(
            """el => Array.from(el.querySelectorAll('div'))
                .filter(d => d.children.length === 2)
                .map(d => Array.from(d.children).map(c => c.innerText.trim()))
                .filter(pair => pair[0] && pair[1] && pair[0].length < 60 && pair[0] !== pair[1])"""
        )
    except Exception:  # noqa: BLE001
        generic_rows = []
    for pair in generic_rows:
        if len(pair) != 2:
            continue
        label, value = clean_text(pair[0]), clean_text(pair[1])
        if label and value and len(label) < 60:
            pairs.setdefault(label, value)

    return pairs


async def expand_fund_row(page: Page, row_index: int) -> Locator | None:
    """Click the toggle in the given (0-based) fund row's first column and
    return a Locator scoped to the newly revealed expansion row(s), or None
    if expansion didn't visibly add any rows."""
    all_rows = page.locator(MAIN_TABLE_ROW_SELECTOR)
    before_count = await all_rows.count()
    target_row = all_rows.nth(row_index)

    toggle = target_row.locator("td").first.locator("button, .p-row-toggler, [class*='toggler']").first
    try:
        if await toggle.count() > 0:
            await toggle.click(timeout=3_000)
        else:
            await target_row.locator("td").first.click(timeout=3_000)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Row %d: could not click expand toggle: %s", row_index, exc)
        return None

    await page.wait_for_timeout(EXPAND_WAIT_MS)
    after_count = await all_rows.count()
    added = after_count - before_count
    if added <= 0:
        return None

    # Assume the expansion content was inserted immediately after the
    # toggled row (standard PrimeNG row-expansion behaviour).
    return page.locator(
        f"{MAIN_TABLE_ROW_SELECTOR}:nth-child(n+{row_index + 2}):nth-child(-n+{row_index + 1 + added})"
    )


async def collapse_fund_row(page: Page, row_index: int) -> None:
    all_rows = page.locator(MAIN_TABLE_ROW_SELECTOR)
    target_row = all_rows.nth(row_index)
    toggle = target_row.locator("td").first.locator("button, .p-row-toggler, [class*='toggler']").first
    try:
        if await toggle.count() > 0:
            await toggle.click(timeout=3_000)
        else:
            await target_row.locator("td").first.click(timeout=3_000)
        await page.wait_for_timeout(400)
    except Exception:  # noqa: BLE001
        pass  # non-fatal; next expand_fund_row call will just see extra rows


def build_fund_level_fields(main_row_cells: dict[str, str]) -> dict[str, str]:
    mapped = map_known_fields(main_row_cells)
    return {
        "fund_name": clean_text(mapped.get("fund_name")),
        "expertise": clean_text(mapped.get("expertise")),
        "strategy": clean_text(mapped.get("strategy")),
        "asset_class": clean_text(mapped.get("asset_class")),
        "investment_vehicle": clean_text(mapped.get("investment_vehicle")),
        "aum_mn_table": amount_to_millions(mapped.get("aum_mn_raw", "")),
        "sfdr_classification": clean_text(mapped.get("sfdr_classification")),
    }


def build_share_class_row(fund_fields: dict[str, str], raw_labels: dict[str, str]) -> dict[str, Any]:
    mapped = map_known_fields(raw_labels)
    return {
        **fund_fields,
        "issuer": ISSUER,
        "isin": normalize_isin(mapped.get("isin", "")),
        "ccy": clean_text(mapped.get("ccy")),
        "ter_bps": percent_to_bps(mapped.get("ter_bps_raw", "")),
        "aum_mn": amount_to_millions(mapped.get("aum_mn_raw", "")) or fund_fields.get("aum_mn_table", ""),
        "nav": clean_text(mapped.get("nav")),
        "nav_date": parse_date(mapped.get("nav_date_raw", "")),
        "launch_date": parse_date(mapped.get("launch_date_raw", "")),
        "fund_domicile": clean_text(mapped.get("fund_domicile")),
        "index_name": clean_text(mapped.get("index_name")),
        "replication_method": clean_text(mapped.get("replication_method")),
        "distribution_type": clean_text(mapped.get("distribution_type")),
        "bloomberg": clean_text(mapped.get("bloomberg")),
        "reuters": clean_text(mapped.get("reuters")),
        "primary_ticker": clean_text(mapped.get("primary_ticker")),
        "share_class": clean_text(mapped.get("share_class")),
        "management_company": "Ossiam",
        "product_page_url": LISTING_URL,
        "fetch_status": "ok" if mapped.get("isin") else "expanded_no_isin",
        "raw_labels": raw_labels,
    }


async def scrape_all_funds(page: Page, debug_dir: Path | None) -> list[dict[str, Any]]:
    headers = await get_main_table_headers(page)
    logging.info("Main table headers: %s", headers)

    all_rows_locator = page.locator(MAIN_TABLE_ROW_SELECTOR)
    total_rows = await all_rows_locator.count()

    # Identify which of the initial rows are real fund rows (not the
    # 'no share classes' placeholder) up front, since the DOM shifts as we
    # expand/collapse.
    fund_row_count = 0
    for i in range(total_rows):
        cells = await all_rows_locator.nth(i).locator("td[colspan]").count()
        if cells == 0:
            fund_row_count += 1
    logging.info("Detected %d fund row(s) in the table", fund_row_count)

    output_rows: list[dict[str, Any]] = []

    for i in range(fund_row_count):
        main_cells_raw = await page.locator(MAIN_TABLE_ROW_SELECTOR).nth(i).locator("td").all_inner_texts()
        main_cells_raw = [clean_text(c) for c in main_cells_raw]
        main_row_dict = dict(zip(headers, main_cells_raw)) if headers else {}
        fund_fields = build_fund_level_fields(main_row_dict)
        fund_label = fund_fields.get("fund_name") or f"row_{i}"
        logging.info("[%d/%d] Expanding: %s", i + 1, fund_row_count, fund_label)

        expansion = await expand_fund_row(page, i)
        if expansion is None:
            logging.warning("  -> no expansion content detected for %s", fund_label)
            output_rows.append(
                {
                    **fund_fields,
                    "issuer": ISSUER,
                    "isin": "",
                    "ccy": "",
                    "ter_bps": "",
                    "aum_mn": fund_fields.get("aum_mn_table", ""),
                    "nav": "",
                    "nav_date": "",
                    "launch_date": "",
                    "fund_domicile": "",
                    "index_name": "",
                    "replication_method": "",
                    "distribution_type": "",
                    "bloomberg": "",
                    "reuters": "",
                    "primary_ticker": "",
                    "share_class": "",
                    "management_company": "Ossiam",
                    "product_page_url": LISTING_URL,
                    "fetch_status": "expand_failed",
                    "raw_labels": {},
                }
            )
            continue

        if debug_dir is not None and i < 3:
            try:
                safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", fund_label)[:80]
                # `expansion` may match multiple rows; dump each.
                count = await expansion.count()
                chunks = []
                for j in range(count):
                    chunks.append(await expansion.nth(j).evaluate("el => el.outerHTML"))
                (debug_dir / f"expansion_{i}_{safe_name}.html").write_text(
                    "\n".join(chunks), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001
                pass

        share_class_rows = await extract_generic_table_pairs(expansion)
        if share_class_rows:
            for raw_labels in share_class_rows:
                output_rows.append(build_share_class_row(fund_fields, raw_labels))
        else:
            raw_labels = await extract_generic_label_value_pairs(expansion)
            if raw_labels:
                output_rows.append(build_share_class_row(fund_fields, raw_labels))
            else:
                text_content = clean_text(await expansion.first.inner_text()) if await expansion.count() else ""
                isins_in_text = sorted(set(ISIN_PATTERN.findall(text_content)))
                output_rows.append(
                    {
                        **fund_fields,
                        "issuer": ISSUER,
                        "isin": isins_in_text[0] if isins_in_text else "",
                        "ccy": "",
                        "ter_bps": "",
                        "aum_mn": fund_fields.get("aum_mn_table", ""),
                        "nav": "",
                        "nav_date": "",
                        "launch_date": "",
                        "fund_domicile": "",
                        "index_name": "",
                        "replication_method": "",
                        "distribution_type": "",
                        "bloomberg": "",
                        "reuters": "",
                        "primary_ticker": "",
                        "share_class": "",
                        "management_company": "Ossiam",
                        "product_page_url": LISTING_URL,
                        "fetch_status": "expanded_no_table" if not text_content else "expanded_text_only",
                        "raw_labels": {"_raw_text": text_content[:2000]},
                    }
                )

        await collapse_fund_row(page, i)

    return output_rows


async def build_snapshot(output_dir: Path) -> dict[str, Any]:
    debug_dir = None
    if DEBUG:
        debug_dir = output_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

    merged_rows: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    profiles_run: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        for profile in CLIENT_PROFILES_TO_TRY:
            logging.info("=== Scraping with client profile: %s / country: %s ===", profile, COUNTRY_ID)
            await page.goto(LISTING_URL, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(1_000)

            await dismiss_cookie_banner(page)
            gate_present = await complete_investor_gate(page, profile, COUNTRY_ID)
            if not gate_present:
                logging.info("No investor-profile modal appeared (already satisfied for this session).")
            profiles_run.append(profile)

            wait_for_fund_table_ready_error: Exception | None = None
            try:
                await wait_for_fund_table_ready(page)
            except Exception as exc:  # noqa: BLE001
                wait_for_fund_table_ready_error = exc

            if debug_dir is not None:
                safe_profile = re.sub(r"[^a-zA-Z0-9]+", "_", profile)
                try:
                    (debug_dir / f"listing_{safe_profile}.html").write_text(
                        await page.content(), encoding="utf-8"
                    )
                    await page.screenshot(
                        path=str(debug_dir / f"listing_{safe_profile}.png"), full_page=True
                    )
                except Exception:  # noqa: BLE001
                    pass

            if wait_for_fund_table_ready_error is not None:
                raise wait_for_fund_table_ready_error

            profile_rows = await scrape_all_funds(page, debug_dir)
            for row in profile_rows:
                key = row.get("isin") or f"noisin::{row.get('fund_name', '')}"
                if key not in merged_rows:
                    merged_rows[key] = row
                status_counts[row["fetch_status"]] = status_counts.get(row["fetch_status"], 0) + 1

        await browser.close()

    all_rows = list(merged_rows.values())

    return {
        "source": {
            "provider": ISSUER,
            "listing_url": LISTING_URL,
            "base_url": BASE_URL,
            "client_profiles_scraped": profiles_run,
            "country_selected": COUNTRY_ID,
        },
        "method": (
            "Headless-browser (Playwright) rendering of the ossiam.com fund "
            "list, submitting the site's cookie banner and investor-profile/"
            "country gate, then expanding each row of the PrimeNG fund table "
            "to extract per-share-class fields (ISIN, currency, TER, NAV, "
            "etc.) via generic table/label-value heuristics. See raw_labels "
            "on each row for everything captured verbatim, and tune "
            "LABEL_ALIASES / CLIENT_PROFILES_TO_TRY / COUNTRY_ID at the top "
            "of this script if fields are missing or the fund count looks "
            "incomplete."
        ),
        "captured_at": timestamp_now().isoformat(),
        "fund_row_count": len(all_rows),
        "status_counts": status_counts,
        "listing_rows": all_rows,
    }


def download_snapshot(output_path: Path) -> Path:
    snapshot = asyncio.run(build_snapshot(output_path.parent))
    write_json(output_path, snapshot)
    logging.info("Ossiam fetch summary: %s", snapshot.get("status_counts", {}))
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved : %s", output_path)
    return output_path


async def download_ossiam_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def main() -> None:
    setup_logging()
    now = timestamp_now()
    output_path = build_output_path(now)
    download_snapshot(output_path)
    print(f"Saved Ossiam snapshot to: {output_path}")


if __name__ == "__main__":
    main()
