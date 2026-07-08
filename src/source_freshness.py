from __future__ import annotations

import json
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


SOURCE_METADATA_SUFFIX = ".source.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_METADATA_DIR = REPO_ROOT / ".source_metadata"


def legacy_metadata_path_for(raw_path: Path) -> Path:
    return raw_path.with_name(f"{raw_path.name}{SOURCE_METADATA_SUFFIX}")


def metadata_path_for(raw_path: Path) -> Path:
    try:
        relative_raw_path = raw_path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return legacy_metadata_path_for(raw_path)
    return SOURCE_METADATA_DIR / relative_raw_path.parent / f"{raw_path.name}{SOURCE_METADATA_SUFFIX}"


def parse_http_last_modified(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return ""
    return parsed.date().isoformat()


def normalize_source_date(value: str | None) -> str:
    if not value:
        return ""
    cleaned = str(value).strip()
    if not cleaned:
        return ""
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def atomic_write_bytes(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, prefix=f"{destination.name}.", suffix=".tmp", delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
    temp_path.replace(destination)


def write_source_metadata(raw_path: Path, payload: dict[str, Any]) -> Path:
    metadata_path = metadata_path_for(raw_path)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return metadata_path


def load_source_metadata(raw_path: Path) -> dict[str, Any]:
    for metadata_path in (metadata_path_for(raw_path), legacy_metadata_path_for(raw_path)):
        if not metadata_path.exists():
            continue
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}
