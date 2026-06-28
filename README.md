# ETF Extractor

This project downloads ETF data from multiple asset-manager websites, extracts a standard set of fields, and builds one combined CSV.

The final output columns are:

```text
ETF Name,Issuer,ISIN,CCY,TER(bps),AUM(M),Date
```

`Date` is stored as a date only in `dd/mm/yyyy` format.

## How it works

- `scrapers/` contains the provider downloaders
- `providers/<provider>/` contains provider-specific field extractors plus dated raw/output files
- `run_all_etf_pipeline.py` runs the end-to-end workflow
- `ISIN-list.xlsx` is the whitelist used for the final combined output

The main pipeline currently covers:

- iShares, Xtrackers, Amundi, Fidelity, Invesco, UBS, SPDR, HSBC, J.P. Morgan, L&G, Palmer Square, VanEck, Franklin Templeton, WisdomTree, Vanguard, First Trust, HANetf, Global X, FinEx, and iM Global Partner

## Setup on a new machine

Use Python 3.11+.

```powershell
git clone <your-repo-url>
cd etf-extractor
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Make sure `ISIN-list.xlsx` is present in the project root before running the pipeline.

## Main commands

Run the full pipeline with fresh downloads:

```powershell
python run_all_etf_pipeline.py
```

On a successful run, the script prints one section per provider, then a final summary.

Example:

```text
=== iShares ===
Input file   : ...\providers\ishares\2026-06-28\iShares-UnitedKingdom.xls
Output file  : ...\providers\ishares\2026-06-28\ishares_selected_fields_all_funds.csv
Source rows  : 1,380
Rows extracted: 1,380
Excluded rows: 0
Missing values: ETF Name=0, Issuer=0, ISIN=0, CCY=0, TER(bps)=0, AUM(M)=4, Date=0

=== Xtrackers ===
...

=== Final ISIN Whitelist Filter ===
Whitelist unique ISIN count: 2,300
Final rows before filtering: 4,197
Final rows after filtering: 2,122
Removed rows count: 2,075

=== Summary ===
Run folder   : ...\pipeline_runs\2026-06-28
Combined CSV : ...\pipeline_runs\2026-06-28\all_etf_fields.csv
Total rows   : 2,122
```

If one provider fails, the pipeline prints an error for that provider and continues unless you used `--stop-on-error`.

Reuse the latest files already saved in the repo instead of downloading again:

```powershell
python run_all_etf_pipeline.py --use-latest-downloads
```

Run only specific providers:

```powershell
python run_all_etf_pipeline.py --providers ishares xtrackers amundi
```

Stop immediately if one provider fails:

```powershell
python run_all_etf_pipeline.py --stop-on-error
```

## Running one provider manually

Example:

```powershell
python scrapers\jpmorgan_extractor.py
python providers\jpmorgan\extract_jpmorgan_fields.py
```

The same pattern applies to most providers:

1. Run the scraper/downloader
2. Run the matching extractor in `providers/<provider>/`

## Output locations

Provider-level raw files and extracted CSVs are saved in dated folders like:

```text
providers\jpmorgan\2026-06-28\
providers\fidelity\2026-06-28\
```

The final combined file is saved here:

```text
pipeline_runs\YYYY-MM-DD\all_etf_fields.csv
```

After a normal full run, you should expect:

```text
providers\
  ishares\YYYY-MM-DD\iShares-UnitedKingdom.xls
  ishares\YYYY-MM-DD\ishares_selected_fields_all_funds.csv
  xtrackers\YYYY-MM-DD\xtrackers_etf_export.xlsx
  xtrackers\YYYY-MM-DD\xtrackers_selected_fields.csv
  ...

pipeline_runs\
  YYYY-MM-DD\all_etf_fields.csv
```

In other words:

1. Each provider gets its own dated folder with the raw downloaded file.
2. That same provider folder also gets the extracted `selected_fields` CSV.
3. The pipeline then creates one final combined CSV in `pipeline_runs\YYYY-MM-DD\`.

For a first local test, the easiest check is:

1. Run `python run_all_etf_pipeline.py`
2. Confirm a new dated folder appears under `pipeline_runs\`
3. Open `pipeline_runs\YYYY-MM-DD\all_etf_fields.csv`
4. Confirm provider-level dated folders were created under `providers\`

## Useful files

- `run_all_etf_pipeline.py`: main pipeline entry point
- `WORKFLOW.md`: implementation notes
- `docs/provider_workflows.md`: provider-by-provider workflow notes
- `normalize_existing_csv_dates.py`: utility to normalize existing CSV `Date` columns

## Notes

- The final combined output is filtered by `ISIN-list.xlsx`
- Some downloaders use Playwright, so Chromium must be installed
## Running on a server

If the project will run on a server instead of your local machine, the workflow is the same, but setup is usually done in a Linux shell and Playwright runs headlessly.

Typical server setup:

```bash
git clone <your-repo-url>
cd etf-extractor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Make sure these are present on the server before running:

- Python 3.11+
- `ISIN-list.xlsx` in the project root
- outbound internet access to the provider websites

Run the pipeline on the server with:

```bash
python run_all_etf_pipeline.py
```

Or, if you want to reuse files that were already downloaded on the server:

```bash
python run_all_etf_pipeline.py --use-latest-downloads
```

After the run finishes, check:

- `pipeline_runs/YYYY-MM-DD/all_etf_fields.csv`
- `providers/<provider>/YYYY-MM-DD/` for raw files and extracted provider CSVs

Server notes:

- Playwright downloaders run without needing a visible browser window.
- If you schedule this with cron or another job runner, run it from the project root so relative paths still work.
- Do not keep `ISIN-list.xlsx` or output CSVs open while the pipeline is running.
