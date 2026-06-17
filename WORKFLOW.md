# ETF Extractor Workflow

This file documents the current workflow implemented in this project.

## Goal

Download ETF source files from official provider websites, then extract a clean dataset with these fields:

- ETF Name
- Issuer
- Asset Class
- CCY
- TER (bps)
- Listing Date
- Distribution
- ISIN
- Ticker
- AUM(M)

`AUM(M)` must be preserved.

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
- `providers/invesco/`
  - Invesco downloaded files

## Requirements

Install Python dependencies:

```powershell
pip install playwright
python -m playwright install chromium
```

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
python providers\ishares\extract_ishares_fields.py --all-funds
```

Output folder:

```text
providers\ishares\ishares_processed\
```

Current iShares processing rules:

- Reads the latest downloaded `.xls` file by default
- Parses the XML spreadsheet directly
- Normalizes header whitespace so headers like `Distribution Type` and `AUM (M)` still work even when the source file contains line breaks
- Keeps ETF rows only by default
- Preserves `AUM(M)` from the source file
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
- Keeps the `Ticker` column in the output
- Leaves `Ticker` blank by default
- Can optionally enrich ticker values from official Xtrackers product pages
- Converts source AUM from GBP into the ETF row currency
- Writes AUM as `AUM(M)`

## Xtrackers AUM Conversion Rule

The current rule is:

- if `As of` is present, use the ECB FX rate for that date
- if `As of` is empty, use the latest available ECB FX rate

Notes:

- source AUM in the downloaded Xtrackers file is `AuM (GBP)`
- output AUM is converted to the ETF row `CCY`
- output value is written in millions as `AUM(M)`
- rates are not hard-coded
- FX source is the ECB daily reference feed

## Output Format

The processed CSV output columns are:

```text
ETF Name,Issuer,Asset Class,CCY,TER (bps),Listing Date,Distribution,ISIN,Ticker,AUM(M)
```

## Verification Commands

Useful checks:

```powershell
python -m py_compile scrapers\Amundi_extractor.py scrapers\ishares_extractor.py scrapers\Xtrackers_extractor.py scrapers\invesco_extractor.py providers\ishares\extract_ishares_fields.py providers\xtrackers\extract_xtrackers_fields.py
```

Run iShares processing:

```powershell
python providers\ishares\extract_ishares_fields.py
```

Run Xtrackers processing:

```powershell
python providers\xtrackers\extract_xtrackers_fields.py
```

## Current Provider Status

- `iShares`: downloader and processor are working
- `Xtrackers`: downloader and processor are working
- `Amundi`: downloader is implemented
- `Invesco`: downloader is implemented

## Important Notes

- Folder structure was intentionally not changed
- Working extractor logic was preserved
- AUM handling was preserved
- Xtrackers FX conversion stays dynamic and does not use static exchange rates
- For Xtrackers, network access is needed during processing to fetch ECB FX rates
