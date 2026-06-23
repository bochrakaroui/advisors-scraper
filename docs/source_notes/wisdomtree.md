# WisdomTree

- Official page used: `https://www.wisdomtree.eu/en-gb/products`
- Raw source used by the collector: the official `WisdomTree Product List` PDF linked from that products page
- Fetch method: direct download of the official product-list PDF, then parsing only rows whose official product names contain `UCITS ETF`
- Saved raw snapshot: `providers/wisdomtree/wisdomtree_downloads/*.json`
- Saved processed CSV: `providers/wisdomtree/wisdomtree_processed/*.csv`

## Notes

- The current collector does not create a separate generic `data` folder.
- The snapshot keeps only WisdomTree Europe `UCITS ETF` rows and excludes ETP rows whose names do not contain `UCITS ETF`.
- The downloaded official product-list PDF exposes ISIN, base currency, and TER.
- The downloaded official product-list PDF does not expose fund-level AUM or product detail URLs, so those fields remain blank in the raw snapshot instead of being invented.

## Run

Download the latest WisdomTree snapshot:

```powershell
python scrapers\wisdomtree_extractor.py
```

Process the latest WisdomTree snapshot:

```powershell
python providers\wisdomtree\extract_wisdomtree_fields.py
```

Run WisdomTree through the full pipeline:

```powershell
python run_all_etf_pipeline.py --providers wisdomtree
```
