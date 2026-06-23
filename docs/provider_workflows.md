# Provider Workflows

This file documents how each provider is fetched in the current repo, using the official provider website only.

## Shared Pipeline Behavior

- Each pipeline run creates a new dated folder under `pipeline_runs/`.
- The combined extracted CSV for that run is saved as `all_etf_fields.csv` inside that run folder.
- Each provider raw download folder keeps only one file: the latest raw file from the most recent successful download.
- The provider extractors then read that raw file and output the selected fields used by the combined pipeline.

## iShares

- Official page: `https://www.ishares.com/uk/individual/en/products/etf-investments`
- Website flow:
  - Opens the ETF screener page with Playwright.
  - Passes the T&C gate if shown.
  - Dismisses the OneTrust cookie overlay.
  - Clicks the visible `DOWNLOAD` control.
  - Selects `DOWNLOAD ALL FUNDS (XLS)`.
- Fetch method:
  - Preferred path is the official export link behind the download menu.
  - If the page exposes the direct export URL, the script downloads that response directly.
  - If not, it falls back to Playwright download handling or response capture.
- Raw file saved:
  - `providers/ishares/ishares_downloads/*.xls`
- Extraction source:
  - Official downloaded Excel XML workbook (`.xls`).

## Xtrackers

- Official page: `https://etf.dws.com/en-gb/product-finder/`
- Website flow:
  - Opens the product finder with Playwright.
  - Accepts cookies and the entry gate if shown.
  - Reads the official `downloadxls` link from the page.
- Fetch method:
  - Uses the official XLSX export endpoint behind the product finder.
  - If the page link is not fully hydrated, the script rebuilds the official query payload from the page URL and calls the same export route.
- Raw file saved:
  - `providers/xtrackers/xtrackers_downloads/*.xlsx`
- Extraction source:
  - Official downloaded XLSX file.

## Amundi

- Official page: `https://www.amundietf.co.uk/en/professional/etf-products/search`
- Website flow:
  - Opens the ETF finder with Playwright.
  - Passes the professional-investor disclaimer gate.
  - Clicks the visible `DOWNLOAD` button.
- Fetch method:
  - Preferred path is the official browser download event.
  - If the site generates the workbook as a browser blob instead of a normal file response, the script captures that blob and saves it.
- Raw file saved:
  - `providers/amundi/amundi_downloads/*.xlsx`
- Extraction source:
  - Official downloaded XLSX file or official browser-generated XLSX blob.

## Invesco

- Official page: `https://www.invesco.com/uk/en/financial-products/etfs.html`
- Website flow:
  - Opens the ETF page with Playwright.
  - Dismisses cookies and the country splash overlay.
  - Waits for the live ETF dataset request used by the page.
  - Clicks `Download all data`.
- Fetch method:
  - The script first captures the live official JSON dataset from Invesco's shareclasses API used by the page.
  - It then tries to save the official workbook download.
  - If there is no direct download event, it tries to capture the official workbook blob.
  - Final fallback: it builds a local XLSX from the official live JSON rows already captured from the page.
- Raw file saved:
  - `providers/invesco/invesco_downloads/*.xlsx`
- Extraction source:
  - Usually an official downloaded XLSX.
  - Fallback can be a locally built XLSX created from the official page JSON response.

## UBS

- Official page: `https://www.ubs.com/uk/en/assetmanagement/funds/etf.html`
- Website flow:
  - Opens the ETF page with Playwright.
  - Selects the required UBS context/profile.
  - Dismisses the cookie banner.
  - Waits for the visible `Download Excel` link.
- Fetch method:
  - Uses the official download link exposed on the page.
  - Saves the workbook through Playwright's download handling.
- Raw file saved:
  - `providers/UBS/UBS_etf_downloads/*.xlsx`
- Extraction source:
  - Official downloaded XLSX file.

## SPDR / State Street

- Official page: `https://www.ssga.com/uk/en_gb/intermediary/etfs/fund-finder`
- Fetch method:
  - The main raw file comes from the official XLSX download URL used by SSGA.
  - Currency backfill is taken from the official fund finder JSON endpoint used by the website.
- Raw file saved:
  - `providers/SPDR/spdr_downloads/*.xlsx`
- Extraction source:
  - Main extraction comes from the official downloaded XLSX.
  - `CCY` gaps are filled from the official SPDR fund-finder JSON feed in memory during extraction.
- Notes:
  - The JSON is not persisted anymore; only the latest raw XLSX is kept in the SPDR download folder.

## HSBC Asset Management

- Official page: `https://www.assetmanagement.hsbc.co.uk/en/institutional-investor/funds?f=Yes`
- Website flow:
  - Opens the filtered funds page with Playwright.
  - Captures the initial official `nav/funds` API request context from the page.
  - Replays that same official API request across all pages to collect the ETF listing rows.
- Fetch method:
  - Main raw source is the official HSBC JSON API response used by the website.
  - Additional enrichments use official HSBC sources only:
    - detail page `detail/list` API responses
    - official factsheet PDFs when needed
- Raw file saved:
  - `providers/hsbc/hsbc_downloads/*.json`
- Extraction source:
  - Primary extraction comes from the saved JSON snapshot.
  - Some fields are enriched from official detail API responses and official factsheet PDFs.
- Notes:
  - `CCY` is sourced from the official listing API and factsheet fallback where needed.
  - `AUM(M)` is enriched from official detail/factsheet sources.
- `TER(bps)` is only filled when HSBC exposes it through those official sources.

## J.P. Morgan Asset Management

- Official page: `https://am.jpmorgan.com/gb/en/asset-management/per/products/fund-explorer/etf`
- Fetch method:
  - Uses the official `FundsMarketingHandler/fund-explorer` JSON endpoint behind the UK ETF explorer.
  - The request is filtered with `country=gb`, `role=per`, `language=en`, and `fundType=etf`.
- Raw file saved:
  - `providers/jpmorgan/jpmorgan_downloads/*.json`
- Extraction source:
  - Primary extraction comes directly from the saved JSON snapshot.
- Notes:
  - The listing payload already exposes share-class name, ISIN, share-class currency, ongoing charge, and assets under management.
  - No product-page fallback is currently needed for the J.P. Morgan provider.

## WisdomTree

- Official page: `https://www.wisdomtree.eu/en-gb/products`
- Website flow:
  - Uses the official `WisdomTree Product List` PDF linked from the UK products page.
  - Parses only rows whose official product names contain `UCITS ETF`.
- Fetch method:
  - Downloads the official product-list PDF directly.
  - Builds a provider snapshot from that PDF for the rows that match the UCITS ETF filter.
- Raw file saved:
  - `providers/wisdomtree/wisdomtree_downloads/*.json`
- Extraction source:
  - Primary extraction comes from the saved WisdomTree JSON snapshot built from the official product-list PDF.
- Notes:
  - The downloaded official product-list PDF exposes ISIN, base currency, and TER.
  - `CCY` is taken from the official base currency in that document.
  - `TER(bps)` is derived from the official TER percentage by multiplying by `100`.
  - The downloaded official product-list PDF does not expose fund-level AUM or detail-page URLs, so those fields remain blank instead of being invented.

## Vanguard

- Official page: `https://www.vanguard.co.uk/uk-fund-directory/product?product-type=etf`
- Website flow:
  - Opens the UK ETF overview page with Playwright.
  - Dismisses the cookie banner if shown.
  - Expands the table page size to `All`.
  - Captures the official `FundsQuery` GraphQL request used by the page.
- Fetch method:
  - Main row extraction comes from the rendered overview table.
  - ISIN enrichment comes from replaying the same official `gpx/graphql` `FundsQuery` request used by the page.
  - Product detail pages are only used as a fallback if an ISIN is still missing after the GraphQL lookup.
- Raw file saved:
  - `providers/vanguard/vanguard_downloads/*.json`
- Extraction source:
  - ETF name, product URL, `CCY`, `AUM(M)`, and `TER(bps)` come from the official Vanguard overview table.
  - `ISIN` comes from the official Vanguard GraphQL payload keyed by `portId`.
- Notes:
  - Only ETF product URLs under `/uk-fund-directory/product/etf/` are kept.
  - `AUM(M)` is normalized from the `Fund size` column.
  - `TER(bps)` is derived from the `Fee` column by multiplying the percentage by `100`.

## First Trust

- Official page: `https://www.ftglobalportfolios.com/uk/professional/Products/`
- Fetch method:
  - ETF/shareclass listing rows come from the embedded official products JSON on the UK products page.
  - `TER(bps)` and `AUM(M)` are enriched from the official First Trust fund-facts HTML endpoint used by the site for each share class.
- Raw file saved:
  - `providers/firsttrust/firsttrust_downloads/*.json`
- Extraction source:
  - `ETF Name`, `ISIN`, and `CCY` come from the listing JSON.
  - `TER(bps)` comes from `Total Expense Ratio`.
  - `AUM(M)` comes from `Total Fund AUM`.
- Notes:
  - The scraper keeps share classes that have a `London Stock Exchange` listing in the official listing data.
  - `CCY` is taken from the official shareclass currency.
