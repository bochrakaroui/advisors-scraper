"""Run all ETF downloaders and build one combined CSV for the run."""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
import xml.etree.ElementTree as ET

from src.source_freshness import load_source_metadata

BASE_DIR = Path(__file__).resolve().parent

if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

try:
    from scrapers.justetf_profile import build_session as build_justetf_session
    from scrapers.justetf_profile import fetch_profile as fetch_justetf_profile
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    build_justetf_session = None  # type: ignore[assignment]
    fetch_justetf_profile = None  # type: ignore[assignment]


def load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from providers.amundi.extract_amundi_fields import (
    INPUT_DIR as AMUNDI_INPUT_DIR,
    extract_rows as extract_amundi_rows,
    find_latest_download as find_latest_amundi_download,
    parse_xlsx_rows as parse_amundi_source_rows,
)
from providers.output_schema import infer_aum_currency_from_row
from providers.firsttrust.extract_firsttrust_fields import (
    INPUT_DIR as FIRSTTRUST_INPUT_DIR,
    extract_rows as extract_firsttrust_rows,
    find_latest_download as find_latest_firsttrust_download,
    parse_snapshot_rows as parse_firsttrust_source_rows,
)
from providers.hanetf.extract_hanetf_fields import (
    INPUT_DIR as HANETF_INPUT_DIR,
    extract_rows as extract_hanetf_rows,
    find_latest_download as find_latest_hanetf_download,
    parse_snapshot_rows as parse_hanetf_source_rows,
)
from providers.palmersquare.extract_palmersquare_fields import (
    INPUT_DIR as PALMERSQUARE_INPUT_DIR,
    extract_rows as extract_palmersquare_rows,
    find_latest_download as find_latest_palmersquare_download,
    parse_snapshot_rows as parse_palmersquare_source_rows,
)
from providers.vaneck.extract_vaneck_fields import (
    INPUT_DIR as VANECK_INPUT_DIR,
    extract_rows as extract_vaneck_rows,
    find_latest_download as find_latest_vaneck_download,
    parse_snapshot_rows as parse_vaneck_source_rows,
)
from providers.franklintempleton.extract_franklintempleton_fields import (
    INPUT_DIR as FRANKLIN_TEMPLETON_INPUT_DIR,
    extract_rows as extract_franklin_templeton_rows,
    find_latest_download as find_latest_franklin_templeton_download,
    parse_snapshot_rows as parse_franklin_templeton_source_rows,
)
from providers.hsbc.extract_hsbc_fields import (
    INPUT_DIR as HSBC_INPUT_DIR,
    extract_rows as extract_hsbc_rows,
    find_latest_download as find_latest_hsbc_download,
    parse_snapshot_rows as parse_hsbc_source_rows,
)
from providers.wisdomtree.extract_wisdomtree_fields import (
    INPUT_DIR as WISDOMTREE_INPUT_DIR,
    extract_rows as extract_wisdomtree_rows,
    find_latest_download as find_latest_wisdomtree_download,
    parse_snapshot_rows as parse_wisdomtree_source_rows,
)
from providers.SPDR.extract_spdr_fields import (
    INPUT_DIR as SPDR_INPUT_DIR,
    extract_rows as extract_spdr_rows,
    find_latest_download as find_latest_spdr_download,
)
from providers.UBS.extract_ubs_fields import (
    INPUT_DIR as UBS_INPUT_DIR,
    extract_rows as extract_ubs_rows,
    find_latest_download as find_latest_ubs_download,
    parse_xlsx_rows as parse_ubs_source_rows,
)
from providers.invesco.extract_invesco_fields import (
    INPUT_DIR as INVESCO_INPUT_DIR,
    extract_rows as extract_invesco_rows,
    find_latest_download as find_latest_invesco_download,
    parse_xlsx_rows as parse_invesco_source_rows,
)
from providers.ishares.extract_ishares_fields import (
    INPUT_DIR as ISHARES_INPUT_DIR,
    extract_rows as extract_ishares_rows,
    find_latest_download as find_latest_ishares_download,
    parse_xml_spreadsheet as parse_ishares_source_rows,
)
from providers.jpmorgan.extract_jpmorgan_fields import (
    INPUT_DIR as JPMORGAN_INPUT_DIR,
    extract_rows as extract_jpmorgan_rows,
    find_latest_download as find_latest_jpmorgan_download,
    parse_snapshot_rows as parse_jpmorgan_source_rows,
)
from providers.landg.extract_LandG_fields import (
    INPUT_DIR as LANDG_INPUT_DIR,
    extract_rows as extract_landg_rows,
    find_latest_download as find_latest_landg_download,
    parse_snapshot_rows as parse_landg_source_rows,
)
from providers.vanguard.extract_vanguard_fields import (
    INPUT_DIR as VANGUARD_INPUT_DIR,
    extract_rows as extract_vanguard_rows,
    find_latest_download as find_latest_vanguard_download,
    parse_snapshot_rows as parse_vanguard_source_rows,
)
from providers.xtrackers.extract_xtrackers_fields import (
    INPUT_DIR as XTRACKERS_INPUT_DIR,
    extract_rows as extract_xtrackers_rows,
    find_latest_download as find_latest_xtrackers_download,
    parse_xlsx_rows as parse_xtrackers_source_rows,
)
from providers.globalx.extract_globalx_fields import (
    INPUT_DIR as GLOBALX_INPUT_DIR,
    extract_rows as extract_globalx_rows,
    find_latest_download as find_latest_globalx_download,
    parse_snapshot_rows as parse_globalx_source_rows,
)
from providers.finex.extract_finex_fields import (
    INPUT_DIR as FINEX_INPUT_DIR,
    extract_rows as extract_finex_rows,
    find_latest_download as find_latest_finex_download,
    parse_snapshot_rows as parse_finex_source_rows,
)
from providers.fidelity.extract_fidelity_fields import (
    INPUT_DIR as FIDELITY_INPUT_DIR,
    extract_rows as extract_fidelity_rows,
    find_latest_download as find_latest_fidelity_download,
    parse_snapshot_rows as parse_fidelity_source_rows,
)
from providers.imgp.extract_imgp_fields import (
    INPUT_DIR as IMGP_INPUT_DIR,
    extract_rows as extract_imgp_rows,
    find_latest_download as find_latest_imgp_download,
    parse_snapshot_rows as parse_imgp_source_rows,
)
from providers.abrdn.extract_abrdn_fields import (
    INPUT_DIR as ABRDN_INPUT_DIR,
    extract_rows as extract_abrdn_rows,
    find_latest_download as find_latest_abrdn_download,
    parse_snapshot_rows as parse_abrdn_source_rows,
)
from providers.Alliance_Bernstein.extract_alliance_bernstein_fields import (
    INPUT_DIR as ALLIANCE_BERNSTEIN_INPUT_DIR,
    extract_rows as extract_alliance_bernstein_rows,
    find_latest_download as find_latest_alliance_bernstein_download,
    parse_snapshot_rows as parse_alliance_bernstein_source_rows,
)
from providers.Alpha_Ucits.extract_alpha_ucits_fields import (
    INPUT_DIR as ALPHA_UCITS_INPUT_DIR,
    extract_rows as extract_alpha_ucits_rows,
    find_latest_download as find_latest_alpha_ucits_download,
    parse_snapshot_rows as parse_alpha_ucits_source_rows,
)
from providers.American_Century_Investments.extract_american_century_investments_fields import (
    INPUT_DIR as AMERICAN_CENTURY_INPUT_DIR,
    extract_rows as extract_american_century_rows,
    find_latest_download as find_latest_american_century_download,
    parse_snapshot_rows as parse_american_century_source_rows,
)
from providers.ARK_Investment_Management.extract_ARK_fields import (
    INPUT_DIR as ARK_INPUT_DIR,
    extract_rows as extract_ark_rows,
    find_latest_download as find_latest_ark_download,
    parse_snapshot_rows as parse_ark_source_rows,
)
from providers.BNP_Paribas_Asset_Management.extract_bnp_fields import (
    INPUT_DIR as BNP_INPUT_DIR,
    extract_rows as extract_bnp_rows,
    find_latest_download as find_latest_bnp_download,
    parse_snapshot_rows as parse_bnp_source_rows,
)
from providers.Columbia.extract_columbia_fields import (
    INPUT_DIR as COLUMBIA_INPUT_DIR,
    extract_rows as extract_columbia_rows,
    find_latest_download as find_latest_columbia_download,
    parse_snapshot_rows as parse_columbia_source_rows,
)
from providers.Connect_ETFs.extract_connect_etfs_fields import (
    INPUT_DIR as CONNECT_ETFS_INPUT_DIR,
    extract_rows as extract_connect_etfs_rows,
    find_latest_download as find_latest_connect_etfs_download,
    parse_snapshot_rows as parse_connect_etfs_source_rows,
)
from providers.Dimensional.extract_dimensional_fields import (
    INPUT_DIR as DIMENSIONAL_INPUT_DIR,
    extract_rows as extract_dimensional_rows,
    find_latest_download as find_latest_dimensional_download,
    parse_snapshot_rows as parse_dimensional_source_rows,
)
from providers.Goldman_Sachs.extract_goldman_sachs_fields import (
    INPUT_DIR as GOLDMAN_SACHS_INPUT_DIR,
    extract_rows as extract_goldman_sachs_rows,
    find_latest_download as find_latest_goldman_sachs_download,
    parse_snapshot_rows as parse_goldman_sachs_source_rows,
)
from providers.Janus_Henderson.extract_janus_henderson_fields import (
    INPUT_DIR as JANUS_HENDERSON_INPUT_DIR,
    extract_rows as extract_janus_henderson_rows,
    find_latest_download as find_latest_janus_henderson_download,
    parse_snapshot_rows as parse_janus_henderson_source_rows,
)
from providers.KraneShares.extract_kraneshares_fields import (
    INPUT_DIR as KRANESHARES_INPUT_DIR,
    extract_rows as extract_kraneshares_rows,
    find_latest_download as find_latest_kraneshares_download,
    parse_snapshot_rows as parse_kraneshares_source_rows,
)
from providers.Nordea.extract_Nordea_fields import (
    INPUT_DIR as NORDEA_INPUT_DIR,
    extract_rows as extract_nordea_rows,
    find_latest_download as find_latest_nordea_download,
    parse_snapshot_rows as parse_nordea_source_rows,
)
from providers.Ossiam.extract_ossiam_fields import (
    INPUT_DIR as OSSIAM_INPUT_DIR,
    extract_rows as extract_ossiam_rows,
    find_latest_download as find_latest_ossiam_download,
)
from providers.Pacer_ETFs.extract_pacer_etfs_fields import (
    INPUT_DIR as PACER_ETFS_INPUT_DIR,
    extract_rows as extract_pacer_etfs_rows,
    find_latest_download as find_latest_pacer_etfs_download,
    parse_snapshot_rows as parse_pacer_etfs_source_rows,
)
from providers.PIMCO.extract_pimco_fields import (
    INPUT_DIR as PIMCO_INPUT_DIR,
    extract_rows as extract_pimco_rows,
    find_latest_download as find_latest_pimco_download,
    parse_snapshot_rows as parse_pimco_source_rows,
)
from providers.Market_Access.extract_market_access_fields import (
    INPUT_DIR as MARKET_ACCESS_INPUT_DIR,
    extract_rows as extract_market_access_rows,
    find_latest_download as find_latest_market_access_download,
    parse_snapshot_rows as parse_market_access_source_rows,
)
from providers.Robeco.extract_Robeco_fields import (
    INPUT_DIR as ROBECO_INPUT_DIR,
    extract_rows as extract_robeco_rows,
    find_latest_download as find_latest_robeco_download,
    parse_snapshot_rows as parse_robeco_source_rows,
)
from providers.Schroders.extract_schroders_fields import (
    INPUT_DIR as SCHRODERS_INPUT_DIR,
    extract_rows as extract_schroders_rows,
    find_latest_download as find_latest_schroders_download,
    parse_snapshot_rows as parse_schroders_source_rows,
)
from providers.Waystone.extract_waystone_fields import (
    INPUT_DIR as WAYSTONE_INPUT_DIR,
    extract_rows as extract_waystone_rows,
    find_latest_download as find_latest_waystone_download,
)
try:
    from providers.vanguard.download_vanguard import download_vanguard_file
except ModuleNotFoundError:
    async def download_vanguard_file() -> Path:
        raise ModuleNotFoundError(
            "Vanguard downloader module is missing: providers.vanguard.download_vanguard"
        )
from scrapers.Amundi_extractor import download_amundi_file
from scrapers.firsttrust_extractor import download_firsttrust_file
from scrapers.hanetf_extractor import download_hanetf_file
from scrapers.franklintempleton_extractor import download_etf_list as download_franklintempleton_file
from scrapers.UBS_extractor import download_ubs_file
from scrapers.Xtrackers_extractor import download_xtrackers_file
from scrapers.hsbc_extractor import download_hsbc_file
from scrapers.invesco_extractor import download_invesco_file
from scrapers.ishares_extractor import download_etf_list
from scrapers.jpmorgan_extractor import download_jpmorgan_file
from scrapers.LandG_extractor import download_landg_file
from scrapers.palmersquare_extractor import download_palmersquare_file
from scrapers.vaneck_extractor import download_etf_list as download_vaneck_file
from scrapers.spdr_collector import download_spdr_file, parse_xlsx_rows as parse_spdr_source_rows
from scrapers.wisdomtree_extractor import download_wisdomtree_file
from scrapers.globalx_extractor import download_globalx_file
from scrapers.finex_extractor import download_finex_file
from scrapers.fidelity_international_extractor import download_fidelity_file
from scrapers.imgp_extractor import download_imgp_file
from scrapers.abrdn_extractor import scrape_abrdn_etfs
from scrapers.Alliance_Bernstein_extractor import scrape_alliance_bernstein_etfs
from scrapers.Alpha_Ucits_extractor import scrape_alpha_ucits
from scrapers.American_Century_Investments_extractor import download_american_century_investments_file
from scrapers.ARK_Investment_Management_extractor import download_ark_file
from scrapers.BNP_Paribas_Asset_Management_extractor import download_bnpparibas_file
from scrapers.Columbia_extractor import download_columbia_file
from scrapers.Connect_ETFs_extractor import download_connect_etfs_file
from scrapers.Dimensional_extractor import download_dimensional_file
from scrapers.Goldman_Sachs_extractor import download_goldman_sachs_file
from scrapers.Janus_Henderson_extractor import download_janus_henderson_file
from scrapers.KraneShares_extractor import download_kraneshares_ucits
from scrapers.MandG_extractor import download_mg_file
from scrapers.Nordea_extractor import scrape_nordea
from scrapers.Ossiam_extractor import download_ossiam_file
from scrapers.Pacer_ETFs_extractor import run as download_pacer_etfs_file
from scrapers.PIMCO_extractor import download_pimco_file
from scrapers.market_access_extractor import download_market_access_file
from scrapers.Robeco_extractor import download_robeco_file
from scrapers.Schroders_extractor import download_schroders_file
from scrapers.Waystone_extractor import download_waystone_file

MG_FIELDS = load_module_from_path(
    "mg_extract_fields",
    BASE_DIR / "providers" / "M&G" / "extract_mg_fields.py",
)
MG_INPUT_DIR = MG_FIELDS.INPUT_DIR
extract_mg_rows = MG_FIELDS.extract_rows
find_latest_mg_download = MG_FIELDS.find_latest_download
parse_mg_source_rows = MG_FIELDS.parse_snapshot_rows
RUNS_DIR = BASE_DIR / "pipeline_runs"
COMBINED_FILENAME = "all_etf_fields.csv"
ISIN_FILTER_PATH = BASE_DIR / "ISIN-list.xlsx"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
XML_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
CELL_REF_PATTERN = re.compile(r"([A-Z]+)")
HEADER_NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")
INTERNAL_SPACE_PATTERN = re.compile(r"\s+")
ISIN_HEADER_CANDIDATES = ("isin", "isincode")
INVISIBLE_ISIN_CHARACTERS = ("\u00A0", "\u2007", "\u202F", "\u200B", "\uFEFF")
ISIN_FILTER_BYPASS_ISSUERS = {"Fidelity International"}
JUSTETF_AUM_FALLBACK_BLOCKED_ISSUERS = {
    "Janus Henderson",
    "J.P. Morgan Asset Management",
    "Waystone",
}
LEGACY_AUM_COLUMN = "AUM(M)"
PARTIAL_AUM_COLUMN = "Partial AUM(M)"
TOTAL_AUM_COLUMN = "Total AUM(M)"
OUTPUT_COLUMNS = [
    "ETF Name",
    "Issuer",
    "ISIN",
    "CCY",
    "TER(bps)",
    PARTIAL_AUM_COLUMN,
    TOTAL_AUM_COLUMN,
    "AUM CCY",
    "Date",
]

ALL_PROVIDERS = (
    "ishares",
    "xtrackers",
    "amundi",
    "fidelity",
    "invesco",
    "ubs",
    "spdr",
    "hsbc",
    "jpmorgan",
    "landg",
    "palmersquare",
    "vaneck",
    "franklintempleton",
    "wisdomtree",
    "vanguard",
    "firsttrust",
    "hanetf",
    "globalx",
    "finex",
    "imgp",
    "abrdn",
    "alliancebernstein",
    "alphaucits",
    "americancenturyinvestments",
    "ark",
    "bnpparibas",
    "columbia",
    "connectetfs",
    "dimensional",
    "goldmansachs",
    "janushenderson",
    "kraneshares",
    "mg",
    "marketaccess",
    "nordea",
    "ossiam",
    "paceretfs",
    "pimco",
    "robeco",
    "schroders",
    "waystone",
)
Downloader = Callable[[], Awaitable[Path]]
Extractor = Callable[[Path], list[dict[str, str]]]
LatestDownloadFinder = Callable[[Path], Path]
SourceRowParser = Callable[[Path], list[dict[str, str]]]


@dataclass(frozen=True)
class ProviderPipeline:
    name: str
    downloader: Downloader
    extractor: Extractor
    input_dir: Path
    output_dir: Path
    output_filename: str
    latest_download_finder: LatestDownloadFinder
    source_row_parser: SourceRowParser


@dataclass(frozen=True)
class PreparedInput:
    path: Path
    status: str
    note: str = ""


@dataclass(frozen=True)
class ProviderRunReport:
    provider_name: str
    input_path: Path
    output_path: Path
    source_row_count: int
    extracted_row_count: int
    missing_counts: dict[str, int]
    reference_isin_count: int
    provider_match_count: int
    unmatched_row_count: int
    valid_isin_count: int
    source_label: str
    source_method: str
    source_url: str
    source_status: str
    discovery_status: str
    extraction_status: str
    isin_filter_status: str
    output_status: str
    result_status: str
    duration_seconds: float
    note: str = ""


@dataclass(frozen=True)
class IsinFilterSummary:
    whitelist_unique_isin_count: int
    final_rows_before_filtering: int
    final_unique_isins_before_filtering: int
    final_rows_after_filtering: int
    final_unique_isins_after_filtering: int
    removed_rows_count: int
    removed_unique_isin_count: int
    unexpected_isin_count_after_filtering: int
    isin_column_name: str


@dataclass(frozen=True)
class FinalCoverageSummary:
    missing_expected_isins: tuple[str, ...]
    missing_aum_identifiers: tuple[str, ...]


async def run_sync_downloader(download_func: Callable[[], Path]) -> Path:
    return await asyncio.to_thread(download_func)


def clean_display_text(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def relative_display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(BASE_DIR.resolve())).replace("\\", "/")
    except Exception:
        return str(path)


def summarize_source_metadata(input_path: Path) -> tuple[str, str, str]:
    source_label = "Official provider website"
    source_method = ""
    source_url = ""

    suffix = input_path.suffix.lower()
    if suffix == ".json":
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except Exception:
            payload = None

        if isinstance(payload, dict):
            source = payload.get("source")
            if isinstance(source, dict):
                source_label = clean_display_text(source.get("provider")) or source_label
                source_url = (
                    clean_display_text(source.get("page_url"))
                    or clean_display_text(source.get("service_url"))
                    or source_url
                )
            source_method = clean_display_text(payload.get("method"))
            source_url = source_url or clean_display_text(payload.get("source_url"))

    if not source_method:
        if suffix in {".xlsx", ".xls", ".xml"}:
            source_method = "File download + parsing"
        elif suffix == ".json":
            source_method = "API / JSON snapshot"
        else:
            source_method = "Provider data extraction"

    return source_label, source_method, source_url


def evaluate_extraction_status(row_count: int, missing_counts: dict[str, int]) -> str:
    if row_count == 0:
        return "EMPTY"
    if any(missing_counts.get(column, 0) for column in ("ISIN", TOTAL_AUM_COLUMN, "TER(bps)")):
        return "PARTIAL"
    return "OK"


def evaluate_result_status(row_count: int, provider_match_count: int, missing_counts: dict[str, int]) -> str:
    if row_count == 0:
        return "EMPTY"
    if provider_match_count == 0:
        return "NO MATCHES"
    if any(missing_counts.get(column, 0) for column in ("ISIN", TOTAL_AUM_COLUMN, "TER(bps)")):
        return "PARTIAL"
    return "SUCCESS"


def print_provider_report(report: ProviderRunReport, run_date: str) -> None:
    source_metadata = load_source_metadata(report.input_path)
    source_date = str(source_metadata.get("source_date") or "").strip()
    freshness_status = str(source_metadata.get("freshness_status") or "").strip()
    freshness_proof = str(source_metadata.get("freshness_proof") or "").strip()
    resolved_source_url = str(source_metadata.get("resolved_source_url") or "").strip()
    freshness_url = resolved_source_url or str(source_metadata.get("source_url") or "").strip()

    print()
    print("ETF EXTRACTOR")
    print(f"Provider : {report.provider_name}")
    print(f"Run Date : {datetime.strptime(run_date, '%Y-%m-%d').strftime('%d/%m/%Y')}")
    print()
    print("[1/5] SOURCE")
    print(f"      Source      : {report.source_label}")
    print(f"      Method      : {report.source_method}")
    print(f"      URL         : {report.source_url or 'n/a'}")
    if source_date:
        print(f"      Source date : {source_date}")
    if freshness_url:
        print(f"      Fresh URL   : {freshness_url}")
    if freshness_status:
        print(f"      Freshness   : {freshness_status}")
    if freshness_proof:
        print(f"      Proof       : {freshness_proof}")
    if report.note:
        print(f"      Note        : {report.note}")
    print(f"      Status      : {report.source_status}")
    print()
    print("[2/5] DISCOVERY")
    print(f"      Source rows : {report.source_row_count:,}")
    print(f"      Raw file    : {relative_display_path(report.input_path)}")
    print(f"      Status      : {report.discovery_status}")
    print()
    print("[3/5] EXTRACTION")
    print(f"      Rows extracted : {report.extracted_row_count:,}")
    print(f"      Valid ISINs    : {report.valid_isin_count:,}")
    print(f"      Missing ISIN   : {report.missing_counts.get('ISIN', 0):,}")
    print(f"      Missing Total AUM : {report.missing_counts.get(TOTAL_AUM_COLUMN, 0):,}")
    print(f"      Missing TER    : {report.missing_counts.get('TER(bps)', 0):,}")
    print(f"      Status         : {report.extraction_status}")
    print()
    print("[4/5] ISIN FILTER")
    print(f"      Reference ISINs  : {report.reference_isin_count:,}")
    print(f"      Provider matches : {report.provider_match_count:,}")
    print(f"      Unmatched rows   : {report.unmatched_row_count:,}")
    print(f"      Status           : {report.isin_filter_status}")
    print()
    print("[5/5] OUTPUT")
    print(f"      Raw file      : {relative_display_path(report.input_path)}")
    print(f"      Selected file : {relative_display_path(report.output_path)}")
    print(f"      Rows saved    : {report.extracted_row_count:,}")
    print(f"      Status        : {report.output_status}")
    print()
    print("RESULT")
    print(f"      Provider     : {report.provider_name}")
    print(f"      Extracted    : {report.extracted_row_count:,}")
    print(f"      ISIN matches : {report.provider_match_count:,}")
    print(f"      Missing Total AUM : {report.missing_counts.get(TOTAL_AUM_COLUMN, 0):,}")
    print(f"      Rows saved   : {report.extracted_row_count:,}")
    print(f"      Status       : {report.result_status}")
    print()
    print(f"Completed in {report.duration_seconds:.2f} seconds")


def parse_listing_rows_json(path: Path) -> list[dict[str, str]]:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("listing_rows", payload.get("rows", []))
        if isinstance(rows, list):
            return rows
    if isinstance(payload, list):
        return payload
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download ETF files and build one combined CSV for the run.")
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=ALL_PROVIDERS,
        default=list(ALL_PROVIDERS),
        help="Optional subset of providers to run. Defaults to all providers.",
    )
    parser.add_argument(
        "--etf-only",
        action="store_true",
        help="Pass through to the iShares extractor. By default iShares keeps all source rows.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop immediately when a provider fails. By default the script attempts all selected providers.",
    )
    parser.add_argument(
        "--use-latest-downloads",
        action="store_true",
        help="Skip new downloads and build the combined CSV from the latest existing download for each selected provider.",
    )
    return parser.parse_args()


def build_unique_date_dir(base_dir: Path, run_date: str) -> Path:
    candidate = base_dir / run_date
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def build_run_dir(run_date: str) -> Path:
    return build_unique_date_dir(RUNS_DIR, run_date)


def build_provider_output_path(output_dir: Path, run_date: str, filename: str) -> Path:
    dated_output_dir = output_dir / run_date
    dated_output_dir.mkdir(parents=True, exist_ok=True)
    return dated_output_dir / filename


def resolve_provider_output_path(
    pipeline: ProviderPipeline,
    input_path: Path,
    run_date: str,
    use_latest_downloads: bool,
) -> Path:
    if use_latest_downloads:
        return input_path.parent / pipeline.output_filename
    return build_provider_output_path(pipeline.output_dir, run_date, pipeline.output_filename)


def enforce_provider_two_file_layout(input_path: Path, output_path: Path) -> list[Path]:
    if input_path.parent != output_path.parent:
        return []

    kept_paths = {input_path.resolve(), output_path.resolve()}
    removed_paths: list[Path] = []

    for candidate in sorted(input_path.parent.iterdir()):
        if not candidate.is_file():
            continue
        if candidate.resolve() in kept_paths:
            continue
        candidate.unlink(missing_ok=True)
        removed_paths.append(candidate)

    return removed_paths


def write_combined_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_csv_with_fallback(
    output_path: Path,
    rows: list[dict[str, str]],
    output_label: str = "provider output",
) -> Path:
    try:
        write_combined_csv(output_path, rows)
        return output_path
    except PermissionError:
        fallback_path = output_path.with_name(f"{output_path.stem}_latest{output_path.suffix}")
        write_combined_csv(fallback_path, rows)
        print(
            f"[WARN] Could not overwrite locked file: {output_path}. "
            f"Saved the latest {output_label} to: {fallback_path}"
        )
        return fallback_path


def normalize_output_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        normalized_row = {column: str(row.get(column, "")).strip() for column in OUTPUT_COLUMNS}
        normalized_row[PARTIAL_AUM_COLUMN] = normalized_row.get(PARTIAL_AUM_COLUMN, "")
        normalized_row[TOTAL_AUM_COLUMN] = (
            normalized_row.get(TOTAL_AUM_COLUMN, "")
            or str(row.get(LEGACY_AUM_COLUMN, "")).strip()
        )
        if not normalized_row.get(PARTIAL_AUM_COLUMN, "") and str(row.get(PARTIAL_AUM_COLUMN, "")).strip():
            normalized_row[PARTIAL_AUM_COLUMN] = str(row.get(PARTIAL_AUM_COLUMN, "")).strip()
        if not normalized_row.get(PARTIAL_AUM_COLUMN, "") and str(row.get("Partial AUM", "")).strip():
            normalized_row[PARTIAL_AUM_COLUMN] = str(row.get("Partial AUM", "")).strip()
        if not normalized_row.get(TOTAL_AUM_COLUMN, "") and str(row.get("Total AUM", "")).strip():
            normalized_row[TOTAL_AUM_COLUMN] = str(row.get("Total AUM", "")).strip()

        if not normalized_row.get(PARTIAL_AUM_COLUMN, "") and not normalized_row.get(TOTAL_AUM_COLUMN, ""):
            normalized_row["AUM CCY"] = ""
        elif not normalized_row.get("AUM CCY", ""):
            normalized_row["AUM CCY"] = (
                infer_aum_currency_from_row(row)
                or str(row.get("CCY", "")).strip()
            )
        normalized_rows.append(normalized_row)
    return normalized_rows


def supplement_missing_aum_from_justetf(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if build_justetf_session is None or fetch_justetf_profile is None:
        return rows

    missing_isins = sorted(
        {
            normalized_isin
            for normalized_isin in (normalize_isin(row.get("ISIN")) for row in rows)
            if normalized_isin
            and any(
                normalize_isin(candidate_row.get("ISIN")) == normalized_isin
                and not str(candidate_row.get(TOTAL_AUM_COLUMN, "")).strip()
                for candidate_row in rows
            )
        }
    )
    if not missing_isins:
        return rows

    session = build_justetf_session()
    fallback_by_isin: dict[str, tuple[str, str]] = {}
    for isin in missing_isins:
        try:
            profile = fetch_justetf_profile(isin, session=session)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] justETF AUM fallback failed for {isin}: {exc}")
            continue

        if str(profile.get("fetch_status", "")).strip() not in {"", "ok"}:
            continue

        aum_m = str(profile.get("aum_mn", "")).strip()
        aum_ccy = str(profile.get("aum_ccy", "")).strip().upper()
        if not aum_m:
            continue
        fallback_by_isin[isin] = (aum_m, aum_ccy)

    if not fallback_by_isin:
        return rows

    supplemented_rows: list[dict[str, str]] = []
    for row in rows:
        isin = normalize_isin(row.get("ISIN"))
        issuer = str(row.get("Issuer", "")).strip()
        if (
            not isin
            or str(row.get(TOTAL_AUM_COLUMN, "")).strip()
            or isin not in fallback_by_isin
            or issuer in JUSTETF_AUM_FALLBACK_BLOCKED_ISSUERS
        ):
            supplemented_rows.append(row)
            continue

        aum_m, aum_ccy = fallback_by_isin[isin]
        supplemented_row = dict(row)
        supplemented_row[TOTAL_AUM_COLUMN] = aum_m
        if aum_ccy:
            supplemented_row["AUM CCY"] = aum_ccy
        supplemented_rows.append(supplemented_row)

    return supplemented_rows


def dedupe_exact_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, ...]] = set()
    deduped_rows: list[dict[str, str]] = []

    for row in rows:
        key = tuple(str(row.get(column, "")).strip() for column in OUTPUT_COLUMNS)
        if key in seen:
            continue
        seen.add(key)
        deduped_rows.append(row)

    return deduped_rows


def validate_rows(provider_name: str, rows: list[dict[str, str]]) -> dict[str, int]:
    expected_columns = set(OUTPUT_COLUMNS)
    missing_counts = {column: 0 for column in OUTPUT_COLUMNS}

    for row in rows:
        row_columns = set(row.keys())
        if row_columns != expected_columns:
            missing_columns = expected_columns - row_columns
            extra_columns = row_columns - expected_columns
            raise ValueError(
                f"{provider_name} row schema mismatch. "
                f"Missing columns: {sorted(missing_columns)}. "
                f"Extra columns: {sorted(extra_columns)}."
            )

        for column in OUTPUT_COLUMNS:
            if not str(row.get(column, "")).strip():
                missing_counts[column] += 1

    return missing_counts


def normalize_isin(value: object | None) -> str | None:
    if value is None:
        return None

    normalized = str(value)
    for invisible_character in INVISIBLE_ISIN_CHARACTERS:
        normalized = normalized.replace(invisible_character, "")
    normalized = normalized.strip().upper()
    normalized = INTERNAL_SPACE_PATTERN.sub("", normalized)
    return normalized or None


def canonicalize_header(value: object | None) -> str:
    if value is None:
        return ""

    normalized = str(value)
    for invisible_character in INVISIBLE_ISIN_CHARACTERS:
        normalized = normalized.replace(invisible_character, " ")
    normalized = normalized.strip().lower()
    return HEADER_NORMALIZE_PATTERN.sub("", normalized)


def detect_isin_column_name(columns: list[str] | tuple[str, ...]) -> str:
    normalized_columns = [(column, canonicalize_header(column)) for column in columns]
    for candidate in ISIN_HEADER_CANDIDATES:
        for original_column, normalized_column in normalized_columns:
            if normalized_column == candidate:
                return original_column
    raise ValueError(f"Could not detect an ISIN column in columns: {list(columns)}")


def column_index_from_ref(cell_ref: str) -> int:
    match = CELL_REF_PATTERN.match(cell_ref)
    if not match:
        return -1

    column_letters = match.group(1)
    index = 0
    for character in column_letters:
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


def get_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    value = cell.find("main:v", XML_NS)
    inline_text = cell.find("main:is", XML_NS)
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr" and inline_text is not None:
        return "".join(node.text or "" for node in inline_text.iterfind(".//main:t", XML_NS)).strip()

    if value is None or value.text is None:
        return ""

    raw_value = value.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)].strip()
        except (ValueError, IndexError):
            return ""
    return raw_value


def read_sheet_rows(sheet_root: ET.Element, shared_strings: list[str]) -> list[dict[int, str]]:
    sheet_rows: list[dict[int, str]] = []
    for row in sheet_root.findall("main:sheetData/main:row", XML_NS):
        row_values: dict[int, str] = {}
        for cell in row.findall("main:c", XML_NS):
            column_index = column_index_from_ref(cell.attrib.get("r", ""))
            if column_index < 0:
                continue
            row_values[column_index] = get_cell_text(cell, shared_strings)
        sheet_rows.append(row_values)
    return sheet_rows


def load_allowed_isins(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Required ISIN filter file not found: {path}")

    allowed_isins: set[str] = set()
    try:
        with zipfile.ZipFile(path) as workbook_zip:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in workbook_zip.namelist():
                shared_strings_root = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
                for item in shared_strings_root.findall("main:si", XML_NS):
                    shared_strings.append(
                        "".join(node.text or "" for node in item.iterfind(".//main:t", XML_NS)).strip()
                    )

            workbook_root = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
            relationships_root = ET.fromstring(workbook_zip.read("xl/_rels/workbook.xml.rels"))
            relationship_map = {
                relation.attrib["Id"]: relation.attrib["Target"]
                for relation in relationships_root.findall("rel:Relationship", XML_NS)
            }

            for sheet in workbook_root.findall("main:sheets/main:sheet", XML_NS):
                relationship_id = sheet.attrib.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                    "",
                )
                target = relationship_map.get(relationship_id)
                if not target:
                    continue

                sheet_root = ET.fromstring(workbook_zip.read(f"xl/{target}"))
                sheet_rows = read_sheet_rows(sheet_root, shared_strings)
                if not sheet_rows:
                    continue

                header_row_index = next(
                    (
                        index
                        for index, row_values in enumerate(sheet_rows[:25])
                        if any(canonicalize_header(value) in ISIN_HEADER_CANDIDATES for value in row_values.values())
                    ),
                    None,
                )
                if header_row_index is None:
                    continue

                header_row = sheet_rows[header_row_index]
                normalized_headers = {
                    column_index: canonicalize_header(header_value)
                    for column_index, header_value in header_row.items()
                }
                isin_column_index = next(
                    (
                        column_index
                        for column_index, normalized_header in normalized_headers.items()
                        if normalized_header in ISIN_HEADER_CANDIDATES
                    ),
                    None,
                )
                if isin_column_index is None:
                    continue

                for row_values in sheet_rows[header_row_index + 1 :]:
                    isin = normalize_isin(row_values.get(isin_column_index))
                    if isin:
                        allowed_isins.add(isin)
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot read the ISIN filter file because it is locked: {path}. "
            "Please close ISIN-list.xlsx and run the pipeline again."
        ) from exc

    if not allowed_isins:
        raise ValueError(f"No ISIN values were loaded from {path}")

    return allowed_isins


def collect_normalized_isins(rows: list[dict[str, str]], isin_column_name: str) -> set[str]:
    return {
        normalized_isin
        for normalized_isin in (normalize_isin(row.get(isin_column_name)) for row in rows)
        if normalized_isin
    }


def collect_missing_whitelist_isins(
    rows: list[dict[str, str]],
    isin_column_name: str,
    whitelist_isins: set[str],
) -> list[str]:
    final_isins = collect_normalized_isins(rows, isin_column_name)
    return sorted(whitelist_isins - final_isins)


def collect_rows_with_missing_aum(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    missing_rows: list[dict[str, str]] = []
    for row in rows:
        if not str(row.get(TOTAL_AUM_COLUMN, "")).strip():
            missing_rows.append(row)
    return missing_rows


def identify_rows_with_missing_aum(rows: list[dict[str, str]]) -> list[str]:
    identifiers: list[str] = []
    for row in rows:
        issuer = clean_display_text(row.get("Issuer")) or "Unknown issuer"
        isin = normalize_isin(row.get("ISIN")) or "NO-ISIN"
        identifiers.append(f"{issuer} / {isin}")
    return identifiers


def verify_final_coverage(
    rows: list[dict[str, str]],
    whitelist_isins: set[str],
    isin_column_name: str,
) -> FinalCoverageSummary:
    missing_isins = collect_missing_whitelist_isins(rows, isin_column_name, whitelist_isins)
    rows_with_missing_aum = collect_rows_with_missing_aum(rows)

    return FinalCoverageSummary(
        missing_expected_isins=tuple(missing_isins),
        missing_aum_identifiers=tuple(identify_rows_with_missing_aum(rows_with_missing_aum)),
    )


def should_bypass_final_isin_filter(row: dict[str, str]) -> bool:
    issuer = str(row.get("Issuer", "")).strip()
    return issuer in ISIN_FILTER_BYPASS_ISSUERS


def validate_whitelisted_rows(rows: list[dict[str, str]], isin_column_name: str, whitelist_isins: set[str]) -> list[str]:
    final_isins = collect_normalized_isins(
        [row for row in rows if not should_bypass_final_isin_filter(row)],
        isin_column_name,
    )
    return sorted(final_isins - whitelist_isins)


def apply_final_isin_whitelist(
    rows: list[dict[str, str]],
    whitelist_isins: set[str],
) -> tuple[list[dict[str, str]], IsinFilterSummary]:
    columns = list(rows[0].keys()) if rows else list(OUTPUT_COLUMNS)
    isin_column_name = detect_isin_column_name(columns)

    final_isins_before_filtering = collect_normalized_isins(rows, isin_column_name)

    filtered_rows: list[dict[str, str]] = []
    removed_rows: list[dict[str, str]] = []
    for row in rows:
        if should_bypass_final_isin_filter(row):
            filtered_rows.append(row)
            continue

        normalized_isin = normalize_isin(row.get(isin_column_name))
        if normalized_isin and normalized_isin in whitelist_isins:
            filtered_rows.append(row)
        else:
            removed_rows.append(row)

    final_isins_after_filtering = collect_normalized_isins(filtered_rows, isin_column_name)
    removed_isins = collect_normalized_isins(removed_rows, isin_column_name)
    unexpected_isins = sorted(final_isins_after_filtering - whitelist_isins)

    summary = IsinFilterSummary(
        whitelist_unique_isin_count=len(whitelist_isins),
        final_rows_before_filtering=len(rows),
        final_unique_isins_before_filtering=len(final_isins_before_filtering),
        final_rows_after_filtering=len(filtered_rows),
        final_unique_isins_after_filtering=len(final_isins_after_filtering),
        removed_rows_count=len(removed_rows),
        removed_unique_isin_count=len(removed_isins),
        unexpected_isin_count_after_filtering=len(unexpected_isins),
        isin_column_name=isin_column_name,
    )
    return filtered_rows, summary


def build_pipelines(include_all_funds: bool) -> dict[str, ProviderPipeline]:
    return {
        "ishares": ProviderPipeline(
            name="iShares",
            downloader=download_etf_list,
            extractor=lambda input_path: extract_ishares_rows(input_path, include_all_funds=include_all_funds),
            input_dir=ISHARES_INPUT_DIR,
            output_dir=ISHARES_INPUT_DIR,
            output_filename=(
                "ishares_selected_fields_all_funds.csv"
                if include_all_funds
                else "ishares_selected_fields_etf_only.csv"
            ),
            latest_download_finder=find_latest_ishares_download,
            source_row_parser=parse_ishares_source_rows,
        ),
        "xtrackers": ProviderPipeline(
            name="Xtrackers",
            downloader=download_xtrackers_file,
            extractor=extract_xtrackers_rows,
            input_dir=XTRACKERS_INPUT_DIR,
            output_dir=XTRACKERS_INPUT_DIR,
            output_filename="xtrackers_selected_fields.csv",
            latest_download_finder=find_latest_xtrackers_download,
            source_row_parser=parse_xtrackers_source_rows,
        ),
        "amundi": ProviderPipeline(
            name="Amundi",
            downloader=download_amundi_file,
            extractor=extract_amundi_rows,
            input_dir=AMUNDI_INPUT_DIR,
            output_dir=AMUNDI_INPUT_DIR,
            output_filename="amundi_selected_fields.csv",
            latest_download_finder=find_latest_amundi_download,
            source_row_parser=parse_amundi_source_rows,
        ),
        "fidelity": ProviderPipeline(
            name="Fidelity",
            downloader=download_fidelity_file,
            extractor=extract_fidelity_rows,
            input_dir=FIDELITY_INPUT_DIR,
            output_dir=FIDELITY_INPUT_DIR,
            output_filename="fidelity_selected_fields.csv",
            latest_download_finder=find_latest_fidelity_download,
            source_row_parser=parse_fidelity_source_rows,
        ),
        "spdr": ProviderPipeline(
            name="SPDR",
            downloader=download_spdr_file,
            extractor=extract_spdr_rows,
            input_dir=SPDR_INPUT_DIR,
            output_dir=SPDR_INPUT_DIR,
            output_filename="spdr_selected_fields.csv",
            latest_download_finder=find_latest_spdr_download,
            source_row_parser=parse_spdr_source_rows,
        ),
        "hsbc": ProviderPipeline(
            name="HSBC",
            downloader=download_hsbc_file,
            extractor=extract_hsbc_rows,
            input_dir=HSBC_INPUT_DIR,
            output_dir=HSBC_INPUT_DIR,
            output_filename="hsbc_selected_fields.csv",
            latest_download_finder=find_latest_hsbc_download,
            source_row_parser=parse_hsbc_source_rows,
        ),
        "ubs": ProviderPipeline(
            name="UBS",
            downloader=download_ubs_file,
            extractor=extract_ubs_rows,
            input_dir=UBS_INPUT_DIR,
            output_dir=UBS_INPUT_DIR,
            output_filename="ubs_selected_fields.csv",
            latest_download_finder=find_latest_ubs_download,
            source_row_parser=parse_ubs_source_rows,
        ),
        "invesco": ProviderPipeline(
            name="Invesco",
            downloader=download_invesco_file,
            extractor=extract_invesco_rows,
            input_dir=INVESCO_INPUT_DIR,
            output_dir=INVESCO_INPUT_DIR,
            output_filename="invesco_selected_fields.csv",
            latest_download_finder=find_latest_invesco_download,
            source_row_parser=parse_invesco_source_rows,
        ),
        "jpmorgan": ProviderPipeline(
            name="J.P. Morgan",
            downloader=download_jpmorgan_file,
            extractor=extract_jpmorgan_rows,
            input_dir=JPMORGAN_INPUT_DIR,
            output_dir=JPMORGAN_INPUT_DIR,
            output_filename="jpmorgan_selected_fields.csv",
            latest_download_finder=find_latest_jpmorgan_download,
            source_row_parser=parse_jpmorgan_source_rows,
        ),
        "landg": ProviderPipeline(
            name="L&G",
            downloader=download_landg_file,
            extractor=extract_landg_rows,
            input_dir=LANDG_INPUT_DIR,
            output_dir=LANDG_INPUT_DIR,
            output_filename="landg_selected_fields.csv",
            latest_download_finder=find_latest_landg_download,
            source_row_parser=parse_landg_source_rows,
        ),
        "palmersquare": ProviderPipeline(
            name="Palmer Square",
            downloader=download_palmersquare_file,
            extractor=extract_palmersquare_rows,
            input_dir=PALMERSQUARE_INPUT_DIR,
            output_dir=PALMERSQUARE_INPUT_DIR,
            output_filename="palmersquare_selected_fields.csv",
            latest_download_finder=find_latest_palmersquare_download,
            source_row_parser=parse_palmersquare_source_rows,
        ),
        "vaneck": ProviderPipeline(
            name="VanEck",
            downloader=download_vaneck_file,
            extractor=extract_vaneck_rows,
            input_dir=VANECK_INPUT_DIR,
            output_dir=VANECK_INPUT_DIR,
            output_filename="vaneck_selected_fields.csv",
            latest_download_finder=find_latest_vaneck_download,
            source_row_parser=parse_vaneck_source_rows,
        ),
        "franklintempleton": ProviderPipeline(
            name="Franklin Templeton",
            downloader=download_franklintempleton_file,
            extractor=extract_franklin_templeton_rows,
            input_dir=FRANKLIN_TEMPLETON_INPUT_DIR,
            output_dir=FRANKLIN_TEMPLETON_INPUT_DIR,
            output_filename="franklintempleton_selected_fields.csv",
            latest_download_finder=find_latest_franklin_templeton_download,
            source_row_parser=parse_franklin_templeton_source_rows,
        ),
        "wisdomtree": ProviderPipeline(
            name="WisdomTree",
            downloader=download_wisdomtree_file,
            extractor=extract_wisdomtree_rows,
            input_dir=WISDOMTREE_INPUT_DIR,
            output_dir=WISDOMTREE_INPUT_DIR,
            output_filename="wisdomtree_selected_fields.csv",
            latest_download_finder=find_latest_wisdomtree_download,
            source_row_parser=parse_wisdomtree_source_rows,
        ),
        "vanguard": ProviderPipeline(
            name="Vanguard",
            downloader=download_vanguard_file,
            extractor=extract_vanguard_rows,
            input_dir=VANGUARD_INPUT_DIR,
            output_dir=VANGUARD_INPUT_DIR,
            output_filename="vanguard_selected_fields.csv",
            latest_download_finder=find_latest_vanguard_download,
            source_row_parser=parse_vanguard_source_rows,
        ),
        "firsttrust": ProviderPipeline(
            name="First Trust",
            downloader=download_firsttrust_file,
            extractor=extract_firsttrust_rows,
            input_dir=FIRSTTRUST_INPUT_DIR,
            output_dir=FIRSTTRUST_INPUT_DIR,
            output_filename="firsttrust_selected_fields.csv",
            latest_download_finder=find_latest_firsttrust_download,
            source_row_parser=parse_firsttrust_source_rows,
        ),
        "hanetf": ProviderPipeline(
            name="HANetf",
            downloader=download_hanetf_file,
            extractor=extract_hanetf_rows,
            input_dir=HANETF_INPUT_DIR,
            output_dir=HANETF_INPUT_DIR,
            output_filename="hanetf_selected_fields.csv",
            latest_download_finder=find_latest_hanetf_download,
            source_row_parser=parse_hanetf_source_rows,
        ),
        "globalx": ProviderPipeline(
            name="Global X ETFs",
            downloader=download_globalx_file,
            extractor=extract_globalx_rows,
            input_dir=GLOBALX_INPUT_DIR,
            output_dir=GLOBALX_INPUT_DIR,
            output_filename="globalx_selected_fields.csv",
            latest_download_finder=find_latest_globalx_download,
            source_row_parser=parse_globalx_source_rows,
        ),
        "finex": ProviderPipeline(
            name="FinEx",
            downloader=download_finex_file,
            extractor=extract_finex_rows,
            input_dir=FINEX_INPUT_DIR,
            output_dir=FINEX_INPUT_DIR,
            output_filename="finex_selected_fields.csv",
            latest_download_finder=find_latest_finex_download,
            source_row_parser=parse_finex_source_rows,
        ),
        "imgp": ProviderPipeline(
            name="iM Global Partner",
            downloader=download_imgp_file,
            extractor=extract_imgp_rows,
            input_dir=IMGP_INPUT_DIR,
            output_dir=IMGP_INPUT_DIR,
            output_filename="imgp_selected_fields.csv",
            latest_download_finder=find_latest_imgp_download,
            source_row_parser=parse_imgp_source_rows,
        ),
        "abrdn": ProviderPipeline(
            name="abrdn",
            downloader=lambda: run_sync_downloader(scrape_abrdn_etfs),
            extractor=extract_abrdn_rows,
            input_dir=ABRDN_INPUT_DIR,
            output_dir=ABRDN_INPUT_DIR,
            output_filename="abrdn_selected_fields.csv",
            latest_download_finder=find_latest_abrdn_download,
            source_row_parser=parse_abrdn_source_rows,
        ),
        "alliancebernstein": ProviderPipeline(
            name="Alliance Bernstein",
            downloader=lambda: run_sync_downloader(scrape_alliance_bernstein_etfs),
            extractor=extract_alliance_bernstein_rows,
            input_dir=ALLIANCE_BERNSTEIN_INPUT_DIR,
            output_dir=ALLIANCE_BERNSTEIN_INPUT_DIR,
            output_filename="alliance_bernstein_selected_fields.csv",
            latest_download_finder=find_latest_alliance_bernstein_download,
            source_row_parser=parse_alliance_bernstein_source_rows,
        ),
        "alphaucits": ProviderPipeline(
            name="Alpha Ucits",
            downloader=lambda: run_sync_downloader(scrape_alpha_ucits),
            extractor=extract_alpha_ucits_rows,
            input_dir=ALPHA_UCITS_INPUT_DIR,
            output_dir=ALPHA_UCITS_INPUT_DIR,
            output_filename="alpha_ucits_selected_fields.csv",
            latest_download_finder=find_latest_alpha_ucits_download,
            source_row_parser=parse_alpha_ucits_source_rows,
        ),
        "americancenturyinvestments": ProviderPipeline(
            name="American Century Investments",
            downloader=download_american_century_investments_file,
            extractor=extract_american_century_rows,
            input_dir=AMERICAN_CENTURY_INPUT_DIR,
            output_dir=AMERICAN_CENTURY_INPUT_DIR,
            output_filename="american_century_investments_selected_fields.csv",
            latest_download_finder=find_latest_american_century_download,
            source_row_parser=parse_american_century_source_rows,
        ),
        "ark": ProviderPipeline(
            name="ARK Investment Management",
            downloader=download_ark_file,
            extractor=extract_ark_rows,
            input_dir=ARK_INPUT_DIR,
            output_dir=ARK_INPUT_DIR,
            output_filename="ark_selected_fields.csv",
            latest_download_finder=find_latest_ark_download,
            source_row_parser=parse_ark_source_rows,
        ),
        "bnpparibas": ProviderPipeline(
            name="BNP Paribas Asset Management",
            downloader=download_bnpparibas_file,
            extractor=extract_bnp_rows,
            input_dir=BNP_INPUT_DIR,
            output_dir=BNP_INPUT_DIR,
            output_filename="bnpparibas_selected_fields.csv",
            latest_download_finder=find_latest_bnp_download,
            source_row_parser=parse_bnp_source_rows,
        ),
        "columbia": ProviderPipeline(
            name="Columbia",
            downloader=download_columbia_file,
            extractor=extract_columbia_rows,
            input_dir=COLUMBIA_INPUT_DIR,
            output_dir=COLUMBIA_INPUT_DIR,
            output_filename="columbia_selected_fields.csv",
            latest_download_finder=find_latest_columbia_download,
            source_row_parser=parse_columbia_source_rows,
        ),
        "connectetfs": ProviderPipeline(
            name="Connect ETFs",
            downloader=download_connect_etfs_file,
            extractor=extract_connect_etfs_rows,
            input_dir=CONNECT_ETFS_INPUT_DIR,
            output_dir=CONNECT_ETFS_INPUT_DIR,
            output_filename="connect_etfs_selected_fields.csv",
            latest_download_finder=find_latest_connect_etfs_download,
            source_row_parser=parse_connect_etfs_source_rows,
        ),
        "dimensional": ProviderPipeline(
            name="Dimensional",
            downloader=download_dimensional_file,
            extractor=extract_dimensional_rows,
            input_dir=DIMENSIONAL_INPUT_DIR,
            output_dir=DIMENSIONAL_INPUT_DIR,
            output_filename="dimensional_selected_fields.csv",
            latest_download_finder=find_latest_dimensional_download,
            source_row_parser=parse_dimensional_source_rows,
        ),
        "goldmansachs": ProviderPipeline(
            name="Goldman Sachs",
            downloader=download_goldman_sachs_file,
            extractor=extract_goldman_sachs_rows,
            input_dir=GOLDMAN_SACHS_INPUT_DIR,
            output_dir=GOLDMAN_SACHS_INPUT_DIR,
            output_filename="goldmansachs_selected_fields.csv",
            latest_download_finder=find_latest_goldman_sachs_download,
            source_row_parser=parse_goldman_sachs_source_rows,
        ),
        "janushenderson": ProviderPipeline(
            name="Janus Henderson",
            downloader=download_janus_henderson_file,
            extractor=extract_janus_henderson_rows,
            input_dir=JANUS_HENDERSON_INPUT_DIR,
            output_dir=JANUS_HENDERSON_INPUT_DIR,
            output_filename="janushenderson_selected_fields.csv",
            latest_download_finder=find_latest_janus_henderson_download,
            source_row_parser=parse_janus_henderson_source_rows,
        ),
        "kraneshares": ProviderPipeline(
            name="KraneShares",
            downloader=download_kraneshares_ucits,
            extractor=extract_kraneshares_rows,
            input_dir=KRANESHARES_INPUT_DIR,
            output_dir=KRANESHARES_INPUT_DIR,
            output_filename="kraneshares_selected_fields.csv",
            latest_download_finder=find_latest_kraneshares_download,
            source_row_parser=parse_kraneshares_source_rows,
        ),
        "mg": ProviderPipeline(
            name="M&G",
            downloader=download_mg_file,
            extractor=extract_mg_rows,
            input_dir=MG_INPUT_DIR,
            output_dir=MG_INPUT_DIR,
            output_filename="mg_selected_fields.csv",
            latest_download_finder=find_latest_mg_download,
            source_row_parser=parse_mg_source_rows,
        ),
        "marketaccess": ProviderPipeline(
            name="Market Access",
            downloader=download_market_access_file,
            extractor=extract_market_access_rows,
            input_dir=MARKET_ACCESS_INPUT_DIR,
            output_dir=MARKET_ACCESS_INPUT_DIR,
            output_filename="market_access_selected_fields.csv",
            latest_download_finder=find_latest_market_access_download,
            source_row_parser=parse_market_access_source_rows,
        ),
        "nordea": ProviderPipeline(
            name="Nordea",
            downloader=lambda: run_sync_downloader(scrape_nordea),
            extractor=extract_nordea_rows,
            input_dir=NORDEA_INPUT_DIR,
            output_dir=NORDEA_INPUT_DIR,
            output_filename="nordea_selected_fields.csv",
            latest_download_finder=find_latest_nordea_download,
            source_row_parser=parse_nordea_source_rows,
        ),
        "ossiam": ProviderPipeline(
            name="Ossiam",
            downloader=download_ossiam_file,
            extractor=extract_ossiam_rows,
            input_dir=OSSIAM_INPUT_DIR,
            output_dir=OSSIAM_INPUT_DIR,
            output_filename="ossiam_selected_fields.csv",
            latest_download_finder=find_latest_ossiam_download,
            source_row_parser=parse_listing_rows_json,
        ),
        "paceretfs": ProviderPipeline(
            name="Pacer ETFs",
            downloader=download_pacer_etfs_file,
            extractor=extract_pacer_etfs_rows,
            input_dir=PACER_ETFS_INPUT_DIR,
            output_dir=PACER_ETFS_INPUT_DIR,
            output_filename="pacer_etfs_selected_fields.csv",
            latest_download_finder=find_latest_pacer_etfs_download,
            source_row_parser=parse_pacer_etfs_source_rows,
        ),
        "pimco": ProviderPipeline(
            name="PIMCO",
            downloader=download_pimco_file,
            extractor=extract_pimco_rows,
            input_dir=PIMCO_INPUT_DIR,
            output_dir=PIMCO_INPUT_DIR,
            output_filename="pimco_selected_fields.csv",
            latest_download_finder=find_latest_pimco_download,
            source_row_parser=parse_pimco_source_rows,
        ),
        "robeco": ProviderPipeline(
            name="Robeco",
            downloader=download_robeco_file,
            extractor=extract_robeco_rows,
            input_dir=ROBECO_INPUT_DIR,
            output_dir=ROBECO_INPUT_DIR,
            output_filename="robeco_selected_fields.csv",
            latest_download_finder=find_latest_robeco_download,
            source_row_parser=parse_robeco_source_rows,
        ),
        "schroders": ProviderPipeline(
            name="Schroders",
            downloader=download_schroders_file,
            extractor=extract_schroders_rows,
            input_dir=SCHRODERS_INPUT_DIR,
            output_dir=SCHRODERS_INPUT_DIR,
            output_filename="schroders_selected_fields.csv",
            latest_download_finder=find_latest_schroders_download,
            source_row_parser=parse_schroders_source_rows,
        ),
        "waystone": ProviderPipeline(
            name="Waystone",
            downloader=download_waystone_file,
            extractor=extract_waystone_rows,
            input_dir=WAYSTONE_INPUT_DIR,
            output_dir=WAYSTONE_INPUT_DIR,
            output_filename="waystone_selected_fields.csv",
            latest_download_finder=find_latest_waystone_download,
            source_row_parser=parse_listing_rows_json,
        ),
    }


async def prepare_input_file(pipeline: ProviderPipeline, use_latest_downloads: bool) -> PreparedInput:
    if use_latest_downloads:
        return PreparedInput(
            path=pipeline.latest_download_finder(pipeline.input_dir),
            status="REUSED",
            note="Using latest saved provider file.",
        )
    return PreparedInput(
        path=await pipeline.downloader(),
        status="OK",
    )


async def run_provider(
    pipeline: ProviderPipeline,
    use_latest_downloads: bool,
    run_date: str,
    whitelist_isins: set[str],
) -> tuple[list[dict[str, str]], ProviderRunReport]:
    started_at = time.perf_counter()
    captured_output = io.StringIO()

    with redirect_stdout(captured_output), redirect_stderr(captured_output):
        prepared_input = await prepare_input_file(pipeline, use_latest_downloads)
        input_path = prepared_input.path
        source_rows = pipeline.source_row_parser(input_path)
        note = prepared_input.note
        source_status = prepared_input.status

        if not source_rows:
            raise ValueError(
                f"{pipeline.name} live source returned zero rows. "
                "Refusing to reuse an older provider file automatically."
            )

        source_row_count = len(source_rows)
        rows = dedupe_exact_rows(
            supplement_missing_aum_from_justetf(
                normalize_output_rows(pipeline.extractor(input_path))
            )
        )
        missing_counts = validate_rows(pipeline.name, rows)
        output_path = resolve_provider_output_path(
            pipeline,
            input_path,
            run_date,
            use_latest_downloads,
        )
        output_path = write_csv_with_fallback(output_path, rows)
        removed_paths = enforce_provider_two_file_layout(input_path, output_path)
        if removed_paths:
            removed_labels = ", ".join(path.name for path in removed_paths)
            cleanup_note = f"Removed extra run files: {removed_labels}"
            note = f"{note} {cleanup_note}".strip() if note else cleanup_note

    valid_isin_count = sum(1 for row in rows if normalize_isin(row.get("ISIN")))
    provider_match_count = sum(
        1 for row in rows if normalize_isin(row.get("ISIN")) in whitelist_isins
    )
    unmatched_row_count = max(0, len(rows) - provider_match_count)
    source_label, source_method, source_url = summarize_source_metadata(input_path)
    discovery_status = "OK" if source_row_count > 0 else "EMPTY"
    extraction_status = evaluate_extraction_status(len(rows), missing_counts)
    isin_filter_status = "OK" if provider_match_count > 0 or len(rows) == 0 else "NO MATCHES"
    output_status = "OK" if output_path.exists() else "FAILED"
    result_status = evaluate_result_status(len(rows), provider_match_count, missing_counts)
    duration_seconds = time.perf_counter() - started_at

    report = ProviderRunReport(
        provider_name=pipeline.name,
        input_path=input_path,
        output_path=output_path,
        source_row_count=source_row_count,
        extracted_row_count=len(rows),
        missing_counts=missing_counts,
        reference_isin_count=len(whitelist_isins),
        provider_match_count=provider_match_count,
        unmatched_row_count=unmatched_row_count,
        valid_isin_count=valid_isin_count,
        source_label=source_label,
        source_method=source_method,
        source_url=source_url,
        source_status=source_status,
        discovery_status=discovery_status,
        extraction_status=extraction_status,
        isin_filter_status=isin_filter_status,
        output_status=output_status,
        result_status=result_status,
        duration_seconds=duration_seconds,
        note=note,
    )
    print_provider_report(report, run_date)
    return rows, report


async def async_main() -> int:
    args = parse_args()
    pipelines = build_pipelines(include_all_funds=not args.etf_only)
    run_date = datetime.now().strftime("%Y-%m-%d")
    run_dir = build_run_dir(run_date)
    run_folder_name = run_dir.name
    combined_output_path = run_dir / COMBINED_FILENAME
    whitelist_isins = load_allowed_isins(ISIN_FILTER_PATH)

    successes: list[ProviderRunReport] = []
    failures: list[tuple[str, Exception]] = []
    combined_rows: list[dict[str, str]] = []

    previous_run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    os.environ[RUN_FOLDER_ENV_VAR] = run_folder_name
    try:
        for provider_key in args.providers:
            pipeline = pipelines[provider_key]
            try:
                rows, report = await run_provider(
                    pipeline,
                    args.use_latest_downloads,
                    run_folder_name,
                    whitelist_isins,
                )
                combined_rows.extend(rows)
                successes.append(report)
            except Exception as exc:
                failures.append((pipeline.name, exc))
                print(f"[ERROR] {pipeline.name} failed: {exc}")
                if args.stop_on_error:
                    break
    finally:
        if previous_run_folder_name is None:
            os.environ.pop(RUN_FOLDER_ENV_VAR, None)
        else:
            os.environ[RUN_FOLDER_ENV_VAR] = previous_run_folder_name

    filtered_rows, filter_summary = apply_final_isin_whitelist(combined_rows, whitelist_isins)
    filtered_rows = dedupe_exact_rows(filtered_rows)
    print()
    print("=== Final ISIN Whitelist Filter ===")
    print(f"Whitelist unique ISIN count: {filter_summary.whitelist_unique_isin_count:,}")
    print(f"Final rows before filtering: {filter_summary.final_rows_before_filtering:,}")
    print(f"Final unique ISINs before filtering: {filter_summary.final_unique_isins_before_filtering:,}")
    print(f"Final rows after filtering: {filter_summary.final_rows_after_filtering:,}")
    print(f"Final unique ISINs after filtering: {filter_summary.final_unique_isins_after_filtering:,}")
    print(f"Removed rows count: {filter_summary.removed_rows_count:,}")
    print(f"Removed unique ISIN count: {filter_summary.removed_unique_isin_count:,}")
    print(f"Unexpected ISIN count after filtering: {filter_summary.unexpected_isin_count_after_filtering:,}")

    unexpected_isins_pre_save = validate_whitelisted_rows(
        filtered_rows,
        filter_summary.isin_column_name,
        whitelist_isins,
    )
    if unexpected_isins_pre_save:
        print("Unexpected non-whitelisted ISINs after final filter:")
        for isin in unexpected_isins_pre_save[:100]:
            print(f"  {isin}")
        raise ValueError(
            "Final ETF rows still contain non-whitelisted ISINs before saving: "
            + ", ".join(unexpected_isins_pre_save[:25])
        )

    combined_output_path = write_csv_with_fallback(
        combined_output_path,
        filtered_rows,
        output_label="combined pipeline output",
    )
    coverage_summary = verify_final_coverage(
        filtered_rows,
        whitelist_isins,
        filter_summary.isin_column_name,
    )

    print()
    print("=== Summary ===")
    print(f"Run folder   : {run_dir}")
    print(f"Combined CSV : {combined_output_path}")
    print(f"Total rows   : {len(filtered_rows):,}")
    print(f"Whitelist ISINs: {filter_summary.whitelist_unique_isin_count:,} from {ISIN_FILTER_PATH}")
    print(f"Rows removed by ISIN filter: {filter_summary.removed_rows_count:,}")
    print(f"Rows with missing total AUM in final CSV: {len(coverage_summary.missing_aum_identifiers):,}")
    if coverage_summary.missing_aum_identifiers:
        print(f"Sample rows still missing {TOTAL_AUM_COLUMN}:")
        for identifier in coverage_summary.missing_aum_identifiers[:10]:
            print(f"  {identifier}")

    if successes:
        for report in successes:
            print(f"{report.provider_name}:")
            print(f"  Raw      -> {relative_display_path(report.input_path)}")
            print(f"  Selected -> {relative_display_path(report.output_path)}")
            print(f"  Extracted -> {report.extracted_row_count:,}")
            print(f"  Matches   -> {report.provider_match_count:,}")
            print(f"  Missing Total AUM -> {report.missing_counts.get(TOTAL_AUM_COLUMN, 0):,}")
            print(f"  Status -> {report.result_status}")

    if failures:
        for provider_name, exc in failures:
            print(f"{provider_name}: FAILED -> {exc}")
        return 1

    if coverage_summary.missing_aum_identifiers:
        print(
            "Final coverage verification failed: the aggregated CSV still has "
            f"{len(coverage_summary.missing_aum_identifiers):,} row(s) with blank {TOTAL_AUM_COLUMN}."
        )
        return 1

    print("All selected providers completed successfully.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
