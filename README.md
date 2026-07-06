# ETF Extractor

This project downloads ETF data from multiple asset-manager websites, normalizes each provider into one shared schema, applies an ISIN whitelist filter, and writes one combined CSV for the run.

## What This Produces

The combined output columns are:

```text
ETF Name,Issuer,ISIN,CCY,TER(bps),AUM(M),AUM CCY,Date
```

Column notes:

- `TER(bps)`: total expense ratio in basis points.
- `AUM(M)`: assets under management expressed in millions.
- `AUM CCY`: currency of the AUM value.
- `Date`: normalized as `dd/mm/yyyy`.

## Project Structure

```text
etf-extractor/
  scrapers/                     # Provider downloaders / collectors
  providers/                    # Provider extractors and dated provider output folders
  pipeline_runs/                # Combined pipeline output by run date
  docs/                         # Provider workflow notes and source notes
  ISIN-list.xlsx                # Required whitelist file for final filtering
  run_all_etf_pipeline.py       # Main pipeline entry point
  requirements.txt              # Python dependencies
```

Most providers follow this pattern:

1. A script in `scrapers/` downloads or scrapes the raw provider data.
2. A matching script in `providers/<provider>/` transforms that raw file into the shared schema.
3. `run_all_etf_pipeline.py` orchestrates both steps and then builds the final filtered CSV.

## Supported Providers

Current provider keys supported by the main pipeline:

```text
ishares, xtrackers, amundi, fidelity, invesco, ubs, spdr, hsbc, jpmorgan,
landg, palmersquare, vaneck, franklintempleton, wisdomtree, vanguard,
firsttrust, hanetf, globalx, finex, imgp, abrdn, alliancebernstein,
alphaucits, americancenturyinvestments, ark, bnpparibas, columbia,
connectetfs, dimensional, goldmansachs, janushenderson, kraneshares, mg,
marketaccess, nordea, ossiam, paceretfs, pimco, robeco, schroders, waystone
```

## Requirements

You need:

- Python 3.11 or newer
- Internet access to the provider websites
- `ISIN-list.xlsx` in the project root
- Playwright Chromium installed locally

If you are on Windows PowerShell and script activation is blocked, you may need:

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

Before running the pipeline, confirm that `ISIN-list.xlsx` exists in the repository root.

## Main Commands

Run the full pipeline with fresh downloads:

```powershell
python run_all_etf_pipeline.py
```

Reuse the latest already-saved provider files instead of downloading again:

```powershell
python run_all_etf_pipeline.py --use-latest-downloads
```

Run only selected providers:

```powershell
python run_all_etf_pipeline.py --providers ishares xtrackers amundi
```

Stop immediately if one provider fails:

```powershell
python run_all_etf_pipeline.py --stop-on-error
```

Pass the iShares-specific ETF-only option through the pipeline:

```powershell
python run_all_etf_pipeline.py --etf-only
```

## Running One Provider Manually

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

In general:

1. Run the provider scraper from `scrapers/`.
2. Run the matching extractor from `providers/<provider>/`.

## Output Locations

Each run creates provider-level files and one combined pipeline output.

Provider output:

```text
providers\<provider>\YYYY-MM-DD\
```

Typical contents:

```text
providers\jpmorgan\2026-07-06\jpmorgan_etf_export.json
providers\jpmorgan\2026-07-06\jpmorgan_selected_fields.csv
```

Final pipeline output:

```text
pipeline_runs\YYYY-MM-DD\all_etf_fields.csv
```

Notes:

- Provider raw files and selected-field CSVs are stored together in the provider date folder.
- Some providers may create suffixed date folders like `2026-07-06 (1)` if a script is run multiple times and the scraper chooses a unique folder name.
- The final combined file is always written under `pipeline_runs\YYYY-MM-DD\`.

## What the Pipeline Prints

For each provider, the pipeline prints:

- source information
- discovery row count
- extraction counts
- missing-field counts
- ISIN whitelist match counts
- saved output paths
- final provider status

After all selected providers finish, it prints:

- the final whitelist filter summary
- the output location of `all_etf_fields.csv`
- per-provider extraction and match counts

## Fresh-Machine Run Checklist

If someone new wants to run this repository successfully, this is the minimum checklist:

1. Clone the repo.
2. Create and activate a virtual environment.
3. Install `requirements.txt`.
4. Run `python -m playwright install chromium`.
5. Make sure `ISIN-list.xlsx` is present in the repo root.
6. Run `python run_all_etf_pipeline.py`.
7. Check `pipeline_runs/YYYY-MM-DD/all_etf_fields.csv`.

## Common Issues

- `ModuleNotFoundError`: the virtual environment is not activated or dependencies are not installed.
- Playwright browser errors: run `python -m playwright install chromium`.
- Empty or partial provider output: the provider website may have changed, timed out, blocked automation, or returned incomplete data.
- Permission errors when writing CSVs: close any open Excel/CSV files before rerunning.
- Missing final rows: the affected ISINs may not be present in `ISIN-list.xlsx`, so they are removed during the final whitelist filter.

## Useful Files

- `run_all_etf_pipeline.py`: main orchestration script
- `providers/output_schema.py`: shared final output schema
- `docs/provider_workflows.md`: provider-by-provider workflow notes
- `docs/source_notes/`: source-specific implementation notes

## Server / Scheduled Runs

If you run this on a server, the setup is the same except the shell commands are usually Linux-based and Playwright runs headlessly.

Recommended server steps:

```bash
git clone https://github.com/bochrakaroui/advisors-scraper.git
cd etf-extractor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python run_all_etf_pipeline.py
```

For scheduled jobs:

- run from the project root
- ensure outbound internet access is allowed
- keep `ISIN-list.xlsx` in place
- do not keep output files open while the job is running
