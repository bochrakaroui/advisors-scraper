# scrapers/LandG_extractor.py
import asyncio
import re
import argparse
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

URL = (
    "https://fundcentres.landg.com/en/uk/adviser-wealth/fund-centre/etf/"
    "?exchangeName=London+Stock+Exchange"
)

PAGE_LOAD_TIMEOUT = 45_000
ELEMENT_TIMEOUT   = 20_000
DOWNLOAD_TIMEOUT  = 90_000


def evaluate_with_retry(page, script: str, arg=None, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            if arg is None:
                return page.evaluate(script)
            return page.evaluate(script, arg)
        except Exception as exc:  # noqa: BLE001
            if "Execution context was destroyed" not in str(exc) or attempt >= retries:
                raise
            page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            page.wait_for_timeout(500)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download L&G ETF 'All data' export.")
    parser.add_argument(
        "--headed", action="store_true", default=False,
        help="Show the browser window (default: headless).",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# FOLDER
# ─────────────────────────────────────────────────────────────────────────────

def build_save_dir() -> Path:
    today = date.today().isoformat()
    save_dir = Path("providers") / "landg" / today
    save_dir.mkdir(parents=True, exist_ok=True)
    return save_dir


# ─────────────────────────────────────────────────────────────────────────────
# COOKIE / GATE DISMISSAL
# Dispatches a real MouseEvent via JS so HeadlessUI registers it properly.
# ─────────────────────────────────────────────────────────────────────────────

def dismiss_cookie_banner(page) -> None:
    """
    Finds and clicks the OneTrust 'Accept all cookies' button.
    Uses JS dispatchEvent so the click is registered even in headless mode.
    """
    print("  Dismissing cookie banner...")

    # From debug: 'Accept all cookies (Recommended) @(835,744)'
    # Use the OneTrust ID first (most reliable), then text fallback
    dismissed = evaluate_with_retry(page, """
        () => {
            // Strategy 1: OneTrust button by ID
            let btn = document.querySelector('#onetrust-accept-btn-handler');

            // Strategy 2: text contains 'Accept all cookies'
            if (!btn) {
                btn = Array.from(document.querySelectorAll('button')).find(b =>
                    b.innerText.toLowerCase().includes('accept all cookies')
                );
            }

            // Strategy 3: text contains 'Accept all'
            if (!btn) {
                btn = Array.from(document.querySelectorAll('button')).find(b =>
                    b.innerText.trim().toLowerCase() === 'accept all'
                );
            }

            if (!btn) return 'NOT_FOUND';

            // Dispatch real mouse events so OneTrust actually processes the click
            ['mousedown','mouseup','click'].forEach(type => {
                btn.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true}));
            });
            return 'CLICKED: ' + btn.innerText.trim().slice(0, 60);
        }
    """)
    print(f"  → Cookie: {dismissed}")
    if dismissed and dismissed.startswith("CLICKED"):
        page.wait_for_timeout(1_500)


def dismiss_investor_gate(page) -> None:
    """
    Dismisses any investor type / T&C / country modal that appears.
    Tries common L&G gate button texts.
    """
    gate_texts = [
        "i confirm that i am",
        "i confirm",
        "confirm and proceed",
        "confirm and continue",
        "i am a professional investor",
        "professional investor",
        "i am an adviser",
        "adviser",
        "accept and continue",
        "i accept",
        "agree and continue",
        "agree",
        "proceed",
    ]
    result = evaluate_with_retry(page, """
        (texts) => {
            const btns = Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent !== null);
            for (const text of texts) {
                const btn = btns.find(b =>
                    b.innerText.trim().toLowerCase().includes(text)
                );
                if (btn) {
                    ['mousedown','mouseup','click'].forEach(type => {
                        btn.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true}));
                    });
                    return 'CLICKED: ' + btn.innerText.trim().slice(0,60);
                }
            }
            return 'NOT_FOUND';
        }
    """, gate_texts)

    if result and result.startswith("CLICKED"):
        print(f"  → Investor gate: {result}")
        page.wait_for_timeout(1_200)
    else:
        print("  → No investor gate found.")


# ─────────────────────────────────────────────────────────────────────────────
# WAIT FOR FUND LIST
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_fund_list(page) -> None:
    signals = [
        "button:has-text('Download data')",
        "text=L&G All Commodities",
        "a:has-text('UCITS ETF')",
    ]
    for sig in signals:
        try:
            page.wait_for_selector(sig, timeout=PAGE_LOAD_TIMEOUT)
            print(f"  ✓ Fund page confirmed ({sig!r}).")
            return
        except PlaywrightTimeoutError:
            continue
    print("  ⚠ Fund list not confirmed — check the page manually.")


# ─────────────────────────────────────────────────────────────────────────────
# OPEN DROPDOWN via JS dispatchEvent (not mouse coords)
# ─────────────────────────────────────────────────────────────────────────────

def click_download_button_via_js(page) -> None:
    """
    Clicks the 'Download data' button using JS dispatchEvent.
    This fires real-feeling browser events without needing coordinates,
    so HeadlessUI registers it and opens the dropdown.
    """
    result = evaluate_with_retry(page, """
        () => {
            const btn = Array.from(document.querySelectorAll('button')).find(b =>
                b.innerText.trim().toLowerCase().includes('download data') &&
                b.offsetParent !== null
            );
            if (!btn) return 'NOT_FOUND';
            btn.focus();
            ['mouseenter','mouseover','mousedown','mouseup','click'].forEach(type => {
                btn.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window
                }));
            });
            return 'CLICKED: ' + btn.innerText.trim().slice(0,40);
        }
    """)
    if not result or not result.startswith("CLICKED"):
        raise RuntimeError(f"'Download data' button not found via JS. Got: {result}")
    print(f"  ✓ 'Download data' JS dispatched: {result}")


# ─────────────────────────────────────────────────────────────────────────────
# WAIT FOR HEADLESSUI MENU
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_headlessui_menu(page) -> bool:
    """Returns True if the menu opened, False otherwise."""
    for selector, label in [
        ("[role='menu'][data-headlessui-state='open']", "HeadlessUI open state"),
        ("[role='menuitem']",                           "menuitem role"),
        ("text=All data",                               "'All data' text"),
    ]:
        try:
            page.wait_for_selector(selector, timeout=5_000)
            print(f"  ✓ Menu confirmed via: {label}")
            return True
        except PlaywrightTimeoutError:
            continue
    return False


# ─────────────────────────────────────────────────────────────────────────────
# CLICK "All data" via JS dispatchEvent
# ─────────────────────────────────────────────────────────────────────────────

def click_all_data_via_js(page) -> None:
    """
    Clicks the 'All data' menuitem using JS dispatchEvent.
    Must be called while the dropdown is still open.
    """
    result = evaluate_with_retry(page, """
        () => {
            // Prefer role=menuitem
            let item = Array.from(document.querySelectorAll('[role="menuitem"]')).find(el =>
                el.innerText.trim().toLowerCase().includes('all data')
            );
            // Fallback: any visible element with that text
            if (!item) {
                item = Array.from(document.querySelectorAll('button, li, a')).find(el =>
                    el.offsetParent !== null &&
                    el.innerText.trim().toLowerCase() === 'all data'
                );
            }
            if (!item) return 'NOT_FOUND';
            item.focus();
            ['mouseenter','mousedown','mouseup','click'].forEach(type => {
                item.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window
                }));
            });
            return 'CLICKED: ' + item.tagName + ' / ' + item.innerText.trim();
        }
    """)
    if not result or not result.startswith("CLICKED"):
        # Dump menuitems for diagnosis
        items = evaluate_with_retry(page, """
            () => Array.from(document.querySelectorAll('[role="menuitem"]'))
                .map(el => el.innerText.trim() + ' visible=' + (el.offsetParent !== null))
                .join(' | ')
        """)
        raise RuntimeError(
            f"'All data' menuitem not found. Got: {result}\n"
            f"Menuitems in DOM: {items}"
        )
    print(f"  ✓ 'All data' JS dispatched: {result}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def _download_landg_file(headless: bool = True) -> Path:
    today = date.today().isoformat()
    save_dir = build_save_dir()

    print("=" * 60)
    print("  L&G ETF Downloader")
    print("=" * 60)
    print(f"  URL      : {URL}")
    print(f"  Save dir : {save_dir}")
    print(f"  Headless : {headless}")
    print("=" * 60)

    with sync_playwright() as pw:

        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
        )

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()

        # ── 1. Open page ──────────────────────────────────────────────────────
        print("\n[1/6] Opening page…")
        page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_timeout(2_500)

        # ── 2. Cookie banner ──────────────────────────────────────────────────
        print("[2/6] Dismissing cookie banner…")
        dismiss_cookie_banner(page)

        # ── 3. Wait for fund list ─────────────────────────────────────────────
        print("[3/6] Waiting for fund list…")
        wait_for_fund_list(page)
        page.wait_for_timeout(1_000)

        # ── 4. Investor / T&C gate (may appear after cookie dismissal) ────────
        print("[4/6] Checking for investor gate…")
        dismiss_investor_gate(page)

        # ── 5. Open the dropdown OUTSIDE expect_download ──────────────────────
        print("[5/6] Opening 'Download data' dropdown…")
        evaluate_with_retry(page, "window.scrollTo(0, 0)")
        page.wait_for_timeout(300)

        click_download_button_via_js(page)

        # Wait for the HeadlessUI menu to open
        menu_open = wait_for_headlessui_menu(page)
        if not menu_open:
            # One retry — sometimes the first JS click doesn't register
            print("  ⚠ Menu didn't open — retrying click...")
            page.wait_for_timeout(800)
            click_download_button_via_js(page)
            menu_open = wait_for_headlessui_menu(page)
            if not menu_open:
                raise RuntimeError(
                    "Dropdown did not open after two attempts.\n"
                    "Run with --headed to inspect the page."
                )

        # ── 6. Click "All data" INSIDE expect_download ────────────────────────
        print("[6/6] Clicking 'All data' and capturing download…")
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as dl_info:
            click_all_data_via_js(page)

        download = dl_info.value
        print("  ✓ Download triggered.")

        suggested = download.suggested_filename
        suffix    = Path(suggested).suffix if suggested else ".xlsx"
        if not suffix:
            suffix = ".xlsx"

        final_name = f"landg_etf_export_{today}{suffix}"
        final_path = save_dir / final_name
        download.save_as(final_path)

        print()
        print("=" * 60)
        print("  ✅ Download complete!")
        print(f"  📁 {final_path}")
        print("=" * 60)

        context.close()
        browser.close()
        return final_path


async def download_landg_file(headless: bool = True) -> Path:
    return await asyncio.to_thread(_download_landg_file, headless)


def main() -> None:
    args = parse_args()
    _download_landg_file(headless=not args.headed)



if __name__ == "__main__":
    main()
