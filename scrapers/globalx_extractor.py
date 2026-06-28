"""Download Global X Europe ETF data — fully headless, no modal popup.

Strategy
--------
Pre-inject the three cookies the site sets after a user picks United Kingdom
+ institutional investor type. The modal never appears, and the fund API
call fires immediately on page load.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Response, async_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
START_URL  = "https://globalxetfs.eu/explore"
ISSUER     = "Global X ETFs"
PROVIDER   = "Global X ETFs"

BASE_DIR   = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "globalx"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
TIMEOUT_MS = 120_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Cookies that tell the site "returning UK institutional visitor, already agreed to T&C"
GATE_COOKIES = [
    {"name": "gx_iso_code",            "value": "UK",           "domain": "globalxetfs.eu", "path": "/"},
    {"name": "gx_investor_type",       "value": "institutional", "domain": "globalxetfs.eu", "path": "/"},
    {"name": "gx_terms_and_conditions","value": "true",          "domain": "globalxetfs.eu", "path": "/"},
]

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
DATE_RE = re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b")
FUND_URL_HINTS = ("fund", "etf", "explore", "product", "nav", "holding")

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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "globalx_etf_export.json"

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def clean(v: Any) -> str:
    if v is None: return ""
    s = re.sub(r"\s+", " ", str(v).replace("\u00ad","").replace("\u00a0"," ").replace("Â","").strip())
    return "" if s in {"","-","--","- "," -","None"} else s

def fmt_dec(v: Decimal, places: int = 2) -> str:
    return format(v.quantize(Decimal("1."+"0"*places), rounding=ROUND_HALF_UP), f".{places}f")

def ter_bps(raw: str) -> str:
    s = re.sub(r"[^0-9,.\-]", "", clean(raw).replace("%",""))
    if not s: return ""
    if "," in s and "." not in s: s = s.replace(",",".")
    try: return fmt_dec(Decimal(s) * 100)
    except InvalidOperation: return ""

def detect_ccy(raw: str) -> str:
    t = clean(raw).upper()
    if "$" in t: return "USD"
    if "€" in t: return "EUR"
    if "£" in t: return "GBP"
    for c in ("USD","EUR","GBP","CHF"):
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
        mul = Decimal("1e9"); n = re.sub(r"(BN|B)$","",n,flags=re.I).strip()
    elif u.endswith("MN") or u.endswith("M"):
        mul = Decimal("1e6"); n = re.sub(r"(MN|M)$","",n,flags=re.I).strip()
    n = re.sub(r"[^0-9.\-]","",n)
    if not n: return "", ccy
    try:
        m = Decimal(n) * mul / Decimal("1e6")
        return (format(m,"f").rstrip("0").rstrip(".") or "0"), ccy
    except InvalidOperation:
        return "", ccy

def norm_date(raw: str) -> str:
    s = clean(raw)
    if not s: return ""
    m = DATE_RE.search(s)
    if m: s = m.group(0)
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try: return datetime.strptime(s, fmt).strftime("%d/%m/%Y 00:00:00")
        except ValueError: pass
    return s

# ---------------------------------------------------------------------------
# API response detection + mapping
# ---------------------------------------------------------------------------
def _is_fund_list(obj: Any) -> bool:
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        keys = {k.lower() for k in obj[0]}
        return bool(keys & {"isin","ticker","nav","ter","aum","netassets","ongoingcharges"})
    if isinstance(obj, dict):
        return any(_is_fund_list(v) for v in obj.values())
    return False

def _extract_funds(obj: Any) -> list[dict]:
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        keys = {k.lower() for k in obj[0]}
        if keys & {"isin","ticker","nav","netassets"}: return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = _extract_funds(v)
            if r: return r
    return []

def _g(d: dict, *keys: str) -> str:
    for k in keys:
        for c in (k, k.lower(), k.upper(), k[0].upper()+k[1:], k.replace("_",""), k.replace("-","")):
            if c in d: return clean(d[c])
    return ""

def map_row(raw: dict, scraped_at: str) -> dict[str, str]:
    isin    = _g(raw,"isin","primaryIsin","primary_isin","ISIN")
    ticker  = _g(raw,"ticker","primaryTicker","primary_ticker","symbol")
    name    = _g(raw,"name","etfName","etf_name","fundName","shortName","displayName")
    aum_raw = _g(raw,"netAssets","net_assets","aum","totalAssets","total_assets")
    nav_raw = _g(raw,"nav","NAV","navPerShare")
    ter_raw = _g(raw,"ongoingCharges","ongoing_charges","ter","TER","expenseRatio","ocf","OCF")
    sfdr    = _g(raw,"sfdr","sfdrClassification","article","Article")
    inc     = _g(raw,"inceptionDate","inception_date","inception","listingDate")
    as_of   = _g(raw,"asOf","as_of","asOfDate","navDate","date")
    aum_m, ccy = aum_millions(aum_raw)
    if not ccy:
        ccy = _g(raw,"currency","ccy","CCY","baseCurrency","tradingCurrency")
    return {
        "provider": PROVIDER, "issuer": ISSUER,
        "etf_name": name, "ticker": ticker,
        "isin": isin.upper() if isin else "",
        "net_assets_raw": aum_raw,
        "aum_numeric": aum_m, "aum_m": aum_m,
        "aum_currency": ccy, "ccy": ccy,
        "nav_raw": nav_raw,
        "as_of_date": as_of, "date": norm_date(as_of),
        "sfdr_classification": sfdr,
        "ongoing_charges_raw": ter_raw, "ter_bps": ter_bps(ter_raw),
        "inception": inc,
        "source_url": START_URL, "scraped_at": scraped_at,
    }

def dedupe(rows: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for r in rows:
        k = clean(r.get("isin","")).upper()
        if k: seen.setdefault(k, r)
    return list(seen.values())

# ---------------------------------------------------------------------------
# DOM text fallback
# ---------------------------------------------------------------------------
def _is_money(v: str) -> bool:
    t = clean(v).upper()
    return (any(s in t for s in ("$","€","£")) or
            any(c in t for c in ("USD","EUR","GBP","CHF"))) and bool(re.search(r"\d",t))

def _is_net_assets(v: str) -> bool:
    t = clean(v).upper()
    return _is_money(t) and ("," in t or "BN" in t or "MN" in t or
                              bool(re.search(r"\d+\.\d+\s*[BM]$", t)))

def _parse_text(text: str, scraped_at: str) -> list[dict]:
    lines = [clean(l) for l in text.splitlines() if clean(l)]
    rows = []
    for i, line in enumerate(lines):
        isin = line.upper()
        if not ISIN_RE.fullmatch(isin): continue
        ticker   = lines[i-2].upper() if i >= 2 else ""
        etf_name = lines[i-1]         if i >= 1 else ""
        seg = lines[i+1:min(len(lines), i+16)]
        mc = [v for v in seg if _is_money(v)]
        aum_raw = next((v for v in mc if _is_net_assets(v)), mc[0] if mc else "")
        nav_raw = ""
        if aum_raw and aum_raw in seg:
            after = seg[seg.index(aum_raw)+1:]
            nc = [v for v in after if _is_money(v)]
            nav_raw = nc[0] if nc else ""
        dates = [DATE_RE.search(v).group(0) for v in seg if DATE_RE.search(v)]
        sfdr  = next((v for v in seg if "Article" in v), "")
        ter_r = next((v for v in seg if "%" in v), "")
        aum_m, ccy = aum_millions(aum_raw)
        rows.append({
            "provider": PROVIDER, "issuer": ISSUER,
            "etf_name": etf_name, "ticker": ticker, "isin": isin,
            "net_assets_raw": aum_raw, "aum_numeric": aum_m, "aum_m": aum_m,
            "aum_currency": ccy, "ccy": ccy, "nav_raw": nav_raw,
            "as_of_date": dates[0] if dates else "",
            "date": norm_date(dates[0] if dates else ""),
            "sfdr_classification": sfdr,
            "ongoing_charges_raw": ter_r, "ter_bps": ter_bps(ter_r),
            "inception": dates[1] if len(dates) > 1 else "",
            "source_url": START_URL, "scraped_at": scraped_at,
        })
    return dedupe(rows)

# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------
async def scrape(scraped_at: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
        )

        # ── inject gate cookies so the modal never appears ──────────────────
        await context.add_cookies(GATE_COOKIES)
        print("[1] Gate cookies injected (UK institutional, T&C accepted)")

        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

        # ── intercept fund-list API responses ───────────────────────────────
        intercepted: list[dict] = []

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
            if not _is_fund_list(body):
                return
            funds = _extract_funds(body)
            if funds:
                print(f"[intercept] {len(funds)} funds from {resp.url[:90]}")
                intercepted.extend(funds)

        page.on("response", on_response)

        # ── navigate ─────────────────────────────────────────────────────────
        print(f"[2] Navigating to {START_URL}")
        await page.goto(START_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)

        # ── wait up to 30 s for the API call ─────────────────────────────────
        print("[3] Waiting for fund data...")
        for tick in range(30):
            await page.wait_for_timeout(1_000)
            await page.evaluate("window.scrollBy(0,300)")
            if intercepted:
                print(f"    Got API data after {tick+1}s ({len(intercepted)} raw records)")
                break
            # also check if ISINs appeared in DOM text
            try:
                txt = await page.locator("body").inner_text(timeout=2_000)
                if re.search(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', txt):
                    print(f"    ISINs in DOM after {tick+1}s — using text fallback")
                    await browser.close()
                    return _parse_text(txt, scraped_at)
            except Exception:
                pass
        else:
            print("[warn] No fund data after 30s — trying DOM text fallback")
            try:
                txt = await page.locator("body").inner_text(timeout=TIMEOUT_MS)
                await browser.close()
                return _parse_text(txt, scraped_at)
            except Exception:
                await browser.close()
                return []

        await browser.close()

    rows = [map_row(r, scraped_at) for r in intercepted]
    return dedupe(rows)

# ---------------------------------------------------------------------------
# Snapshot + I/O
# ---------------------------------------------------------------------------
def print_summary(rows: list[dict]) -> None:
    miss = lambda f: sum(1 for r in rows if not clean(r.get(f,"")))
    print(f"Source URL used:    {START_URL}")
    print(f"Raw rows extracted: {len(rows):,}")
    print(f"Unique ISINs:       {len({r['isin'] for r in rows if r.get('isin')}):,}")
    for f, l in [("etf_name","ETF Name"),("isin","ISIN"),("ccy","CCY"),
                  ("ter_bps","TER(bps)"),("aum_m","AUM(M)"),("date","Date")]:
        print(f"  Missing {l}: {miss(f):,}")

async def build_snapshot(now: datetime) -> dict:
    scraped_at = now.isoformat()
    rows = await scrape(scraped_at)
    print_summary(rows)
    return {
        "source_url": START_URL,
        "method": "Global X explore — cookie gate bypass + API interception",
        "captured_at": scraped_at,
        "row_count": len(rows),
        "rows": rows,
    }

def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
async def download_globalx_file() -> Path:
    now = datetime.now()
    output_path = build_output_path(now)
    snapshot = await build_snapshot(now)
    write_json(output_path, snapshot)
    return output_path
def main() -> None:
    output_path = asyncio.run(download_globalx_file())
    print(f"Raw snapshot saved: {output_path}")
    print(f"Done! Open your file at: {output_path.resolve()}")
if __name__ == "__main__":
    main()
