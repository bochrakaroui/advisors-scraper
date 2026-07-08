# ETF Extractor

This repository downloads ETF data from many provider websites, normalizes each provider into one shared schema, applies the `ISIN-list.xlsx` whitelist, and writes one final combined CSV.

## What You Get

Each pipeline run produces:

- Provider raw data files inside `providers/<provider>/<YYYY-MM-DD>/`
- Provider extracted-field files inside the same dated folder
- One final combined file at `pipeline_runs/<YYYY-MM-DD>/all_etf_fields.csv`

The final CSV columns are:

```text
ETF Name,Issuer,ISIN,CCY,TER(bps),AUM(M),AUM CCY,Date
```

Field meanings:

- `TER(bps)`: total expense ratio in basis points
- `AUM(M)`: assets under management, always expressed in millions
- `AUM CCY`: currency of the AUM value
- `Date`: source date, normalized as `dd/mm/yyyy`

## Repository Layout

```text
etf-extractor/
  scrapers/                     provider downloaders / collectors
  providers/                    provider extractors + dated provider outputs
  pipeline_runs/                final combined output by run date
  docs/                         provider notes
  src/                          shared helper utilities
  tests/                        focused tests
  ISIN-list.xlsx                whitelist used in the final filter
  run_all_etf_pipeline.py       main entry point
  requirements.txt              Python dependencies
```

## Before You Start

You need:

- Python 3.11 or newer
- Internet access
- `ISIN-list.xlsx` present in the project root
- Playwright Chromium installed locally

If you are on Windows PowerShell and activation is blocked, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## Setup on a New Machine

### Windows PowerShell

```powershell
git clone <your-repo-url>
cd etf-extractor
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### Linux / macOS

```bash
git clone <your-repo-url>
cd etf-extractor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## First Run Checklist

Before your first run, confirm all of these are true:

1. You are in the project root.
2. Your virtual environment is activated.
3. `requirements.txt` is installed.
4. `python -m playwright install chromium` has been run.
5. `ISIN-list.xlsx` exists in the repository root.

## Main Commands

### 1. Full live run

This downloads fresh provider data where the provider scraper supports it, then builds the final CSV.

```powershell
python run_all_etf_pipeline.py
```

### 2. Rebuild from already-saved provider files

Use this when you do not want to hit provider websites again.

```powershell
python run_all_etf_pipeline.py --use-latest-downloads
```

### 3. Run only some providers

Example:

```powershell
python run_all_etf_pipeline.py --providers ishares xtrackers amundi
```

### 4. Stop as soon as one provider fails

```powershell
python run_all_etf_pipeline.py --stop-on-error
```

### 5. Pass the iShares ETF-only option

```powershell
python run_all_etf_pipeline.py --etf-only
```

## What Happens During a Run

For each selected provider, the pipeline does this:

1. Download or reuse the latest provider raw file.
2. Parse that raw file.
3. Run the matching provider extractor.
4. Save the extracted CSV in the provider's dated folder.
5. Apply the final ISIN whitelist filter when building the combined file.

The console output shows, for each provider:

- source information
- raw row count
- extracted row count
- missing-field counts
- whitelist match counts
- raw and selected output paths
- final provider status

At the end, the script prints the final combined CSV path and the total row counts.

## Where Files Are Written

### Provider-level output

Each provider writes into its own dated folder:

```text
providers/<provider>/<YYYY-MM-DD>/
```

Typical example:

```text
providers/jpmorgan/2026-07-08/jpmorgan_etf_export.json
providers/jpmorgan/2026-07-08/jpmorgan_selected_fields.csv
```

### Final combined output

```text
pipeline_runs/<YYYY-MM-DD>/all_etf_fields.csv
```

## Running One Provider Manually

If you want to test one provider outside the full pipeline, run:

1. The provider scraper in `scrapers/`
2. The matching extractor in `providers/<provider>/`

Example for J.P. Morgan:

```powershell
python scrapers\jpmorgan_extractor.py
python providers\jpmorgan\extract_jpmorgan_fields.py
```

Example for WisdomTree:

```powershell
python scrapers\wisdomtree_extractor.py
python providers\wisdomtree\extract_wisdomtree_fields.py
```

## Supported Provider Keys

Use these values with `--providers`:

```text
ishares
xtrackers
amundi
fidelity
invesco
ubs
spdr
hsbc
jpmorgan
landg
palmersquare
vaneck
franklintempleton
wisdomtree
vanguard
firsttrust
hanetf
globalx
finex
imgp
abrdn
alliancebernstein
alphaucits
americancenturyinvestments
ark
bnpparibas
columbia
connectetfs
dimensional
goldmansachs
janushenderson
kraneshares
mg
marketaccess
nordea
ossiam
paceretfs
pimco
robeco
schroders
waystone
```

## Typical New-Machine Workflow

For most users, this is the simplest end-to-end flow:

```powershell
git clone <your-repo-url>
cd etf-extractor
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python run_all_etf_pipeline.py
```

Then open:

```text
pipeline_runs/<today's date>/all_etf_fields.csv
```

## Common Problems

### `ModuleNotFoundError`

Cause:
- The virtual environment is not active, or dependencies were not installed.

Fix:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Playwright browser errors

Cause:
- Chromium is not installed for Playwright.

Fix:

```powershell
python -m playwright install chromium
```

### The pipeline cannot find `ISIN-list.xlsx`

Cause:
- The whitelist file is missing from the project root.

Fix:
- Put `ISIN-list.xlsx` back in the repository root.

### Permission denied when writing CSV or XLS files

Cause:
- A generated file is open in Excel or another program.

Fix:
- Close the file and rerun the command.

### Some providers fail or return partial data

Cause:
- Provider websites may be down, changed, rate-limited, or temporarily blocking automation.

Fix:
- Rerun the provider or rerun the full pipeline later.
- If you already have saved provider raw files, try:

```powershell
python run_all_etf_pipeline.py --use-latest-downloads
```

## Notes About Source Metadata

The project stores source freshness metadata for some providers, including iShares, under:

```text
.source_metadata/
```

These files are used by the pipeline and should not be deleted if you want the freshness reporting to remain accurate.

## Useful Files

- [run_all_etf_pipeline.py](/abs/path/C:/Users/Bochra/OneDrive/Desktop/etf-extractor/run_all_etf_pipeline.py:1): main pipeline script
- [providers/output_schema.py](/abs/path/C:/Users/Bochra/OneDrive/Desktop/etf-extractor/providers/output_schema.py:1): shared output schema
- [docs/workflow.md](/abs/path/C:/Users/Bochra/OneDrive/Desktop/etf-extractor/docs/workflow.md:1): high-level project workflow
- [docs/provider_workflows.md](/abs/path/C:/Users/Bochra/OneDrive/Desktop/etf-extractor/docs/provider_workflows.md:1): provider workflow notes

## Scheduled Runs

If this runs on a server or scheduler:

- run from the project root
- keep the virtual environment available
- ensure internet access is allowed
- keep `ISIN-list.xlsx` in place
- do not keep output files open
- install Playwright Chromium on that machine first

Example Linux server setup:

```bash
git clone <your-repo-url>
cd etf-extractor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python run_all_etf_pipeline.py
```
