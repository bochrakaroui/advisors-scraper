# ETF Extractor Workflow

This file documents the current workflow implemented in this project.

## Goal

Download ETF source files from official provider websites, then extract a clean dataset with these fields:

- ETF Name
- Issuer
- ISIN
- CCY
- TER(bps)
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
- `providers/UBS/`
  - UBS downloaded files
  - UBS processed files
  - UBS field extractor
- `providers/jpmorgan/`
  - J.P. Morgan downloaded files
  - J.P. Morgan processed files
  - J.P. Morgan field extractor
- `providers/wisdomtree/`
  - WisdomTree downloaded files
  - WisdomTree processed files
  - WisdomTree field extractor
- `providers/vanguard/`
  - Vanguard downloaded files
  - Vanguard processed files
  - Vanguard field extractor
- `providers/firsttrust/`
  - First Trust downloaded files
  - First Trust processed files
  - First Trust field extractor
- `docs/provider_workflows.md`
  - provider-by-provider source and fetch-method notes
- `pipeline_runs/`
  - one folder per pipeline run, named like `2026-06-21_13-04-45`
  - contains the final combined CSV for that run

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
python run_all_etf_pipeline.py --providers ubs
python run_all_etf_pipeline.py --providers jpmorgan
python run_all_etf_pipeline.py --providers wisdomtree
python run_all_etf_pipeline.py --providers vanguard
python run_all_etf_pipeline.py --providers firsttrust
```

Pipeline behavior:

- deletes the provider `*_processed` folders before the run
- creates a new folder under `pipeline_runs\` named like `2026-06-21_13-04-45`
- writes one combined CSV there containing appended rows from all selected providers after the final ISIN whitelist filter

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

### UBS

Download the latest UBS file:

```powershell
python scrapers\UBS_extractor.py
```

Saved to:

```text
providers\UBS\UBS_etf_downloads\
```

### J.P. Morgan

Download the latest J.P. Morgan file:

```powershell
python scrapers\jpmorgan_extractor.py
```

Saved to:

```text
providers\jpmorgan\jpmorgan_downloads\
```

### WisdomTree

Download the latest WisdomTree file:

```powershell
python scrapers\wisdomtree_extractor.py
```

Saved to:

```text
providers\wisdomtree\wisdomtree_downloads\
```

### Vanguard

Download the latest Vanguard file:

```powershell
python providers\vanguard\download_vanguard.py
```

Saved to:

```text
providers\vanguard\vanguard_downloads\
```

### First Trust

Download the latest First Trust file:

```powershell
python scrapers\firsttrust_extractor.py
```

Saved to:

```text
providers\firsttrust\firsttrust_downloads\
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
- Reads source `TER / OCF`
- Writes `TER(bps)` in basis points
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
- Reads source `TER p.a. (%)`
- Writes `TER(bps)` in basis points
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
- Reads source `OGC`
- Writes `TER(bps)` in basis points
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
- Reads source `terocf`
- Writes `TER(bps)` in basis points
- Scales source `aum` into millions and writes it as `AUM(M)`
- Adds `Date` from the downloaded filename timestamp

### UBS Processing

Run:

```powershell
python providers\UBS\extract_ubs_fields.py
```

Output folder:

```text
providers\UBS\UBS_processed\
```

Current UBS processing rules:

- Reads the latest downloaded `.xlsx` file by default
- Parses the workbook directly
- Keeps all real source rows
- Keeps the source `Currency` as `CCY`
- Reads source `TER (flat fee)(%)`
- Writes `TER(bps)` in basis points
- Preserves source `AUM(M)` as `AUM(M)`
- Adds `Date` from the downloaded filename timestamp

### J.P. Morgan Processing

Run:

```powershell
python providers\jpmorgan\extract_jpmorgan_fields.py
```

Output folder:

```text
providers\jpmorgan\jpmorgan_processed\
```

Current J.P. Morgan processing rules:

- Reads the latest downloaded `.json` snapshot by default
- Filters ETF rows only from the official listing payload
- Keeps the share-class currency as `CCY`
- Converts `ongoingCharge` into `TER(bps)`
- Converts `assetsUnderManagement` into `AUM(M)`
- Adds `Date` from the downloaded filename timestamp

### WisdomTree Processing

Run:

```powershell
python providers\wisdomtree\extract_wisdomtree_fields.py
```

Output folder:

```text
providers\wisdomtree\wisdomtree_processed\
```

Current WisdomTree processing rules:

- Reads the latest downloaded `.json` snapshot by default
- Uses the official `WisdomTree Product List` PDF linked from the UK products page as the source behind that snapshot
- Keeps only rows whose official product names contain `UCITS ETF`
- Uses the official base currency as `CCY`
- Converts source TER percentages into `TER(bps)`
- Leaves `AUM(M)` blank when the official downloaded source does not expose fund-level AUM
- Adds `Date` from the downloaded filename timestamp

### Vanguard Processing

Run:

```powershell
python providers\vanguard\extract_vanguard_fields.py
```

Output folder:

```text
providers\vanguard\vanguard_processed\
```

Current Vanguard processing rules:

- Reads the latest downloaded `.json` snapshot by default
- Uses the official Vanguard UK ETF overview table as the main source
- Keeps the official listing currency as `CCY`
- Converts overview-table fee percentages into `TER(bps)`
- Normalizes overview-table fund size into `AUM(M)`
- Uses the official Vanguard GraphQL payload for `ISIN`
- Adds `Date` from the downloaded filename timestamp

### First Trust Processing

Run:

```powershell
python providers\firsttrust\extract_firsttrust_fields.py
```

Output folder:

```text
providers\firsttrust\firsttrust_processed\
```

Current First Trust processing rules:

- Reads the latest downloaded `.json` snapshot by default
- Uses the official embedded products JSON for ETF/shareclass rows
- Keeps the official shareclass currency as `CCY`
- Converts official `Total Expense Ratio` values into `TER(bps)`
- Normalizes official `Total Fund AUM` values into `AUM(M)`
- Adds `Date` from the downloaded filename timestamp

## Output Format

The output CSV columns are:

```text
ETF Name,Issuer,ISIN,CCY,TER(bps),AUM(M),Date
```

## Verification Commands

Useful checks:

```powershell
python -m py_compile scrapers\Amundi_extractor.py scrapers\UBS_extractor.py scrapers\ishares_extractor.py scrapers\Xtrackers_extractor.py scrapers\invesco_extractor.py scrapers\jpmorgan_extractor.py scrapers\wisdomtree_extractor.py providers\ishares\extract_ishares_fields.py providers\xtrackers\extract_xtrackers_fields.py providers\amundi\extract_amundi_fields.py providers\invesco\extract_invesco_fields.py providers\UBS\extract_ubs_fields.py providers\jpmorgan\extract_jpmorgan_fields.py providers\wisdomtree\extract_wisdomtree_fields.py providers\vanguard\download_vanguard.py providers\vanguard\extract_vanguard_fields.py run_all_etf_pipeline.py
python -m py_compile scrapers\Amundi_extractor.py scrapers\UBS_extractor.py scrapers\ishares_extractor.py scrapers\Xtrackers_extractor.py scrapers\invesco_extractor.py scrapers\jpmorgan_extractor.py scrapers\wisdomtree_extractor.py scrapers\firsttrust_extractor.py providers\ishares\extract_ishares_fields.py providers\xtrackers\extract_xtrackers_fields.py providers\amundi\extract_amundi_fields.py providers\invesco\extract_invesco_fields.py providers\UBS\extract_ubs_fields.py providers\jpmorgan\extract_jpmorgan_fields.py providers\wisdomtree\extract_wisdomtree_fields.py providers\vanguard\download_vanguard.py providers\vanguard\extract_vanguard_fields.py providers\firsttrust\extract_firsttrust_fields.py run_all_etf_pipeline.py
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

Run UBS processing:

```powershell
python providers\UBS\extract_ubs_fields.py
```

Run J.P. Morgan processing:

```powershell
python providers\jpmorgan\extract_jpmorgan_fields.py
```

Run WisdomTree processing:

```powershell
python providers\wisdomtree\extract_wisdomtree_fields.py
```

## Current Provider Status

- `iShares`: downloader and processor are working
- `Xtrackers`: downloader and processor are working
- `Amundi`: downloader and processor are working
- `Invesco`: downloader and processor are working
- `UBS`: downloader and processor are working
- `SPDR`: downloader and processor are working
- `HSBC`: downloader and processor are working
- `J.P. Morgan`: downloader and processor are working
- `WisdomTree`: downloader and processor are working

## Important Notes

- Folder structure was intentionally not changed
- Processing now writes a narrow seven-column dataset for every provider
- The pipeline writes one combined CSV per run inside `pipeline_runs\<timestamp>\`
- No provider performs FX conversion during processing
