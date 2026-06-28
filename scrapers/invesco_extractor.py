"""Download the Invesco ETF export workbook."""

import asyncio
import json
import math
import os
import shutil
import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, TimeoutError as PlaywrightTimeoutError, async_playwright


URL = "https://www.invesco.com/uk/en/financial-products/etfs.html"
BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "providers" / "invesco"
TIMEOUT_MS = 120_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

INIT_SCRIPT = """
(() => {
    window.__invescoCapturedBlobs = [];

    const originalCreateObjectURL = URL.createObjectURL.bind(URL);
    URL.createObjectURL = function (value) {
        try {
            if (value instanceof Blob) {
                window.__invescoCapturedBlobs.push(value);
            }
        } catch (error) {}

        return originalCreateObjectURL(value);
    };
})();
"""

INVALID_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
RUN_FOLDER_ENV_VAR = "ETF_PIPELINE_RUN_FOLDER"


def build_run_output_dir(base_dir: Path) -> Path:
    run_folder_name = os.environ.get(RUN_FOLDER_ENV_VAR)
    if run_folder_name:
        output_dir = base_dir / run_folder_name
    else:
        run_date = datetime.now().strftime("%Y-%m-%d")
        output_dir = base_dir / run_date
        os.environ[RUN_FOLDER_ENV_VAR] = output_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_output_path() -> Path:
    return build_run_output_dir(OUTPUT_DIR) / "invesco_etf_export.xlsx"


def is_shareclasses_url(url: str) -> bool:
    return "dng-api.invesco.com/cache/v1/accounts/" in url and "shareclasses" in url


def sanitize_xml_text(value: str) -> str:
    cleaned = INVALID_XML_RE.sub("", value)
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def normalize_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def excel_column_name(index: int) -> str:
    letters: list[str] = []
    current = index + 1
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))


def cell_xml(cell_ref: str, value: Any) -> str:
    value = normalize_cell_value(value)

    if value == "":
        return f'<c r="{cell_ref}"/>'

    if isinstance(value, bool):
        return f'<c r="{cell_ref}" t="b"><v>{1 if value else 0}</v></c>'

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            text = sanitize_xml_text(str(value))
            return f'<c r="{cell_ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
        return f'<c r="{cell_ref}"><v>{value}</v></c>'

    text = sanitize_xml_text(str(value))
    return f'<c r="{cell_ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def ordered_columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    columns: list[str] = []

    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                columns.append(key)

    return columns


def build_sheet_xml(rows: list[dict[str, Any]]) -> str:
    if not rows:
        rows = [{"message": "No ETF rows were returned by the page response."}]

    columns = ordered_columns(rows)
    if not columns:
        columns = ["message"]
        rows = [{"message": "No ETF columns were returned by the page response."}]

    sheet_rows: list[str] = []

    header_cells = [
        cell_xml(f"{excel_column_name(index)}1", column)
        for index, column in enumerate(columns)
    ]
    sheet_rows.append(f'<row r="1">{"".join(header_cells)}</row>')

    for row_index, row in enumerate(rows, start=2):
        cells = [
            cell_xml(f"{excel_column_name(column_index)}{row_index}", row.get(column, ""))
            for column_index, column in enumerate(columns)
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    last_cell = f"{excel_column_name(len(columns) - 1)}{len(rows) + 1}"

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:{last_cell}"/>
  <sheetViews>
    <sheetView workbookViewId="0"/>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <sheetData>
    {''.join(sheet_rows)}
  </sheetData>
</worksheet>
"""


def build_xlsx_bytes(rows: list[dict[str, Any]]) -> bytes:
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sheet_xml = build_sheet_xml(rows)

    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
"""

    root_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Invesco ETF Data" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

    workbook_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""

    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
            xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Microsoft Excel</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <HeadingPairs>
    <vt:vector size="2" baseType="variant">
      <vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant>
      <vt:variant><vt:i4>1</vt:i4></vt:variant>
    </vt:vector>
  </HeadingPairs>
  <TitlesOfParts>
    <vt:vector size="1" baseType="lpstr">
      <vt:lpstr>Invesco ETF Data</vt:lpstr>
    </vt:vector>
  </TitlesOfParts>
  <Company></Company>
  <LinksUpToDate>false</LinksUpToDate>
  <SharedDoc>false</SharedDoc>
  <HyperlinksChanged>false</HyperlinksChanged>
  <AppVersion>16.0300</AppVersion>
</Properties>
"""

    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created_at}</dcterms:modified>
  <dc:title>Invesco ETF Export</dc:title>
</cp:coreProperties>
"""

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", content_types_xml)
        workbook.writestr("_rels/.rels", root_rels_xml)
        workbook.writestr("docProps/app.xml", app_xml)
        workbook.writestr("docProps/core.xml", core_xml)
        workbook.writestr("xl/workbook.xml", workbook_xml)
        workbook.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return buffer.getvalue()


async def click_with_fallback(locator: Locator, label: str) -> None:
    await locator.wait_for(state="visible", timeout=TIMEOUT_MS)
    await locator.scroll_into_view_if_needed()

    try:
        await locator.click(timeout=10_000)
    except Exception as exc:
        print(f"    Normal click failed for {label}: {exc}")
        print(f"    Retrying {label} with force click.")
        try:
            await locator.click(timeout=10_000, force=True)
        except Exception:
            print(f"    Falling back to DOM click for {label}.")
            await locator.evaluate("(element) => element.click()")


async def find_first_visible_locator(
    selectors: list[tuple[str, Locator]],
    timeout_ms: int = 5_000,
) -> tuple[str, Locator]:
    for label, locator in selectors:
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            return label, locator
        except Exception:
            continue

    raise TimeoutError("No visible matching locator found")


async def dismiss_cookie_banner(page) -> None:
    candidates = [
        ("OneTrust accept button", page.locator("button#onetrust-accept-btn-handler").first),
        ("Accept All button", page.locator("button:has-text('Accept All')").first),
    ]

    for label, locator in candidates:
        try:
            await locator.wait_for(state="visible", timeout=2_500)
            print(f"    Dismissing cookie banner via {label} ...")
            await click_with_fallback(locator, label)
            await page.wait_for_timeout(1_000)
            return
        except Exception:
            continue


async def dismiss_country_splash(page) -> None:
    splash = page.locator(".country-splash").first
    splash_count = await page.locator(".country-splash").count()

    try:
        await splash.wait_for(state="visible", timeout=10_000)
        print("    Removing country splash overlay ...")
    except Exception:
        if splash_count == 0:
            print("    No country splash overlay detected.")
            return
        print("    Removing residual country splash nodes ...")

    continue_button = page.locator(".country-splash button:has-text('Continue')").first
    try:
        await continue_button.wait_for(state="visible", timeout=2_000)
        await click_with_fallback(continue_button, "Country splash continue button")
        await page.wait_for_timeout(1_000)
    except Exception:
        pass

    await page.evaluate(
        """
        () => {
            for (const selector of [
                '.country-splash',
                '.country-splash__background',
                '.country-splash__container',
                '.country-splash__dialog',
            ]) {
                for (const node of document.querySelectorAll(selector)) {
                    node.remove();
                }
            }

            document.body.style.overflow = 'auto';
            document.documentElement.style.overflow = 'auto';
        }
        """
    )
    await page.wait_for_timeout(500)


async def wait_for_blob_bytes(page, attempts: int = 20, delay_ms: int = 1_000) -> bytes | None:
    for _ in range(attempts):
        raw_bytes = await page.evaluate(
            """
            async () => {
                const blobs = window.__invescoCapturedBlobs || [];

                for (let index = blobs.length - 1; index >= 0; index -= 1) {
                    const blob = blobs[index];
                    if (!blob) {
                        continue;
                    }

                    const type = (blob.type || '').toLowerCase();
                    const looksLikeWorkbook =
                        type.includes('excel') ||
                        type.includes('spreadsheet') ||
                        type.includes('officedocument') ||
                        (blob.size > 4096 && !type.includes('javascript'));

                    if (!looksLikeWorkbook) {
                        continue;
                    }

                    return Array.from(new Uint8Array(await blob.arrayBuffer()));
                }

                return null;
            }
            """
        )

        if raw_bytes:
            return bytes(raw_bytes)

        await page.wait_for_timeout(delay_ms)

    return None


async def wait_for_shareclasses_data(
    shareclasses_future: "asyncio.Future[tuple[str, list[dict[str, Any]]]]",
    timeout_seconds: int = 45,
) -> tuple[str, list[dict[str, Any]]]:
    return await asyncio.wait_for(shareclasses_future, timeout=timeout_seconds)


async def download_invesco_file() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=USER_AGENT,
            accept_downloads=True,
            viewport={"width": 1440, "height": 1400},
        )
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()

        loop = asyncio.get_running_loop()
        shareclasses_future: "asyncio.Future[tuple[str, list[dict[str, Any]]]]" = loop.create_future()

        async def handle_response(response) -> None:
            if shareclasses_future.done():
                return
            if response.status != 200 or not is_shareclasses_url(response.url):
                return

            try:
                payload = json.loads(await response.text())
            except Exception:
                return

            if isinstance(payload, list) and payload:
                shareclasses_future.set_result((response.url, payload))

        page.on("response", lambda response: asyncio.create_task(handle_response(response)))

        print("[1/5] Loading Invesco ETF page ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(6_000)

        print("[2/5] Clearing overlays ...")
        await dismiss_cookie_banner(page)
        await dismiss_country_splash(page)

        print("[3/5] Waiting for the live ETF dataset response ...")
        data_url, rows = await wait_for_shareclasses_data(shareclasses_future)
        print(f"    Captured {len(rows):,} ETF rows from:")
        print(f"    {data_url}")

        print("[4/5] Clicking the visible download control ...")
        await dismiss_country_splash(page)
        label, download_button = await find_first_visible_locator(
            [
                (
                    "button.download-all__button",
                    page.locator("button.download-all__button").first,
                ),
                (
                    "button[aria-label='Download all data']",
                    page.locator("button[aria-label='Download all data']").first,
                ),
                (
                    "role button named Download all data",
                    page.get_by_role("button", name=re.compile(r"download all data", re.IGNORECASE)).first,
                ),
            ],
            timeout_ms=30_000,
        )
        print(f"    Using locator for {label}.")

        final_path = build_output_path()

        try:
            async with page.expect_download(timeout=15_000) as download_info:
                await click_with_fallback(download_button, "Download all data button")

            download = await download_info.value
            await download.save_as(final_path)
            print(f"    Browser download captured -> {final_path}")
            await browser.close()
            return final_path
        except PlaywrightTimeoutError:
            print("    No direct browser download event detected.")

        blob_bytes = await wait_for_blob_bytes(page, attempts=20, delay_ms=1_000)
        if blob_bytes and blob_bytes[:2] == b"PK":
            final_path.write_bytes(blob_bytes)
            print(f"    Workbook blob captured -> {final_path}")
            print(f"    Size: {len(blob_bytes):,} bytes")
            await browser.close()
            return final_path

        print("[5/5] Falling back to an XLSX built from the live page data ...")
        workbook_bytes = build_xlsx_bytes(rows)
        final_path.write_bytes(workbook_bytes)
        print(f"    Generated workbook -> {final_path}")
        print(f"    Size: {len(workbook_bytes):,} bytes")

        await browser.close()
        return final_path


if __name__ == "__main__":
    saved = asyncio.run(download_invesco_file())
    print(f"\nDone! Open your file at: {saved.resolve()}")
