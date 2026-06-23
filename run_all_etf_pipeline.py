"""Run all ETF downloaders and build one combined CSV for the run."""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
import xml.etree.ElementTree as ET

from providers.amundi.extract_amundi_fields import (
    INPUT_DIR as AMUNDI_INPUT_DIR,
    OUTPUT_COLUMNS,
    extract_rows as extract_amundi_rows,
    find_latest_download as find_latest_amundi_download,
    parse_xlsx_rows as parse_amundi_source_rows,
)
from providers.firsttrust.extract_firsttrust_fields import (
    INPUT_DIR as FIRSTTRUST_INPUT_DIR,
    extract_rows as extract_firsttrust_rows,
    find_latest_download as find_latest_firsttrust_download,
    parse_snapshot_rows as parse_firsttrust_source_rows,
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
from providers.vanguard.download_vanguard import download_vanguard_file
from scrapers.Amundi_extractor import download_amundi_file
from scrapers.firsttrust_extractor import download_firsttrust_file
from scrapers.UBS_extractor import download_ubs_file
from scrapers.Xtrackers_extractor import download_xtrackers_file
from scrapers.hsbc_extractor import download_hsbc_file
from scrapers.invesco_extractor import download_invesco_file
from scrapers.ishares_extractor import download_etf_list
from scrapers.jpmorgan_extractor import download_jpmorgan_file
from scrapers.spdr_collector import download_spdr_file, parse_xlsx_rows as parse_spdr_source_rows
from scrapers.wisdomtree_extractor import download_wisdomtree_file


BASE_DIR = Path(__file__).resolve().parent
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

ALL_PROVIDERS = (
    "ishares",
    "xtrackers",
    "amundi",
    "invesco",
    "ubs",
    "spdr",
    "hsbc",
    "jpmorgan",
    "wisdomtree",
    "vanguard",
    "firsttrust",
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
    suffix = 1
    while candidate.exists():
        candidate = base_dir / f"{run_date} ({suffix})"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
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


def validate_whitelisted_rows(rows: list[dict[str, str]], isin_column_name: str, whitelist_isins: set[str]) -> list[str]:
    final_isins = collect_normalized_isins(rows, isin_column_name)
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
    }


async def prepare_input_file(pipeline: ProviderPipeline, use_latest_downloads: bool) -> Path:
    if use_latest_downloads:
        return pipeline.latest_download_finder(pipeline.input_dir)
    return await pipeline.downloader()


async def run_provider(
    pipeline: ProviderPipeline,
    use_latest_downloads: bool,
    run_date: str,
) -> tuple[Path, Path, int, list[dict[str, str]], dict[str, int]]:
    print()
    print(f"=== {pipeline.name} ===")
    input_path = await prepare_input_file(pipeline, use_latest_downloads)
    source_row_count = len(pipeline.source_row_parser(input_path))
    rows = pipeline.extractor(input_path)
    missing_counts = validate_rows(pipeline.name, rows)
    output_path = build_provider_output_path(pipeline.output_dir, run_date, pipeline.output_filename)
    write_combined_csv(output_path, rows)
    print(f"Input file   : {input_path}")
    print(f"Output file  : {output_path}")
    print(f"Source rows  : {source_row_count:,}")
    print(f"Rows extracted: {len(rows):,}")
    print(f"Excluded rows: {source_row_count - len(rows):,}")
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

    write_combined_csv(combined_output_path, filtered_rows)

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
