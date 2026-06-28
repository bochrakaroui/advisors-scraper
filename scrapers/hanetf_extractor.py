"""
HANetf ETF Scraper — Fixed v5
==============================
Key insight from repeated failures:
  The modal steps are ALL AJAX — no page navigation occurs at any point.
  The previous crashes were caused by a delayed navigation from a *different*
  source (e.g. the site's own JS redirecting after cookie/localStorage is set).

Strategy:
  - Never use expect_navigation or wait_for_load_state for modal steps.
  - After each click, wait for a concrete DOM change that confirms the step worked.
  - After "I Agree", wait for the modal element to disappear from the DOM
    (or become display:none), then add a generous pause before any evaluate().
  - Wrap the post-modal evaluate() in a retry loop that handles any
    residual navigation by waiting and retrying.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR.parent / "providers" / "hanetf"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

URL = "https://hanetf.com/product-list/?fund-structure=etf"
HEADLESS = True          # Set to False to watch the browser and debug visually
ISSUER = "HANetf"

CCY_MAP = {"$": "USD", "€": "EUR", "£": "GBP"}

OUTPUT_COLUMNS = ["ETF Name", "Issuer", "ISIN", "CCY", "TER(bps)", "AUM(M)", "Date"]

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_run_output_dir(base_dir: Path) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        run_date = datetime.now().strftime("%Y-%m-%d")
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {"", "-", "—", "–"} else text


def parse_ccy(raw_aum: str) -> str:
    val = clean_text(raw_aum)
    return CCY_MAP.get(val[0], "") if val else ""


def format_decimal(value: object | None, places: int = 2) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"[^\d.,-]", "", cleaned)
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    if not cleaned:
        return ""
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        return ""
    quantizer = Decimal("1." + "0" * places)
    return format(d.quantize(quantizer, rounding=ROUND_HALF_UP), f".{places}f")


def parse_aum_m(raw: str) -> str:
    val = clean_text(raw)
    if not val:
        return ""
    if val[0] in CCY_MAP:
        val = val[1:]
    return format_decimal(val, places=2)


def parse_ter_bps(raw: str) -> str:
    cleaned = clean_text(raw).replace("%", "").strip()
    if not cleaned:
        return ""
    try:
        return format_decimal(str(Decimal(cleaned) * Decimal("100")), places=2)
    except InvalidOperation:
        return ""


def is_valid_isin(value: str) -> bool:
    return bool(ISIN_RE.fullmatch(clean_text(value).upper()))


# ---------------------------------------------------------------------------
# JS snippets
# ---------------------------------------------------------------------------

_FIRE_CLICK_JS = r"""
(selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    ['mousedown','mouseup','click'].forEach(name =>
        el.dispatchEvent(new MouseEvent(name, {bubbles: true, cancelable: true}))
    );
    return true;
}
"""

_MODAL_GONE_JS = r"""
() => {
    const m = document.getElementById('region-investor-type-modal');
    if (!m) return true;
    const style = window.getComputedStyle(m);
    return style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0';
}
"""

_MODAL_VISIBLE_JS = r"""
() => {
    const m = document.getElementById('region-investor-type-modal');
    if (!m) return false;
    return window.getComputedStyle(m).display !== 'none';
}
"""

_FORCE_HIDE_MODAL_JS = r"""
() => {
    const m = document.getElementById('region-investor-type-modal');
    if (m) {
        m.style.setProperty('display', 'none', 'important');
        m.style.visibility = 'hidden';
        m.style.pointerEvents = 'none';
    }
}
"""

_STEP2_VISIBLE_JS = r"""
() => {
    const s = document.getElementById('step2');
    if (!s) return false;
    const style = window.getComputedStyle(s);
    return style.display !== 'none' && style.visibility !== 'hidden';
}
"""

_ACCEPT_VISIBLE_JS = r"""
() => {
    const w = document.getElementById('acceptTermsCondition');
    if (!w) return false;
    const style = window.getComputedStyle(w);
    return style.display !== 'none' && style.visibility !== 'hidden';
}
"""

_EXTRACT_ROWS_JS = r"""
() => {
    const results = [];
    let tbl = document.getElementById('DataTables_Table_0');
    if (!tbl) {
        let max = 0;
        for (const t of document.querySelectorAll('table')) {
            const n = t.querySelectorAll('tbody tr').length;
            if (n > max) { max = n; tbl = t; }
        }
    }
    if (!tbl) return results;

    for (const row of tbl.querySelectorAll('tbody tr')) {
        const cells = row.querySelectorAll('td');
        if (cells.length < 4) continue;
        const nameEl = cells[0].querySelector('a');
        const name   = (nameEl ? nameEl.innerText : cells[0].innerText).trim();
        const isin   = cells[2] ? cells[2].innerText.trim() : '';
        const aum    = (cells[4] || cells[3]).innerText.trim();
        const ter    = (cells[6] || cells[5] || {innerText:''}).innerText.trim();
        if (name) results.push({ name, isin, aum, ter });
    }
    return results;
}
"""

_TABLE_HAS_ROWS_JS = r"""
() => {
    const tbl = document.getElementById('DataTables_Table_0');
    if (!tbl) return false;
    return [...tbl.querySelectorAll('tbody tr')].some(r => r.querySelectorAll('td').length >= 4);
}
"""

_NEXT_PAGE_JS = r"""
() => {
    const selectors = [
        '#DataTables_Table_0_next',
        '[id$="_next"]',
        '.dataTables_paginate .next',
        'a.next',
        'li.next a',
    ];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (!btn) continue;
        if (btn.classList.contains('disabled') ||
            btn.parentElement?.classList.contains('disabled')) return 'disabled';
        btn.click();
        return 'clicked';
    }
    return 'no-button';
}
"""


# ---------------------------------------------------------------------------
# Safe evaluate — retries if context is temporarily destroyed by navigation
# ---------------------------------------------------------------------------
def safe_evaluate(page, js: str, arg=None, retries: int = 5, delay: float = 2.0):
    """
    Calls page.evaluate() and retries up to `retries` times if the execution
    context is destroyed mid-flight (caused by a background navigation).
    """
    from playwright._impl._errors import Error as PWError
    for attempt in range(retries):
        try:
            if arg is not None:
                return page.evaluate(js, arg)
            return page.evaluate(js)
        except (PWError, Exception) as e:
            if "Execution context was destroyed" in str(e) or "context" in str(e).lower():
                print(f"      WARN Context destroyed (attempt {attempt + 1}/{retries}), waiting {delay}s ...")
                time.sleep(delay)
                # Wait for page to stabilise after navigation
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    page.wait_for_timeout(1_000)
                except Exception:
                    pass
            else:
                raise
    raise RuntimeError(f"safe_evaluate failed after {retries} retries")


# ---------------------------------------------------------------------------
# Modal dismissal — pure AJAX, no navigation expected
# ---------------------------------------------------------------------------
def dismiss_modal(page) -> None:
    """
    All three modal steps are AJAX — the page does NOT navigate.
    We confirm each step by waiting for a DOM change, not a navigation event.

    If the site triggers a background redirect after cookies are set,
    safe_evaluate() handles the resulting context destruction gracefully.
    """

    # ── Step 1: force step1 visible, click UK ────────────────────────────────
    page.evaluate(r"() => { const s = document.getElementById('step1'); if(s) { s.style.display='block'; s.style.visibility='visible'; } }")
    page.wait_for_timeout(500)

    clicked = page.evaluate(_FIRE_CLICK_JS, 'li[data-country="uk-en"]')
    print(f"      Country clicked: {clicked}")

    # Wait for step2 to appear in the DOM (confirms the AJAX step completed)
    try:
        page.wait_for_function(_STEP2_VISIBLE_JS, timeout=10_000)
        print("      step2 visible OK")
    except PWTimeout:
        # Step2 may already be visible or hidden by CSS — force-show it
        print("      step2 not auto-visible - force-showing ...")
        page.evaluate(r"() => { const s = document.getElementById('step2'); if(s) { s.style.display='block'; s.style.visibility='visible'; } }")
    page.wait_for_timeout(500)

    # ── Step 2: click investor type ──────────────────────────────────────────
    clicked2 = page.evaluate(_FIRE_CLICK_JS, 'li[data-investor-type="institutional"]')
    print(f"      Investor type clicked: {clicked2}")

    # Wait for the T&C / accept section to appear
    try:
        page.wait_for_function(_ACCEPT_VISIBLE_JS, timeout=8_000)
        print("      Accept section visible OK")
    except PWTimeout:
        print("      Accept section not auto-visible - force-showing ...")
        page.evaluate(r"""
        () => {
            const w = document.getElementById('acceptTermsCondition');
            if (w) { w.style.display = 'block'; w.style.visibility = 'visible'; }
        }
        """)
    page.wait_for_timeout(500)

    # ── Step 3: click "I Agree" ───────────────────────────────────────────────
    clicked3 = page.evaluate(_FIRE_CLICK_JS, '.accept-button-pop')
    if not clicked3:
        clicked3 = page.evaluate(r"""
        () => {
            for (const el of document.querySelectorAll('span,a,button,div')) {
                const txt = (el.innerText||el.textContent||'').trim();
                if (/^i agree$/i.test(txt) && el.children.length < 3) {
                    ['mousedown','mouseup','click'].forEach(n =>
                        el.dispatchEvent(new MouseEvent(n,{bubbles:true,cancelable:true}))
                    );
                    return true;
                }
            }
            return false;
        }
        """)
    print(f"      I Agree clicked: {clicked3}")

    # Wait for modal to disappear — this is the definitive signal that the
    # flow completed. If a navigation fires here, the modal will be gone on
    # the new page too, so wait_for_function will succeed after reload.
    try:
        page.wait_for_function(_MODAL_GONE_JS, timeout=12_000)
        print("      Modal gone OK")
    except PWTimeout:
        print("      Modal did not auto-hide - force-hiding ...")
        # Use safe_evaluate in case context was destroyed by a navigation
        safe_evaluate(page, _FORCE_HIDE_MODAL_JS)

    # Extra settle time after modal dismissal
    page.wait_for_timeout(2_000)

    # ── Persist so soft-navigations don't re-show the modal ──────────────────
    safe_evaluate(page, r"""
    () => {
        document.cookie = 'hanetf_region=uk-en; path=/; max-age=86400';
        document.cookie = 'hanetf_investor_type=institutional; path=/; max-age=86400';
        try {
            localStorage.setItem('hanetf_region', 'uk-en');
            localStorage.setItem('hanetf_investor', 'institutional');
        } catch(_) {}
    }
    """)


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------
def scrape_hanetf() -> pd.DataFrame:
    records: list[dict[str, str]] = []
    today = datetime.now().strftime("%d/%m/%Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        context.set_extra_http_headers({
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        page = context.new_page()

        # ── 1. Load ──────────────────────────────────────────────────────────
        print("[1/4] Loading page ...")
        try:
            page.goto(URL, wait_until="commit", timeout=60_000)
        except PWTimeout:
            print("      WARN goto timed out - continuing anyway ...")

        try:
            page.wait_for_selector("body", timeout=15_000)
        except PWTimeout:
            print("      WARN body selector timed out")

        page.wait_for_timeout(4_000)
        print("      Page loaded.")

        # ── 2. Dismiss modal ─────────────────────────────────────────────────
        print("[2/4] Dismissing modal ...")
        dismiss_modal(page)

        # ── 3. Wait for DataTable ────────────────────────────────────────────
        print("[3/4] Waiting for DataTable ...")
        try:
            page.wait_for_function(_TABLE_HAS_ROWS_JS, timeout=25_000)
        except PWTimeout:
            print("      WARN Timed out - trying scroll trigger ...")
            page.evaluate("() => window.scrollTo(0, 500)")
            page.wait_for_timeout(3_000)

        # ── 4. Paginate + extract ────────────────────────────────────────────
        print("[4/4] Extracting rows ...")
        page_num = 0

        while True:
            page_num += 1
            print(f"  -> Page {page_num} ...", end=" ", flush=True)

            batch: list[dict] = page.evaluate(_EXTRACT_ROWS_JS)
            print(f"{len(batch)} rows")

            for row in batch:
                name    = clean_text(row.get("name", ""))
                isin    = clean_text(row.get("isin", "")).upper()
                aum_raw = clean_text(row.get("aum", ""))
                ter_raw = clean_text(row.get("ter", ""))

                if not name or not is_valid_isin(isin):
                    continue

                records.append({
                    "ETF Name": name,
                    "Issuer":   ISSUER,
                    "ISIN":     isin,
                    "CCY":      parse_ccy(aum_raw),
                    "TER(bps)": parse_ter_bps(ter_raw),
                    "AUM(M)":   parse_aum_m(aum_raw),
                    "Date":     today,
                })

            next_status: str = page.evaluate(_NEXT_PAGE_JS)
            print(f"      Next: {next_status}")
            if next_status in ("no-button", "disabled"):
                break

            page.wait_for_timeout(1_500)
            try:
                page.wait_for_function(_TABLE_HAS_ROWS_JS, timeout=10_000)
            except PWTimeout:
                print("      WARN Next page did not render - stopping.")
                break

        browser.close()

    df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    if not df.empty:
        before = len(df)
        df = df.drop_duplicates(subset=["ISIN"], keep="first")
        df = df[df["ISIN"].str.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", na=False)]
        print(f"\nDedup + ISIN filter: {len(df)} rows  (was {before})")

    return df


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export(df: pd.DataFrame) -> Path:
    output_dir = build_run_output_dir(OUTPUT_DIR)
    output_path = output_dir / f"hanetf_raw_{datetime.now().strftime('%Y-%m-%d')}.csv"
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print("=" * 60)
    print(f"  Rows : {len(df):,}")
    print(f"  File : {output_path}")
    print("=" * 60)
    return output_path


def _download_hanetf_file() -> Path:
    return export(scrape_hanetf())


async def download_hanetf_file() -> Path:
    return await asyncio.to_thread(_download_hanetf_file)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    df = scrape_hanetf()
    if df.empty:
        print("\nWARN No data captured.")
        print("   Tip: set HEADLESS = False at the top of the file to debug visually.")
    else:
        export(df)
        print("\nFirst 5 rows:")
        print(df.head().to_string(index=False))
