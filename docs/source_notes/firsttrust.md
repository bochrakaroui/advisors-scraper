# First Trust

- Official page used: `https://www.ftglobalportfolios.com/uk/professional/Products/`
- Listing source: embedded official products JSON on the UK products page
- Detail enrichment source: official First Trust product detail pages, using `isin_code` in the URL
- AUM source: `Total Fund AUM` on the product overview page
- TER source: `Total Expense Ratio` on the product overview page

## Saved files

- Raw snapshot: `providers/firsttrust/firsttrust_downloads/*.json`
- Processed CSV: `providers/firsttrust/firsttrust_processed/*.csv`

## Notes

- The listing page JSON exposes ETF names, product URLs, shareclass ISINs, and shareclass currencies.
- The scraper keeps only share classes that have a `London Stock Exchange` listing in the official listing data.
- `CCY` is taken from the official shareclass currency.
- `AUM(M)` is normalized from `Total Fund AUM`.
- `TER(bps)` is normalized from `Total Expense Ratio`.
