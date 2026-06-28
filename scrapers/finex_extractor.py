"""Download FinEx ETF data — listing page + per-product detail pages.

Enriches each row with:
  ccy               – fund base currency
  ter_bps           – total expense ratio in basis points (e.g. 0.90% → 90)
  aum_mn            – fund AUM in millions (raw value ÷ 1 000 000, rounded to 2 dp)
  nav               – unit NAV (latest)
  inception_date
  index_name
  replication_method
  annual_volatility
  tracking_error
  shares_in_issue
  detail_fetch_status – "ok" | "http_error:<code>" | "error:<type>" | "no_url"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PAGE_URL = "https://finexetf.com/product/"
ISSUER   = "FinEx"
DELAY_S  = 0.5   # polite pause between detail-page requests

BASE_DIR   = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "finex"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Output / path helpers
# ---------------------------------------------------------------------------

def build_run_output_dir(base_dir: Path, run_date: str) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


def timestamp_now() -> datetime:
    return datetime.now()


def build_output_path(now: datetime) -> Path:
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "finex_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").strip()
    return "" if cleaned in {"", "-", "--", "- ", " -"} else cleaned


def normalize_isin(value: str) -> str:
    return re.sub(r"\s+", "", clean_text(value).upper())


def ter_to_bps(raw: str) -> str:
    """Convert '0.90%' → '90'.  Returns '' if unparseable."""
    m = re.search(r"([\d.,]+)", raw.replace(",", "."))
    if not m:
        return ""
    try:
        return str(round(float(m.group(1)) * 100))
    except ValueError:
        return ""


def aum_raw_to_mn(raw: str) -> str:
    """Convert '331 800 098' (with any whitespace) → '331.8'.  Returns '' if unparseable."""
    digits = re.sub(r"[\s,]", "", raw)
    if not digits:
        return ""
    try:
        val = float(digits)
        # Return empty string for zero to distinguish "truly zero" from "not published"
        return "" if val == 0.0 else str(round(val / 1_000_000, 2))
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Listing-page scraper  (unchanged logic from original)
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def parse_table(
    soup: BeautifulSoup,
    table: BeautifulSoup,
    *,
    terminated: bool,
) -> list[dict[str, str]]:
    """Extract rows from a single <table> element on the listing page."""
    rows: list[dict[str, str]] = []

    headers: list[str] = []
    thead = table.find("thead")
    if thead:
        headers = [clean_text(th.get_text()).lower() for th in thead.find_all("th")]

    col_product = next((i for i, h in enumerate(headers) if "product" in h), 0)
    col_asset   = next((i for i, h in enumerate(headers) if "asset" in h), 1)
    col_isin    = next((i for i, h in enumerate(headers) if "isin" in h), 2)
    col_bbg     = next((i for i, h in enumerate(headers) if "bbg" in h or "ticker" in h), 3)

    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        def td_text(idx: int) -> str:
            return clean_text(tds[idx].get_text()) if idx < len(tds) else ""

        isin = normalize_isin(td_text(col_isin))
        if not isin:
            continue

        etf_name = td_text(col_product)
        href = ""
        if col_product < len(tds):
            a_tag = tds[col_product].find("a")
            if a_tag:
                etf_name = clean_text(a_tag.get_text())
                href = a_tag.get("href", "")

        product_url = (
            f"https://finexetf.com{href}" if href.startswith("/") else href
        )

        # Flag ISINs that look malformed (valid ISINs are always 12 chars)
        isin_note = "malformed_isin" if len(isin) != 12 else ""

        rows.append({
            "etf_name":            etf_name,
            "issuer":              ISSUER,
            "isin":                isin,
            "isin_note":           isin_note,
            "asset_class":         td_text(col_asset),
            "bbg_ticker":          td_text(col_bbg),
            "product_url":         product_url,
            "terminated":          "true" if terminated else "false",
            # populated by enrich_from_detail_page()
            "ccy":                 "",
            "ter_bps":             "",
            "aum_mn":              "",
            "nav":                 "",
            "inception_date":      "",
            "index_name":          "",
            "replication_method":  "",
            "annual_volatility":   "",
            "tracking_error":      "",
            "shares_in_issue":     "",
            # "ok" | "http_error:<code>" | "error:<type>" | "no_url"
            "detail_fetch_status": "pending",
        })

    return rows


def extract_listing_rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    all_rows: list[dict[str, str]] = []

    tables = soup.find_all("table")
    logging.info("Found %s table(s) on the listing page.", len(tables))

    for i, table in enumerate(tables):
        terminated = False
        for sibling in table.find_all_previous(["h2", "h3", "h4"]):
            if "terminat" in sibling.get_text().lower():
                terminated = True
                break

        rows = parse_table(soup, table, terminated=terminated)
        logging.info("Table %s: %s row(s) (terminated=%s).", i + 1, len(rows), terminated)
        all_rows.extend(rows)

    return all_rows


# ---------------------------------------------------------------------------
# Detail-page scraper
# ---------------------------------------------------------------------------

def _table_kv(soup: BeautifulSoup) -> dict[str, str]:
    """
    Walk every <table> on the page and collect key→value pairs from rows
    that have exactly two cells.  Keys are lowercased and stripped.
    """
    kv: dict[str, str] = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) == 2:
                key = clean_text(cells[0].get_text()).lower()
                val = clean_text(cells[1].get_text())
                if key and val:
                    kv[key] = val
    return kv


def enrich_from_detail_page(row: dict[str, str]) -> None:
    """
    Fetch the product detail page and populate the enrichment fields
    directly on *row* (mutates in place).

    Sets row["detail_fetch_status"] to:
      "ok"                – enrichment succeeded
      "http_error:<code>" – server returned a non-2xx response
      "error:<type>"      – connection or other exception
      "no_url"            – product_url was empty
    """
    url = row.get("product_url", "")
    if not url:
        logging.warning("No product_url for %s — skipping detail fetch.", row["isin"])
        row["detail_fetch_status"] = "no_url"
        return

    try:
        html = fetch_html(url)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        logging.warning("HTTP %s fetching detail page %s", code, url)
        row["detail_fetch_status"] = f"http_error:{code}"
        return
    except Exception as exc:
        logging.warning("Could not fetch detail page %s: %s", url, exc)
        row["detail_fetch_status"] = f"error:{type(exc).__name__}"
        return

    soup = BeautifulSoup(html, "html.parser")
    kv   = _table_kv(soup)

    # --- currency ---
    row["ccy"] = kv.get("fund base currency", "")

    # --- TER → basis points ---
    raw_ter = kv.get("total expense ratio", "")
    row["ter_bps"] = ter_to_bps(raw_ter) if raw_ter else ""

    # --- AUM → millions (empty string if zero or missing, not "0.0") ---
    raw_aum = kv.get("fund aum", "")
    row["aum_mn"] = aum_raw_to_mn(raw_aum) if raw_aum else ""

    # --- NAV ---
    row["nav"] = kv.get("unit nav", "")

    # --- inception date ---
    row["inception_date"] = kv.get("inception date", "")

    # --- index ---
    row["index_name"] = kv.get("reference index", "")

    # --- replication ---
    row["replication_method"] = kv.get("replication method", kv.get("replication basis", ""))

    # --- risk metrics ---
    row["annual_volatility"] = kv.get("annual volatility", "")
    row["tracking_error"]    = kv.get("tracking error", "")

    # --- shares in issue ---
    row["shares_in_issue"] = kv.get("shares in issue", "")

    row["detail_fetch_status"] = "ok"

    logging.info(
        "  %-14s  ccy=%-4s  ter=%s bps  aum=%s mn  nav=%s",
        row["isin"],
        row["ccy"] or "?",
        row["ter_bps"] or "?",
        row["aum_mn"] or "?",
        row["nav"] or "?",
    )


# ---------------------------------------------------------------------------
# Main snapshot builder
# ---------------------------------------------------------------------------

def build_snapshot(now: datetime) -> dict[str, object]:
    # 1. Listing page → basic rows
    logging.info("Fetching listing page: %s", PAGE_URL)
    listing_html = fetch_html(PAGE_URL)
    listing_rows = extract_listing_rows(listing_html)

    active     = [r for r in listing_rows if r["terminated"] == "false"]
    terminated = [r for r in listing_rows if r["terminated"] == "true"]
    logging.info(
        "Listing page: %s active + %s terminated rows.",
        len(active), len(terminated),
    )

    malformed = [r for r in listing_rows if r.get("isin_note") == "malformed_isin"]
    if malformed:
        logging.warning(
            "Malformed ISIN(s) found on listing page: %s",
            ", ".join(r["isin"] for r in malformed),
        )

    # 2. Detail pages → enrich every row (active + terminated)
    logging.info("Fetching individual product pages …")
    for i, row in enumerate(listing_rows):
        logging.info(
            "[%d/%d] %s  (%s)",
            i + 1, len(listing_rows),
            row["isin"], row["etf_name"],
        )
        enrich_from_detail_page(row)
        if i < len(listing_rows) - 1:
            time.sleep(DELAY_S)

    # Summary
    status_counts: dict[str, int] = {}
    for r in listing_rows:
        s = r["detail_fetch_status"]
        status_counts[s] = status_counts.get(s, 0) + 1
    logging.info("Detail-fetch summary: %s", status_counts)

    return {
        "source_url":   PAGE_URL,
        "method":       "requests + BeautifulSoup — listing page + per-product detail pages",
        "captured_at":  now.isoformat(),
        "listing_rows": listing_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now      = timestamp_now()
    snapshot = build_snapshot(now)
    write_json(destination, snapshot)
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved: %s", destination)


async def download_finex_file() -> Path:
    now         = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("listing_rows", [])


def main() -> None:
    output_path = build_output_path(timestamp_now())
    download_snapshot(output_path)


if __name__ == "__main__":
    main()
