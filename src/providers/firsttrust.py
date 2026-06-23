"""Legacy First Trust entrypoint kept as a wrapper around the provider-folder architecture."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from providers.firsttrust.extract_firsttrust_fields import process_file
from scrapers.firsttrust_extractor import download_firsttrust_file


def main() -> None:
    raw_snapshot_path = asyncio.run(download_firsttrust_file())
    process_file(raw_snapshot_path)


if __name__ == "__main__":
    main()
