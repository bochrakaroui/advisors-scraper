# SPDR Source Notes

## Official page used

- Fund finder page:
  `https://www.ssga.com/uk/en_gb/intermediary/etfs/fund-finder`

## How the site loads data

- The fund finder page loads ETF listings from an official SSGA JSON endpoint:
  `https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=uk&language=en_gb&role=intermediary&product=&ui=fund-finder`
- The same page also exposes an official XLSX download link in the "Related Links" section:
  `https://www.ssga.com/uk/en_gb/intermediary/library-content/products/fund-data/etfs/emea/spdr-product-data-emea-en.xlsx`

## Method used in the collector

- Chosen method: official XLSX download
- Reason:
  - it is the preferred source type
  - it already contains the exact fields needed
  - it avoids unnecessary Playwright table scraping

## Fields extracted

- `etf_name` from `Fund Name`
- `issuer` as constant:
  `SPDR / State Street Global Advisors`
- `isin` from `ISIN`
- `ccy` from `Share Class Currency`
- `aum_mn` from `Total Fund Assets Raw`, converted to millions

## Limitations

- `aum_mn` is derived from the raw total fund assets value and divided by `1,000,000`
- the collector assumes the official workbook keeps the same header names
- if SSGA removes or renames the official XLSX link, the collector would need to fall back to the official JSON endpoint

## How to run

```powershell
python scrapers\spdr_collector.py
```

## Output locations

- Raw source files:
  `providers/SPDR/spdr_downloads/`
- Cleaned CSV files:
  `providers/SPDR/spdr_processed/`
