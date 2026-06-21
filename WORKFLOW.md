# ETF Extractor Workflow

This file documents the current workflow implemented in this project.

## Goal

Download ETF source files from official provider websites, then extract a clean dataset with these fields:

- ETF Name
- Issuer
- CCY
- TER
- AUM(M)
- Date

`Date` is taken from the downloaded filename timestamp and written in a readable format such as `17/06/2026 13:54:57`.

## Project Structure

The current structure is kept as-is:

- `scrapers/`
  - website download scripts
- `providers/ishares/`
  - iShares downloaded files
  - iShares processed files
  - iShares field extractor
- `providers/xtrackers/`
  - Xtrackers downloaded files
  - Xtrackers processed files
  - Xtrackers field extractor
- `providers/amundi/`
  - Amundi downloaded files
  - Amundi processed files
  - Amundi field extractor
- `providers/invesco/`
  - Invesco downloaded files
  - Invesco processed files
  - Invesco field extractor
- `pipeline_runs/`
  - one folder per pipeline run, named like `pipeline_run_2026-06-21_11-50-42`
  - contains the combined CSV and validation report for that run

## Requirements

Install Python dependencies:

```powershell
pip install playwright
python -m playwright install chromium
```

## One-Command Run

Run every provider downloader and processor in sequence:

```powershell
python run_all_etf_pipeline.py
```

Optional:

```powershell
python run_all_etf_pipeline.py --providers ishares xtrackers
python run_all_etf_pipeline.py --etf-only
python run_all_etf_pipeline.py --stop-on-error
python run_all_etf_pipeline.py --use-latest-downloads
```

Pipeline behavior:

- deletes the provider `*_processed` folders before the run
- creates a new folder under `pipeline_runs\` named like `20260617_135457`
- writes one combined CSV there containing appended rows from all selected providers
- writes a `validation_report.txt` there with row counts and missing-value checks per provider

## Download Workflows

### iShares

Download the latest iShares file:

```powershell
python scrapers\ishares_extractor.py
```

Saved to:

```text
providers\ishares\ishares_downloads\
```

### Xtrackers

Download the latest Xtrackers file:

```powershell
python scrapers\Xtrackers_extractor.py
```

Saved to:

```text
providers\xtrackers\xtrackers_downloads\
```

### Amundi

Download the latest Amundi file:

```powershell
python scrapers\Amundi_extractor.py
```

Saved to:

```text
providers\amundi\amundi_downloads\
```

### Invesco

Download the latest Invesco file:

```powershell
python scrapers\invesco_extractor.py
```

Saved to:

```text
providers\invesco\invesco_downloads\
```

## Processing Workflows

### iShares Processing

Run:

```powershell
python providers\ishares\extract_ishares_fields.py
```

Optional:

```powershell
python providers\ishares\extract_ishares_fields.py --etf-only
```

Output folder:

```text
providers\ishares\ishares_processed\
```

Current iShares processing rules:

- Reads the latest downloaded `.xls` file by default
- Parses the XML spreadsheet directly
- Normalizes header whitespace so headers still work even when the source file contains line breaks
- Keeps all real source rows by default
- Can optionally keep ETF rows only with `--etf-only`
- Extracts source `TER / OCF` into `TER`
- Preserves source `AUM (M)` as `AUM(M)`
- Adds `Date` from the downloaded filename timestamp
- Writes the final cleaned CSV with the required output fields

### Xtrackers Processing

Run:

```powershell
python providers\xtrackers\extract_xtrackers_fields.py
```

Optional ticker enrichment:

```powershell
python providers\xtrackers\extract_xtrackers_fields.py --enrich-tickers
```

Output folder:

```text
providers\xtrackers\xtrackers_processed\
```

Current Xtrackers processing rules:

- Reads the latest downloaded `.xlsx` file by default
- Parses the workbook directly
- Keeps all real source rows and excludes non-data footer/disclaimer rows
- Keeps the source `Share class currency` as `CCY`
- Extracts source `TER p.a. (%)` into `TER`
- Scales source `AuM (GBP)` into millions only
- Does not perform FX conversion
- Adds `Date` from the downloaded filename timestamp

### Amundi Processing

Run:

```powershell
python providers\amundi\extract_amundi_fields.py
```

Output folder:

```text
providers\amundi\amundi_processed\
```

Current Amundi processing rules:

- Reads the latest downloaded `.xlsx` file by default
- Parses the workbook directly
- Keeps all real source rows
- Keeps the source `Share Class Currency` as `CCY`
- Extracts source `OGC` into `TER`
- Extracts the numeric part of `Assets Under Management` and writes it as `AUM(M)`
- Adds `Date` from the downloaded filename timestamp

### Invesco Processing

Run:

```powershell
python providers\invesco\extract_invesco_fields.py
```

Output folder:

```text
providers\invesco\invesco_processed\
```

Current Invesco processing rules:

- Reads the latest downloaded `.xlsx` file by default
- Parses the workbook directly
- Keeps all real source rows
- Keeps the source `currency` as `CCY`
- Extracts source `terocf` into `TER`
- Scales source `aum` into millions and writes it as `AUM(M)`
- Adds `Date` from the downloaded filename timestamp

## Output Format

The output CSV columns are:

```text
ETF Name,Issuer,CCY,TER,AUM(M),Date
```

## Verification Commands

Useful checks:

```powershell
python -m py_compile scrapers\Amundi_extractor.py scrapers\ishares_extractor.py scrapers\Xtrackers_extractor.py scrapers\invesco_extractor.py providers\ishares\extract_ishares_fields.py providers\xtrackers\extract_xtrackers_fields.py providers\amundi\extract_amundi_fields.py providers\invesco\extract_invesco_fields.py run_all_etf_pipeline.py
```

Run iShares processing:

```powershell
python providers\ishares\extract_ishares_fields.py
```

Run Xtrackers processing:

```powershell
python providers\xtrackers\extract_xtrackers_fields.py
```

Run Amundi processing:

```powershell
python providers\amundi\extract_amundi_fields.py
```

Run Invesco processing:

```powershell
python providers\invesco\extract_invesco_fields.py
```

## Current Provider Status

- `iShares`: downloader and processor are working
- `Xtrackers`: downloader and processor are working
- `Amundi`: downloader and processor are working
- `Invesco`: downloader and processor are working

## Important Notes

- Folder structure was intentionally not changed
- Processing now writes a narrow six-column dataset for every provider
- The pipeline writes one combined CSV per run inside `pipeline_runs\<timestamp>\`
- No provider performs FX conversion during processing
