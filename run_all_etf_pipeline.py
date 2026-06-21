"""Run all ETF downloaders and build one combined CSV for the run."""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from providers.amundi.extract_amundi_fields import (
    INPUT_DIR as AMUNDI_INPUT_DIR,
    OUTPUT_COLUMNS,
    extract_rows as extract_amundi_rows,
    find_latest_download as find_latest_amundi_download,
    parse_xlsx_rows as parse_amundi_source_rows,
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
from providers.xtrackers.extract_xtrackers_fields import (
    INPUT_DIR as XTRACKERS_INPUT_DIR,
    extract_rows as extract_xtrackers_rows,
    find_latest_download as find_latest_xtrackers_download,
    parse_xlsx_rows as parse_xtrackers_source_rows,
)
from scrapers.Amundi_extractor import download_amundi_file
from scrapers.UBS_extractor import download_ubs_file
from scrapers.Xtrackers_extractor import download_xtrackers_file
from scrapers.invesco_extractor import download_invesco_file
from scrapers.ishares_extractor import download_etf_list
from scrapers.spdr_collector import download_spdr_file, parse_xlsx_rows as parse_spdr_source_rows


BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "pipeline_runs"
COMBINED_FILENAME = "all_etf_fields.csv"

ALL_PROVIDERS = ("ishares", "xtrackers", "amundi", "invesco", "ubs", "spdr")
PROCESSED_DIRS = (
    BASE_DIR / "providers" / "ishares" / "ishares_processed",
    BASE_DIR / "providers" / "xtrackers" / "xtrackers_processed",
    BASE_DIR / "providers" / "amundi" / "amundi_processed",
    BASE_DIR / "providers" / "invesco" / "invesco_processed",
    BASE_DIR / "providers" / "UBS" / "UBS_processed",
    BASE_DIR / "providers" / "SPDR" / "spdr_processed",
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
    latest_download_finder: LatestDownloadFinder
    source_row_parser: SourceRowParser


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


def build_run_dir() -> Path:
    run_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = RUNS_DIR / run_timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def remove_readonly_and_retry(function, path: str, excinfo) -> None:
    os.chmod(path, 0o666)
    function(path)


def clean_processed_dirs() -> None:
    for processed_dir in PROCESSED_DIRS:
        if processed_dir.exists():
            shutil.rmtree(processed_dir, onexc=remove_readonly_and_retry)


def write_combined_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_validation_report(
    output_path: Path,
    successes: list[tuple[str, Path, int, int, dict[str, int]]],
    failures: list[tuple[str, Exception]],
    total_rows: int,
) -> None:
    lines = [
        "ETF Pipeline Validation Report",
        f"Generated: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        f"Total rows: {total_rows:,}",
        "",
    ]

    if successes:
        lines.append("Successful providers:")
        for provider_name, input_path, source_row_count, row_count, missing_counts in successes:
            lines.append(f"- {provider_name}")
            lines.append(f"  Source: {input_path}")
            lines.append(f"  Source rows parsed: {source_row_count:,}")
            lines.append(f"  Rows: {row_count:,}")
            lines.append(f"  Excluded rows: {source_row_count - row_count:,}")
            lines.append("  Missing: " + ", ".join(f"{column}={count}" for column, count in missing_counts.items()))
        lines.append("")

    if failures:
        lines.append("Failed providers:")
        for provider_name, exc in failures:
            lines.append(f"- {provider_name}: {exc}")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


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


def build_pipelines(include_all_funds: bool) -> dict[str, ProviderPipeline]:
    return {
        "ishares": ProviderPipeline(
            name="iShares",
            downloader=download_etf_list,
            extractor=lambda input_path: extract_ishares_rows(input_path, include_all_funds=include_all_funds),
            input_dir=ISHARES_INPUT_DIR,
            latest_download_finder=find_latest_ishares_download,
            source_row_parser=parse_ishares_source_rows,
        ),
        "xtrackers": ProviderPipeline(
            name="Xtrackers",
            downloader=download_xtrackers_file,
            extractor=extract_xtrackers_rows,
            input_dir=XTRACKERS_INPUT_DIR,
            latest_download_finder=find_latest_xtrackers_download,
            source_row_parser=parse_xtrackers_source_rows,
        ),
        "amundi": ProviderPipeline(
            name="Amundi",
            downloader=download_amundi_file,
            extractor=extract_amundi_rows,
            input_dir=AMUNDI_INPUT_DIR,
            latest_download_finder=find_latest_amundi_download,
            source_row_parser=parse_amundi_source_rows,
        ),
        "spdr": ProviderPipeline(
            name="SPDR",
            downloader=download_spdr_file,
            extractor=extract_spdr_rows,
            input_dir=SPDR_INPUT_DIR,
            latest_download_finder=find_latest_spdr_download,
            source_row_parser=parse_spdr_source_rows,
        ),
        "ubs": ProviderPipeline(
            name="UBS",
            downloader=download_ubs_file,
            extractor=extract_ubs_rows,
            input_dir=UBS_INPUT_DIR,
            latest_download_finder=find_latest_ubs_download,
            source_row_parser=parse_ubs_source_rows,
        ),
        "invesco": ProviderPipeline(
            name="Invesco",
            downloader=download_invesco_file,
            extractor=extract_invesco_rows,
            input_dir=INVESCO_INPUT_DIR,
            latest_download_finder=find_latest_invesco_download,
            source_row_parser=parse_invesco_source_rows,
        ),
    }


async def prepare_input_file(pipeline: ProviderPipeline, use_latest_downloads: bool) -> Path:
    if use_latest_downloads:
        return pipeline.latest_download_finder(pipeline.input_dir)
    return await pipeline.downloader()


async def run_provider(
    pipeline: ProviderPipeline,
    use_latest_downloads: bool,
) -> tuple[Path, int, list[dict[str, str]], dict[str, int]]:
    print()
    print(f"=== {pipeline.name} ===")
    input_path = await prepare_input_file(pipeline, use_latest_downloads)
    source_row_count = len(pipeline.source_row_parser(input_path))
    rows = pipeline.extractor(input_path)
    missing_counts = validate_rows(pipeline.name, rows)
    print(f"Input file   : {input_path}")
    print(f"Source rows  : {source_row_count:,}")
    print(f"Rows extracted: {len(rows):,}")
    print(f"Excluded rows: {source_row_count - len(rows):,}")
    print(
        "Missing values: "
        + ", ".join(f"{column}={count}" for column, count in missing_counts.items())
    )
    return input_path, source_row_count, rows, missing_counts


async def async_main() -> int:
    args = parse_args()
    pipelines = build_pipelines(include_all_funds=not args.etf_only)
    run_dir = build_run_dir()
    combined_output_path = run_dir / COMBINED_FILENAME
    validation_report_path = run_dir / "validation_report.txt"

    clean_processed_dirs()

    successes: list[tuple[str, Path, int, int, dict[str, int]]] = []
    failures: list[tuple[str, Exception]] = []
    combined_rows: list[dict[str, str]] = []

    for provider_key in args.providers:
        pipeline = pipelines[provider_key]
        try:
            input_path, source_row_count, rows, missing_counts = await run_provider(pipeline, args.use_latest_downloads)
            combined_rows.extend(rows)
            successes.append((pipeline.name, input_path, source_row_count, len(rows), missing_counts))
        except Exception as exc:
            failures.append((pipeline.name, exc))
            print(f"[ERROR] {pipeline.name} failed: {exc}")
            if args.stop_on_error:
                break

    write_combined_csv(combined_output_path, combined_rows)
    write_validation_report(validation_report_path, successes, failures, len(combined_rows))

    print()
    print("=== Summary ===")
    print(f"Run folder   : {run_dir}")
    print(f"Combined CSV : {combined_output_path}")
    print(f"Validation   : {validation_report_path}")
    print(f"Total rows   : {len(combined_rows):,}")

    if successes:
        for provider_name, input_path, source_row_count, row_count, missing_counts in successes:
            print(f"{provider_name}:")
            print(f"  Source -> {input_path}")
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
