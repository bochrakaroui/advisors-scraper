"""Run all ETF downloaders and build one combined CSV for the run."""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
import xml.etree.ElementTree as ET

from providers.amundi.extract_amundi_fields import (
    INPUT_DIR as AMUNDI_INPUT_DIR,
    extract_rows as extract_amundi_rows,
    find_latest_download as find_latest_amundi_download,
    parse_xlsx_rows as parse_amundi_source_rows,
)
from providers.output_schema import (
    OUTPUT_COLUMNS,
    extract_row_isin,
    infer_consistent_row_currency,
    infer_aum_currency_from_row,
)
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
from providers.American_Century_Investments.extract_american_century_investments_fields import (
    INPUT_DIR as AMERICAN_CENTURY_INVESTMENTS_INPUT_DIR,
    extract_rows as extract_american_century_investments_rows,
    find_latest_download as find_latest_american_century_investments_download,
    parse_snapshot_rows as parse_american_century_investments_source_rows,
)
from providers.Columbia.extract_columbia_fields import (
    INPUT_DIR as COLUMBIA_INPUT_DIR,
    extract_rows as extract_columbia_rows,
    find_latest_download as find_latest_columbia_download,
    parse_snapshot_rows as parse_columbia_source_rows,
)
from providers.ARK_Investment_Management.extract_ARK_fields import (
    INPUT_DIR as ARK_INPUT_DIR,
    extract_rows as extract_ark_rows,
    find_latest_download as find_latest_ark_download,
    parse_snapshot_rows as parse_ark_source_rows,
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
from providers.Ossiam.extract_ossiam_fields import (
    INPUT_DIR as OSSIAM_INPUT_DIR,
    extract_rows as extract_ossiam_rows,
    find_latest_download as find_latest_ossiam_download,
    parse_snapshot as parse_ossiam_source_rows,
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
from providers.Expat.extract_expat_fields import (
    INPUT_DIR as EXPAT_INPUT_DIR,
    extract_rows as extract_expat_rows,
    find_latest_download as find_latest_expat_download,
    parse_snapshot_rows as parse_expat_source_rows,
)
from providers.Waystone.extract_waystone_fields import (
    INPUT_DIR as WAYSTONE_INPUT_DIR,
    extract_rows as extract_waystone_rows,
    find_latest_download as find_latest_waystone_download,
    load_rows as parse_waystone_source_rows,
)
from providers.KraneShares.extract_kraneshares_fields import (
    INPUT_DIR as KRANESHARES_INPUT_DIR,
    extract_rows as extract_kraneshares_rows,
    find_latest_download as find_latest_kraneshares_download,
    parse_snapshot_rows as parse_kraneshares_source_rows,
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
from providers.Nordea.extract_Nordea_fields import (
    INPUT_DIR as NORDEA_INPUT_DIR,
    extract_rows as extract_nordea_rows,
    find_latest_download as find_latest_nordea_download,
    parse_snapshot_rows as parse_nordea_source_rows,
)
from providers.Pacer_ETFs.extract_pacer_etfs_fields import (
    INPUT_DIR as PACER_ETFS_INPUT_DIR,
    extract_rows as extract_pacer_etfs_rows,
    find_latest_download as find_latest_pacer_etfs_download,
    parse_snapshot_rows as parse_pacer_etfs_source_rows,
)
from providers.Market_Access.extract_market_access_fields import (
    INPUT_DIR as MARKET_ACCESS_INPUT_DIR,
    extract_rows as extract_market_access_rows,
    find_latest_download as find_latest_market_access_download,
    parse_snapshot_rows as parse_market_access_source_rows,
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
from scrapers.American_Century_Investments_extractor import download_american_century_investments_file
from scrapers.Columbia_extractor import download_columbia_file
from scrapers.BNP_Paribas_Asset_Management_extractor import download_bnpparibas_file
from scrapers.Goldman_Sachs_extractor import download_goldman_sachs_file
from scrapers.ARK_Investment_Management_extractor import download_ark_file
from scrapers.Robeco_extractor import download_robeco_file
from scrapers.PIMCO_extractor import download_pimco_file
from scrapers.KraneShares_extractor import download_kraneshares_ucits as download_kraneshares_file
from scrapers.MandG_extractor import download_mg_file
from scrapers.market_access_extractor import download_market_access_file
from scrapers.Schroders_extractor import download_schroders_file
from scrapers.Ossiam_extractor import download_ossiam_file
from scrapers.Connect_ETFs_extractor import download_connect_etfs_file
from scrapers.Dimensional_extractor import download_dimensional_file
from scrapers.Expat_extractor import download_expat_file
from scrapers.Waystone_extractor import download_waystone_file
from scrapers.abrdn_extractor import scrape_abrdn_etfs
from scrapers.Alliance_Bernstein_extractor import scrape_alliance_bernstein_etfs
from scrapers.Alpha_Ucits_extractor import scrape_alpha_ucits
from scrapers.Nordea_extractor import scrape_nordea
from scrapers.Pacer_ETFs_extractor import run as download_pacer_etfs_file

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "pipeline_runs"
COMBINED_FILENAME = "all_etf_fields.csv"
ISIN_FILTER_PATH = BASE_DIR / "ISIN-list.xlsx"
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"
JANUS_HENDERSON_SCRAPER_PATH = BASE_DIR / "scrapers" / "Janus_Henderson_extractor.py"
BNP_PARIBAS_EXTRACTOR_PATH = (
    BASE_DIR / "providers" / "BNP_Paribas_Asset_Management" / "extract_bnp_fields.py"
)
GOLDMAN_SACHS_EXTRACTOR_PATH = (
    BASE_DIR / "providers" / "Goldman_Sachs" / "extract_goldman_sachs_fields.py"
)
JANUS_HENDERSON_EXTRACTOR_PATH = (
    BASE_DIR / "providers" / "Janus_Henderson" / "extract_janus_henderson_fields.py"
)
PIMCO_EXTRACTOR_PATH = BASE_DIR / "providers" / "PIMCO" / "extract_pimco_fields.py"
MANDG_EXTRACTOR_PATH = BASE_DIR / "providers" / "M&G" / "extract_mg_fields.py"
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


def load_bnp_paribas_extractor_module():
    spec = importlib.util.spec_from_file_location(
        "bnp_paribas_asset_management_extract_bnp_fields",
        BNP_PARIBAS_EXTRACTOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(
            f"BNP Paribas extractor module is missing: {BNP_PARIBAS_EXTRACTOR_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_janus_henderson_scraper_module():
    spec = importlib.util.spec_from_file_location(
        "janus_henderson_scraper_module",
        JANUS_HENDERSON_SCRAPER_PATH,
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(
            f"Janus Henderson scraper module is missing: {JANUS_HENDERSON_SCRAPER_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_goldman_sachs_extractor_module():
    spec = importlib.util.spec_from_file_location(
        "goldman_sachs_extract_goldman_sachs_fields",
        GOLDMAN_SACHS_EXTRACTOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(
            f"Goldman Sachs extractor module is missing: {GOLDMAN_SACHS_EXTRACTOR_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_janus_henderson_extractor_module():
    spec = importlib.util.spec_from_file_location(
        "janus_henderson_extract_janus_henderson_fields",
        JANUS_HENDERSON_EXTRACTOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(
            f"Janus Henderson extractor module is missing: {JANUS_HENDERSON_EXTRACTOR_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_pimco_extractor_module():
    spec = importlib.util.spec_from_file_location(
        "pimco_extract_pimco_fields",
        PIMCO_EXTRACTOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"PIMCO extractor module is missing: {PIMCO_EXTRACTOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_mandg_extractor_module():
    spec = importlib.util.spec_from_file_location(
        "mandg_extract_mg_fields",
        MANDG_EXTRACTOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"M&G extractor module is missing: {MANDG_EXTRACTOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_janus_henderson_scraper_module = load_janus_henderson_scraper_module()
download_janus_henderson_file = _janus_henderson_scraper_module.download_janus_henderson_file

_bnp_paribas_extractor_module = load_bnp_paribas_extractor_module()
BNP_PARIBAS_INPUT_DIR = _bnp_paribas_extractor_module.INPUT_DIR
extract_bnp_paribas_rows = _bnp_paribas_extractor_module.extract_rows
find_latest_bnp_paribas_download = _bnp_paribas_extractor_module.find_latest_download
parse_bnp_paribas_source_rows = _bnp_paribas_extractor_module.parse_snapshot_rows

_goldman_sachs_extractor_module = load_goldman_sachs_extractor_module()
GOLDMAN_SACHS_INPUT_DIR = _goldman_sachs_extractor_module.INPUT_DIR
extract_goldman_sachs_rows = _goldman_sachs_extractor_module.extract_rows
find_latest_goldman_sachs_download = _goldman_sachs_extractor_module.find_latest_download
parse_goldman_sachs_source_rows = _goldman_sachs_extractor_module.parse_snapshot_rows

_janus_henderson_extractor_module = load_janus_henderson_extractor_module()
JANUS_HENDERSON_INPUT_DIR = _janus_henderson_extractor_module.INPUT_DIR
extract_janus_henderson_rows = _janus_henderson_extractor_module.extract_rows
find_latest_janus_henderson_download = _janus_henderson_extractor_module.find_latest_download
parse_janus_henderson_source_rows = _janus_henderson_extractor_module.parse_snapshot_rows

_pimco_extractor_module = load_pimco_extractor_module()
PIMCO_INPUT_DIR = _pimco_extractor_module.INPUT_DIR
extract_pimco_rows = _pimco_extractor_module.extract_rows
find_latest_pimco_download = _pimco_extractor_module.find_latest_download
parse_pimco_source_rows = _pimco_extractor_module.parse_snapshot_rows

_mandg_extractor_module = load_mandg_extractor_module()
MANDG_INPUT_DIR = _mandg_extractor_module.INPUT_DIR
extract_mandg_rows = _mandg_extractor_module.extract_rows
find_latest_mandg_download = _mandg_extractor_module.find_latest_download
parse_mandg_source_rows = _mandg_extractor_module.parse_snapshot_rows


async def download_abrdn_file() -> Path:
    return await asyncio.to_thread(scrape_abrdn_etfs)


async def download_alliance_bernstein_file() -> Path:
    return await asyncio.to_thread(scrape_alliance_bernstein_etfs)


async def download_alpha_ucits_file() -> Path:
    return await asyncio.to_thread(scrape_alpha_ucits)


async def download_nordea_file() -> Path:
    return await asyncio.to_thread(scrape_nordea)

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
    "americancenturyinvestments",
    "columbia",
    "bnpparibas",
    "goldmansachs",
    "janushenderson",
    "ark",
    "robeco",
    "pimco",
    "kraneshares",
    "mandg",
    "schroders",
    "ossiam",
    "connectetfs",
    "dimensional",
    "expat",
    "waystone",
    "abrdn",
    "alliancebernstein",
    "alphaucits",
    "nordea",
    "paceretfs",
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
    apply_isin_whitelist_to_provider_output: bool = False


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


def backfill_aum_currency(
    rows: list[dict[str, str]],
    source_rows: list[dict[str, object]],
) -> list[dict[str, str]]:
    source_rows_by_isin: dict[str, list[dict[str, object]]] = {}
    for source_row in source_rows:
        normalized_isin = normalize_isin(extract_row_isin(source_row))
        if not normalized_isin:
            continue
        source_rows_by_isin.setdefault(normalized_isin, []).append(source_row)

    enriched_rows: list[dict[str, str]] = []
    for row in rows:
        updated_row = dict(row)
        if str(updated_row.get("AUM CCY", "")).strip():
            enriched_rows.append(updated_row)
            continue

        normalized_isin = normalize_isin(updated_row.get("ISIN"))
        inferred_aum_currency = ""
        matching_source_rows = source_rows_by_isin.get(normalized_isin or "", [])
        for source_row in matching_source_rows:
            inferred_aum_currency = infer_aum_currency_from_row(source_row)
            if inferred_aum_currency:
                break

        if not inferred_aum_currency:
            inferred_aum_currency = infer_consistent_row_currency(matching_source_rows)

        updated_row["AUM CCY"] = inferred_aum_currency
        enriched_rows.append(updated_row)

    return enriched_rows


def normalize_output_date(value: object | None) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""

    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return cleaned


def normalize_date_column(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        updated_row = dict(row)
        updated_row["Date"] = normalize_output_date(updated_row.get("Date"))
        normalized_rows.append(updated_row)
    return normalized_rows


def try_find_latest_download(pipeline: ProviderPipeline) -> Path | None:
    try:
        return pipeline.latest_download_finder(pipeline.input_dir)
    except FileNotFoundError:
        return None


def is_network_related_error(exc: Exception) -> bool:
    message = str(exc).lower()
    type_name = type(exc).__name__.lower()
    network_type_markers = (
        "connectionerror",
        "proxyerror",
        "sslerror",
        "newconnectionerror",
    )
    markers = (
        "err_internet_disconnected",
        "err_network_access_denied",
        "name resolution",
        "failed to resolve",
        "getaddrinfo failed",
        "httpsconnectionpool(",
        "max retries exceeded",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "connection aborted",
        "connection reset",
        "connection refused",
        "ssl eof",
        "proxyerror",
        "read timed out",
        "connect timeout",
        "dns",
    )
    return (
        "timeout" in type_name
        or any(marker in type_name for marker in network_type_markers)
        or any(marker in message for marker in markers)
    )


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
        "americancenturyinvestments": ProviderPipeline(
            name="American Century Investments",
            downloader=download_american_century_investments_file,
            extractor=extract_american_century_investments_rows,
            input_dir=AMERICAN_CENTURY_INVESTMENTS_INPUT_DIR,
            output_dir=AMERICAN_CENTURY_INVESTMENTS_INPUT_DIR,
            output_filename="american_century_investments_selected_fields.csv",
            latest_download_finder=find_latest_american_century_investments_download,
            source_row_parser=parse_american_century_investments_source_rows,
        ),
        "columbia": ProviderPipeline(
            name="Columbia Threadneedle Investments",
            downloader=download_columbia_file,
            extractor=extract_columbia_rows,
            input_dir=COLUMBIA_INPUT_DIR,
            output_dir=COLUMBIA_INPUT_DIR,
            output_filename="columbia_selected_fields.csv",
            latest_download_finder=find_latest_columbia_download,
            source_row_parser=parse_columbia_source_rows,
        ),
        "bnpparibas": ProviderPipeline(
            name="BNP Paribas Asset Management",
            downloader=download_bnpparibas_file,
            extractor=extract_bnp_paribas_rows,
            input_dir=BNP_PARIBAS_INPUT_DIR,
            output_dir=BNP_PARIBAS_INPUT_DIR,
            output_filename="bnpparibas_selected_fields.csv",
            latest_download_finder=find_latest_bnp_paribas_download,
            source_row_parser=parse_bnp_paribas_source_rows,
        ),
        "goldmansachs": ProviderPipeline(
            name="Goldman Sachs Asset Management",
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
        "ark": ProviderPipeline(
            name="ARK Invest Europe",
            downloader=download_ark_file,
            extractor=extract_ark_rows,
            input_dir=ARK_INPUT_DIR,
            output_dir=ARK_INPUT_DIR,
            output_filename="ark_selected_fields.csv",
            latest_download_finder=find_latest_ark_download,
            source_row_parser=parse_ark_source_rows,
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
        "kraneshares": ProviderPipeline(
            name="KraneShares",
            downloader=download_kraneshares_file,
            extractor=extract_kraneshares_rows,
            input_dir=KRANESHARES_INPUT_DIR,
            output_dir=KRANESHARES_INPUT_DIR,
            output_filename="kraneshares_selected_fields.csv",
            latest_download_finder=find_latest_kraneshares_download,
            source_row_parser=parse_kraneshares_source_rows,
        ),
        "mandg": ProviderPipeline(
            name="M&G",
            downloader=download_mg_file,
            extractor=extract_mandg_rows,
            input_dir=MANDG_INPUT_DIR,
            output_dir=MANDG_INPUT_DIR,
            output_filename="mg_selected_fields.csv",
            latest_download_finder=find_latest_mandg_download,
            source_row_parser=parse_mandg_source_rows,
        ),
        "market_access": ProviderPipeline(
            name="Market Access",
            downloader=download_market_access_file,
            extractor=extract_market_access_rows,
            input_dir=MARKET_ACCESS_INPUT_DIR,
            output_dir=MARKET_ACCESS_INPUT_DIR,
            output_filename="market_access_selected_fields.csv",
            latest_download_finder=find_latest_market_access_download,
            source_row_parser=parse_market_access_source_rows,
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
        "ossiam": ProviderPipeline(
            name="Ossiam",
            downloader=download_ossiam_file,
            extractor=extract_ossiam_rows,
            input_dir=OSSIAM_INPUT_DIR,
            output_dir=OSSIAM_INPUT_DIR,
            output_filename="ossiam_selected_fields.csv",
            latest_download_finder=find_latest_ossiam_download,
            source_row_parser=lambda path: parse_ossiam_source_rows(path)[0],
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
        "expat": ProviderPipeline(
            name="Expat",
            downloader=download_expat_file,
            extractor=extract_expat_rows,
            input_dir=EXPAT_INPUT_DIR,
            output_dir=EXPAT_INPUT_DIR,
            output_filename="expat_selected_fields.csv",
            latest_download_finder=find_latest_expat_download,
            source_row_parser=parse_expat_source_rows,
        ),
        "waystone": ProviderPipeline(
            name="Waystone",
            downloader=download_waystone_file,
            extractor=extract_waystone_rows,
            input_dir=WAYSTONE_INPUT_DIR,
            output_dir=WAYSTONE_INPUT_DIR,
            output_filename="waystone_selected_fields.csv",
            latest_download_finder=find_latest_waystone_download,
            source_row_parser=parse_waystone_source_rows,
        ),
        "abrdn": ProviderPipeline(
            name="abrdn",
            downloader=download_abrdn_file,
            extractor=extract_abrdn_rows,
            input_dir=ABRDN_INPUT_DIR,
            output_dir=ABRDN_INPUT_DIR,
            output_filename="abrdn_selected_fields.csv",
            latest_download_finder=find_latest_abrdn_download,
            source_row_parser=parse_abrdn_source_rows,
        ),
        "alliancebernstein": ProviderPipeline(
            name="AllianceBernstein",
            downloader=download_alliance_bernstein_file,
            extractor=extract_alliance_bernstein_rows,
            input_dir=ALLIANCE_BERNSTEIN_INPUT_DIR,
            output_dir=ALLIANCE_BERNSTEIN_INPUT_DIR,
            output_filename="alliance_bernstein_selected_fields.csv",
            latest_download_finder=find_latest_alliance_bernstein_download,
            source_row_parser=parse_alliance_bernstein_source_rows,
        ),
        "alphaucits": ProviderPipeline(
            name="Alpha UCITS",
            downloader=download_alpha_ucits_file,
            extractor=extract_alpha_ucits_rows,
            input_dir=ALPHA_UCITS_INPUT_DIR,
            output_dir=ALPHA_UCITS_INPUT_DIR,
            output_filename="alpha_ucits_selected_fields.csv",
            latest_download_finder=find_latest_alpha_ucits_download,
            source_row_parser=parse_alpha_ucits_source_rows,
            apply_isin_whitelist_to_provider_output=True,
        ),
        "nordea": ProviderPipeline(
            name="Nordea",
            downloader=download_nordea_file,
            extractor=extract_nordea_rows,
            input_dir=NORDEA_INPUT_DIR,
            output_dir=NORDEA_INPUT_DIR,
            output_filename="nordea_selected_fields.csv",
            latest_download_finder=find_latest_nordea_download,
            source_row_parser=parse_nordea_source_rows,
            apply_isin_whitelist_to_provider_output=True,
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
    }


async def prepare_input_file(pipeline: ProviderPipeline, use_latest_downloads: bool) -> Path:
    if use_latest_downloads:
        return pipeline.latest_download_finder(pipeline.input_dir)

    fallback_input_path = try_find_latest_download(pipeline)
    try:
        return await pipeline.downloader()
    except Exception as exc:
        if fallback_input_path is not None and is_network_related_error(exc):
            print(
                f"[WARN] {pipeline.name} download failed due to a network error; "
                f"using latest existing snapshot instead: {fallback_input_path}"
            )
            return fallback_input_path
        raise


async def run_provider(
    pipeline: ProviderPipeline,
    use_latest_downloads: bool,
    run_date: str,
    whitelist_isins: set[str],
) -> tuple[Path, Path, int, list[dict[str, str]], dict[str, int]]:
    print()
    print(f"=== {pipeline.name} ===")
    input_path = await prepare_input_file(pipeline, use_latest_downloads)
    source_rows = pipeline.source_row_parser(input_path)
    source_row_count = len(source_rows)
    rows = dedupe_exact_rows(pipeline.extractor(input_path))
    rows = backfill_aum_currency(rows, source_rows)
    rows = normalize_date_column(rows)
    provider_filter_summary: IsinFilterSummary | None = None
    if pipeline.apply_isin_whitelist_to_provider_output:
        rows, provider_filter_summary = apply_final_isin_whitelist(rows, whitelist_isins)
        rows = dedupe_exact_rows(rows)
    missing_counts = validate_rows(pipeline.name, rows)
    output_path = build_provider_output_path(pipeline.output_dir, run_date, pipeline.output_filename)
    output_path = write_csv_with_fallback(output_path, rows)
    print(f"Input file   : {input_path}")
    print(f"Output file  : {output_path}")
    print(f"Source rows  : {source_row_count:,}")
    print(f"Rows extracted: {len(rows):,}")
    print(f"Excluded rows: {source_row_count - len(rows):,}")
    if provider_filter_summary is not None:
        print(
            "ISIN filter : "
            f"kept {provider_filter_summary.final_rows_after_filtering:,} of "
            f"{provider_filter_summary.final_rows_before_filtering:,} extracted row(s); "
            f"removed {provider_filter_summary.removed_rows_count:,} row(s)"
        )
    print(
        "Missing values: "
        + ", ".join(f"{column}={count}" for column, count in missing_counts.items())
    )
    return input_path, output_path, source_row_count, rows, missing_counts


async def async_main() -> int:
    args = parse_args()
    pipelines = build_pipelines(include_all_funds=not args.etf_only)
    run_date = datetime.now().strftime("%Y-%m-%d")
    run_dir = build_run_dir(run_date)
    run_folder_name = run_dir.name
    combined_output_path = run_dir / COMBINED_FILENAME
    whitelist_isins = load_allowed_isins(ISIN_FILTER_PATH)

    successes: list[tuple[str, Path, Path, int, int, dict[str, int]]] = []
    failures: list[tuple[str, Exception]] = []
    combined_rows: list[dict[str, str]] = []

    previous_run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    os.environ[RUN_FOLDER_ENV_VAR] = run_folder_name
    try:
        for provider_key in args.providers:
            pipeline = pipelines[provider_key]
            try:
                input_path, output_path, source_row_count, rows, missing_counts = await run_provider(
                    pipeline,
                    args.use_latest_downloads,
                    run_folder_name,
                    whitelist_isins,
                )
                combined_rows.extend(rows)
                successes.append((pipeline.name, input_path, output_path, source_row_count, len(rows), missing_counts))
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

    print()
    print("=== Summary ===")
    print(f"Run folder   : {run_dir}")
    print(f"Combined CSV : {combined_output_path}")
    print(f"Total rows   : {len(filtered_rows):,}")
    print(f"Whitelist ISINs: {filter_summary.whitelist_unique_isin_count:,} from {ISIN_FILTER_PATH}")
    print(f"Rows removed by ISIN filter: {filter_summary.removed_rows_count:,}")

    if successes:
        for provider_name, input_path, output_path, source_row_count, row_count, missing_counts in successes:
            print(f"{provider_name}:")
            print(f"  Source -> {input_path}")
            print(f"  Output -> {output_path}")
            print(f"  Source rows -> {source_row_count:,}")
            print(f"  Rows   -> {row_count:,}")
            print(f"  Excluded -> {source_row_count - row_count:,}")
            print("  Missing -> " + ", ".join(f"{column}={count}" for column, count in missing_counts.items()))

    if failures:
        for provider_name, exc in failures:
            print(f"{provider_name}: FAILED -> {exc}")
        return 1

    print("All selected providers completed successfully.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
