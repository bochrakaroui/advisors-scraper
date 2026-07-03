"""Download Goldman Sachs ETF data from the official UK institutions fund finder."""

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

import requests


ISSUER = "Goldman Sachs Asset Management"
PAGE_URL = (
    "https://am.gs.com/en-gb/institutions/funds"
    "?locale=en-gb&audience=institutions&eft=true&sf=funds&filters=funds%7CETF"
)
FUNDS_SERVICE_URL = "https://am.gs.com/services/funds"
DETAIL_URL_TEMPLATE = (
    "https://am.gs.com/en-gb/institutions/funds/detail/{pv_number}/{share_class_id}/{slug}"
)
COUNTRY_CODE = "gb"
LANGUAGE_CODE = "en"
AUDIENCE = "institutions"
PAGE_SIZE = 25
REQUEST_TIMEOUT_S = 45

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "Goldman_Sachs"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Content-Type": "application/json",
    "Referer": PAGE_URL,
}

LIST_QUERY = """
query getFundShareClasses($fundRequest: FundRequest) {
  fundShareClasses(fundRequest: $fundRequest) {
    facet {
      ...facet
      __typename
    }
    funds {
      baseCurrency
      scBaseCurrency
      displayFundDetail
      dataSource
      fundCategory
      fundLocalName
      fundNickname
      fundNikkeiName
      sfdr: sfdrArticleClassificationNumber
      dailyPerformance {
        ...dailyPerformance
        __typename
      }
      distributionFrequency
      fundName
      fundType
      isOffshore
      marketingAssetClass
      marketingAssetClassI18nKey
      marketingSubAssetClass
      monthlyPerformance {
        ...monthlyPerformance
        __typename
      }
      pvNumber
      shareClassId
      shareClassNumber
      shareClassInceptionDate
      shareClassType: flShareClassType
      ticker
      __typename
    }
    __typename
  }
}

fragment dailyPerformance on DailyPerformance {
  nav {
    asAtDate
    value
    __typename
  }
  navChange {
    asAtDate
    value
    __typename
  }
  shareClassNetAssets {
    asAtDate
    value
    __typename
  }
  latestDividends {
    asAtDate
    value
    __typename
  }
  __typename
}

fragment facet on Facet {
  numberOfFundsAvailable: numberOfFundsShowing
  numberOfShareClassesAvailable: numberOfShareClassesShowing
  filters {
    ...filter
    __typename
  }
  __typename
}

fragment filter on Filter {
  label
  id
  options {
    label
    id
    subLabel
    options {
      label
      id
      subLabel
      __typename
    }
    __typename
  }
  __typename
}

fragment monthlyPerformance on MonthlyPerformance {
  asAtDate
  annualisedReturns1yr
  annualisedReturns3yr
  annualisedReturns5yr
  annualisedReturns10yr
  annualisedReturnsSinceIncept
  __typename
}
""".strip()

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SPACE_PATTERN = re.compile(r"\s+")
NON_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


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
    return build_run_output_dir(OUTPUT_DIR, now.strftime("%Y-%m-%d")) / "goldmansachs_etf_export.json"


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value: object | None) -> str:
    if value is None:
        return ""
    cleaned = str(value).replace("\u00ad", "").replace("\u00a0", " ").strip()
    cleaned = SPACE_PATTERN.sub(" ", cleaned)
    return "" if cleaned in {"", "-", "--", "- ", " -", "None"} else cleaned


def normalize_isin(value: object | None) -> str:
    return clean_text(value).upper().replace(" ", "")


def slugify(value: object | None) -> str:
    cleaned = clean_text(value).lower()
    slug = NON_SLUG_PATTERN.sub("-", cleaned).strip("-")
    return slug or "fund-detail"


def to_decimal(value: object | None) -> Decimal | None:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def format_decimal(value: Decimal, places: int = 2) -> str:
    quantized = value.quantize(Decimal("1." + ("0" * places)), rounding=ROUND_HALF_UP)
    return format(quantized, f".{places}f")


def amount_to_millions(value: object | None) -> str:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return ""
    return format_decimal(decimal_value / Decimal("1000000"), places=2)


def get_dict(value: object | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def build_detail_url(pv_number: str, share_class_id: str, fund_name: str) -> str:
    if not pv_number or not share_class_id:
        return ""
    return DETAIL_URL_TEMPLATE.format(
        pv_number=pv_number,
        share_class_id=share_class_id,
        slug=slugify(fund_name),
    )


def build_list_payload(offset: int) -> dict[str, Any]:
    return {
        "operationName": "getFundShareClasses",
        "variables": {
            "fundRequest": {
                "country": COUNTRY_CODE,
                "language": LANGUAGE_CODE,
                "audience": AUDIENCE,
                "limit": PAGE_SIZE,
                "offset": offset,
                "sortBy": "FN",
                "sortOrder": "ASC",
                "filterParam": {
                    "selectedFilter": "funds",
                    "funds": ["ETF"],
                    "searchText": "",
                },
            }
        },
        "query": LIST_QUERY,
    }


def fetch_listing_page(offset: int) -> dict[str, Any]:
    response = SESSION.post(
        FUNDS_SERVICE_URL,
        json=build_list_payload(offset),
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()
    if "errors" in payload:
        raise RuntimeError(f"Goldman Sachs funds service returned errors: {payload['errors']}")
    return payload


def transform_listing_row(source_row: dict[str, Any]) -> dict[str, str]:
    daily_performance = get_dict(source_row.get("dailyPerformance"))
    monthly_performance = get_dict(source_row.get("monthlyPerformance"))
    nav = get_dict(daily_performance.get("nav"))
    nav_change = get_dict(daily_performance.get("navChange"))
    share_class_net_assets = get_dict(daily_performance.get("shareClassNetAssets"))

    fund_name = clean_text(source_row.get("fundName"))
    pv_number = clean_text(source_row.get("pvNumber"))
    share_class_id = normalize_isin(source_row.get("shareClassId"))

    return {
        "etf_name": fund_name,
        "issuer": ISSUER,
        "isin": share_class_id,
        "share_class_number": clean_text(source_row.get("shareClassNumber")),
        "pv_number": pv_number,
        "ticker": clean_text(source_row.get("ticker")).upper(),
        "ccy": clean_text(source_row.get("scBaseCurrency") or source_row.get("baseCurrency")).upper(),
        "base_currency": clean_text(source_row.get("baseCurrency")).upper(),
        "share_class_currency": clean_text(source_row.get("scBaseCurrency")).upper(),
        "share_class_type": clean_text(source_row.get("shareClassType")),
        "nav": clean_text(nav.get("value")),
        "nav_date": clean_text(nav.get("asAtDate")),
        "nav_change": clean_text(nav_change.get("value")),
        "nav_change_date": clean_text(nav_change.get("asAtDate")),
        "aum_mn": amount_to_millions(share_class_net_assets.get("value")),
        "aum_date": clean_text(share_class_net_assets.get("asAtDate")),
        "distribution_frequency": clean_text(source_row.get("distributionFrequency")),
        "sfdr": clean_text(source_row.get("sfdr")),
        "asset_class": clean_text(source_row.get("marketingAssetClass")),
        "asset_class_key": clean_text(source_row.get("marketingAssetClassI18nKey")),
        "sub_asset_class": clean_text(source_row.get("marketingSubAssetClass")),
        "fund_category": clean_text(source_row.get("fundCategory")),
        "fund_type": clean_text(source_row.get("fundType")),
        "inception_date": clean_text(source_row.get("shareClassInceptionDate")),
        "performance_1y": clean_text(monthly_performance.get("annualisedReturns1yr")),
        "performance_3y": clean_text(monthly_performance.get("annualisedReturns3yr")),
        "performance_5y": clean_text(monthly_performance.get("annualisedReturns5yr")),
        "performance_10y": clean_text(monthly_performance.get("annualisedReturns10yr")),
        "performance_since_inception": clean_text(monthly_performance.get("annualisedReturnsSinceIncept")),
        "performance_as_of": clean_text(monthly_performance.get("asAtDate")),
        "is_offshore": clean_text(source_row.get("isOffshore")),
        "display_fund_detail": clean_text(source_row.get("displayFundDetail")),
        "data_source": clean_text(source_row.get("dataSource")),
        "detail_url": build_detail_url(pv_number, share_class_id, fund_name),
    }


def build_snapshot(now: datetime) -> dict[str, object]:
    first_page = fetch_listing_page(offset=0)
    fund_share_classes = get_dict(get_dict(first_page.get("data")).get("fundShareClasses"))
    first_page_rows = fund_share_classes.get("funds", [])
    if not isinstance(first_page_rows, list):
        raise RuntimeError("Goldman Sachs funds service returned an unexpected funds payload.")

    facet = get_dict(fund_share_classes.get("facet"))
    total_share_classes = int(facet.get("numberOfShareClassesAvailable") or len(first_page_rows))
    total_funds = int(facet.get("numberOfFundsAvailable") or 0)

    raw_pages = [first_page]
    raw_rows: list[dict[str, Any]] = list(first_page_rows)
    seen_share_class_ids = {
        normalize_isin(get_dict(row).get("shareClassId"))
        for row in raw_rows
        if normalize_isin(get_dict(row).get("shareClassId"))
    }
    max_reported_share_classes = total_share_classes

    for offset in range(PAGE_SIZE, PAGE_SIZE * 10, PAGE_SIZE):
        logging.info("Fetching Goldman Sachs ETF listing page offset=%s", offset)
        page_payload = fetch_listing_page(offset=offset)
        raw_pages.append(page_payload)
        page_fund_share_classes = get_dict(get_dict(page_payload.get("data")).get("fundShareClasses"))
        page_facet = get_dict(page_fund_share_classes.get("facet"))
        max_reported_share_classes = max(
            max_reported_share_classes,
            int(page_facet.get("numberOfShareClassesAvailable") or 0),
        )
        page_rows = page_fund_share_classes.get("funds", [])
        if not isinstance(page_rows, list) or not page_rows:
            break

        new_rows = []
        for row in page_rows:
            share_class_id = normalize_isin(get_dict(row).get("shareClassId"))
            if share_class_id and share_class_id in seen_share_class_ids:
                continue
            if share_class_id:
                seen_share_class_ids.add(share_class_id)
            new_rows.append(row)

        if not new_rows:
            break

        raw_rows.extend(new_rows)

        if len(seen_share_class_ids) >= max_reported_share_classes:
            break

    listing_rows = [transform_listing_row(row) for row in raw_rows]
    logging.info(
        "Captured %d Goldman Sachs ETF share classes across %d funds.",
        len(listing_rows),
        total_funds,
    )

    return {
        "source": {
            "provider": ISSUER,
            "page_url": PAGE_URL,
            "service_url": FUNDS_SERVICE_URL,
            "country": COUNTRY_CODE,
            "language": LANGUAGE_CODE,
            "audience": AUDIENCE,
            "page_size": PAGE_SIZE,
        },
        "method": "Official Goldman Sachs fund finder GraphQL listing API",
        "captured_at": now.isoformat(),
        "total_funds": total_funds,
        "total_share_classes_reported": max_reported_share_classes,
        "total_share_classes_captured": len(listing_rows),
        "pages": raw_pages,
        "listing_rows": listing_rows,
    }


def download_snapshot(destination: Path) -> None:
    setup_logging()
    now = timestamp_now()
    snapshot = build_snapshot(now)
    write_json(destination, snapshot)
    logging.info("Data method : %s", snapshot["method"])
    logging.info("Snapshot saved: %s", destination)


async def download_goldman_sachs_file() -> Path:
    now = timestamp_now()
    output_path = build_output_path(now)
    await asyncio.to_thread(download_snapshot, output_path)
    return output_path


def parse_snapshot_rows(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("listing_rows", [])
    return rows if isinstance(rows, list) else []


def main() -> None:
    output_path = build_output_path(timestamp_now())
    download_snapshot(output_path)


if __name__ == "__main__":
    main()
