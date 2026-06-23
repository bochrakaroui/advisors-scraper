# Vanguard

- Official page used: `https://www.vanguard.co.uk/uk-fund-directory/product?product-type=etf`
- Primary source for the collected ETF rows: the rendered Vanguard ETF overview table
- ISIN source: Vanguard `gpx/graphql` `FundsQuery` response, keyed by the ETF `portId`
- Fallback for missing ISIN: ETF product detail page text regex

## Saved files

- Raw snapshot: `providers/vanguard/vanguard_downloads/*.json`
- Processed CSV: `providers/vanguard/vanguard_processed/*.csv`

## Notes

- The overview table provides the ETF name, product URL, currency, fund size, and fee.
- `AUM(M)` is normalized from the overview table `Fund size` column.
- `TER(bps)` is normalized from the overview table `Fee` column by multiplying the percentage by `100`.
- No generic `data/` folder is created.

## How To Run

```powershell
python providers\vanguard\download_vanguard.py
python providers\vanguard\extract_vanguard_fields.py
python run_all_etf_pipeline.py --providers vanguard
```
