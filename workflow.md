# Project Workflow

This document explains the project at a high level, without going deep into code or provider-specific implementation details.

## Goal

The purpose of this project is to produce one clean ETF dataset from many different asset-manager websites.

Each provider publishes data in its own format, so the project:

1. collects data from the provider source
2. reshapes it into one common format
3. filters it against the project ISIN list
4. writes one final combined CSV

## The Big Picture

The workflow is:

```text
provider website
-> raw provider file
-> provider field extraction
-> shared output format
-> final whitelist filtering
-> combined CSV
```

In practical terms, the project always tries to keep the same idea:

- first save what was scraped from the source
- then create the cleaned version used by the pipeline
- then merge all providers together

## Step 1: Provider Data Collection

Each provider has its own scraper or downloader.

At this stage, the project connects to the provider website and gathers the latest available data from that source. Depending on the provider, this may come from:

- an Excel download
- a CSV download
- a JSON API
- an XML feed
- a rendered web page

The result of this step is one raw provider file saved inside that provider's dated folder.

Example:

```text
providers/jpmorgan/2026-07-08/jpmorgan_etf_export.json
```

## Step 2: Provider Extraction

After the raw provider file exists, the project runs a provider-specific extractor.

This extractor reads the raw file and maps the provider's fields into the project's shared ETF schema.

That means the extractor pulls out the fields the final pipeline needs, such as:

- ETF name
- issuer
- ISIN
- currency
- TER
- AUM
- AUM currency
- source date

The result of this step is the provider's selected-fields CSV, saved in the same dated folder as the raw file.

Example:

```text
providers/jpmorgan/2026-07-08/jpmorgan_selected_fields.csv
```

## Step 3: One Shared Structure

Even though providers all publish data differently, the project standardizes them into one shared structure.

This is what makes it possible to merge many providers into one final file.

At this stage, the project is no longer thinking in terms of the original provider layout. It is thinking in terms of one common schema that all providers must fit into.

## Step 4: Final ISIN Filtering

Once provider outputs are ready, the pipeline combines them and applies the whitelist from:

```text
ISIN-list.xlsx
```

This step makes sure the final file is focused on the required ISIN universe for the project.

In other words:

- provider files may contain more products than needed
- the final combined file keeps only the rows relevant to the project whitelist

## Step 5: Final Combined Output

After combining and filtering, the pipeline writes the final CSV for that run inside:

```text
pipeline_runs/<YYYY-MM-DD>/all_etf_fields.csv
```

This is the main final deliverable of the project.

If someone only cares about the final result, this is usually the file they want.

## Folder Philosophy

The project is organized around dated runs.

That means each run creates dated folders so you can clearly see:

- what raw file was used
- what extracted provider file was created
- what final combined file was produced

This makes the workflow easier to follow and easier to rerun.

## What Happens for Each Provider

At a surface level, every provider follows the same pattern:

1. get the latest data from the provider source
2. save the raw provider file
3. extract the selected project fields
4. save the extracted provider CSV
5. include that provider in the final combined run

The exact scraping mechanics may differ by provider, but the overall business workflow stays the same.

## Why the Project Is Split This Way

This structure helps with a few important things:

- it keeps raw source data separate from cleaned output
- it makes provider issues easier to isolate
- it lets you rerun one provider without redesigning the whole pipeline
- it keeps the final consolidation consistent across all providers

## If You Are New to the Project

The easiest way to think about it is:

- `scrapers/` gets the provider data
- `providers/` cleans and formats the provider data
- `run_all_etf_pipeline.py` orchestrates everything
- `pipeline_runs/` stores the final combined result

If you understand those four ideas, you understand the overall workflow of the repository.

## Related Documents

- `README.md`: setup and run instructions
- `docs/provider_workflows.md`: provider-by-provider source workflow notes
