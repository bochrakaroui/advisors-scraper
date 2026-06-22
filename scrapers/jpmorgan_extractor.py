"""Download J.P. Morgan UK ETF listing data from the official fund explorer API."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import requests


PAGE_URL = "https://am.jpmorgan.com/gb/en/asset-management/per/products/fund-explorer/etf"
API_URL = (
    "https://am.jpmorgan.com/FundsMarketingHandler/fund-explorer"
    "?country=gb&role=per&userLoggedIn=false&language=en&fundType=etf"
)
BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "jpmorgan" / "jpmorgan_downloads"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": PAGE_URL,
    "Accept": "application/json, text/plain, */*",
}


def timestamp_now() -> datetime:
    return datetime.now()


def build_output_path(now: datetime) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"jpmorgan_etf_export_{now.strftime('%Y%m%d_%H%M%S')}.json"


def download_snapshot(destination: Path) -> None:
    response = requests.get(API_URL, headers=REQUEST_HEADERS, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Unexpected J.P. Morgan API payload: expected a list of ETF rows.")
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


async def download_jpmorgan_file() -> Path:
    output_path = build_output_path(timestamp_now())
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected J.P. Morgan raw snapshot in {path}: expected a list.")
    return payload


def main() -> None:
    output_path = asyncio.run(download_jpmorgan_file())
    print(f"Source page : {PAGE_URL}")
    print(f"Raw file    : {output_path}")


if __name__ == "__main__":
    main()
