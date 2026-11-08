"""Fetch and cache fieldmaps.io edge-matched admin parquets."""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from oex.logging_setup import get_logger

logger = get_logger(__name__)


class PcodeCacheError(RuntimeError):
    pass


@dataclass(frozen=True)
class PcodeCacheEntry:
    level: int
    path: Path
    upstream_date: str
    upstream_url: str


def _load_local_metadata(metadata_path: Path) -> dict[str, Any]:
    if not metadata_path.exists():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PcodeCacheError(f"Could not read pcode cache meta {metadata_path}: {exc}") from exc


def _write_local_metadata(metadata_path: Path, payload: dict[str, Any]) -> None:
    """Replace atomically: concurrent exports share this file and interleave otherwise."""
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{metadata_path.name}.", dir=str(metadata_path.parent)
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary_path, metadata_path)


def _fetch_manifest(manifest_url: str, *, timeout: float = 60.0) -> list[dict[str, Any]]:
    logger.debug("Fetching fieldmaps manifest: %s", manifest_url)
    response = requests.get(manifest_url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise PcodeCacheError(
            f"Unexpected manifest shape from {manifest_url}: expected list, got {type(payload).__name__}"
        )
    return payload


def _select_manifest_entry(
    manifest: list[dict[str, Any]], group: str, level: int
) -> dict[str, Any]:
    for entry in manifest:
        if entry.get("grp") == group and entry.get("adm") == level:
            return entry
    raise PcodeCacheError(
        f"Manifest has no entry for group={group!r} adm={level}; "
        f"available: {[(item.get('grp'), item.get('adm')) for item in manifest]}"
    )


def _atomic_download(url: str, target: Path, *, timeout: float = 600.0) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    start = time.time()
    bytes_read = 0
    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with temporary_path.open("wb") as output_file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output_file.write(chunk)
                        bytes_read += len(chunk)
        os.replace(temporary_path, target)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    duration = time.time() - start
    megabytes = bytes_read / (1024 * 1024)
    logger.info(
        "Downloaded %s (%.1f MB in %.1fs) -> %s",
        url,
        megabytes,
        duration,
        target,
    )


def ensure_admin_parquets(
    *,
    cache_dir: Path,
    levels: list[int],
    manifest_url: str,
    parquet_url_template: str,
    manifest_group: str,
) -> dict[int, PcodeCacheEntry]:
    if not levels:
        raise PcodeCacheError("ensure_admin_parquets requires at least one level")

    cache_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = cache_dir / "meta.json"
    local_metadata = _load_local_metadata(metadata_path)

    manifest = _fetch_manifest(manifest_url)

    entries: dict[int, PcodeCacheEntry] = {}
    new_metadata: dict[str, Any] = dict(local_metadata)
    new_metadata.setdefault("levels", {})

    for level in sorted(set(levels)):
        manifest_entry = _select_manifest_entry(manifest, manifest_group, level)
        upstream_date = str(manifest_entry.get("date") or "")
        if not upstream_date:
            raise PcodeCacheError(
                f"Manifest entry for adm{level} is missing a `date`: {manifest_entry}"
            )

        upstream_url = parquet_url_template.format(level=level)
        target_path = cache_dir / f"adm{level}_polygons.parquet"

        prior = new_metadata["levels"].get(str(level), {})
        prior_date = prior.get("date")

        if target_path.exists() and prior_date == upstream_date:
            logger.info(
                "[pcodes] adm%d up to date (%s) -> %s",
                level,
                upstream_date,
                target_path,
            )
        else:
            logger.info(
                "[pcodes] adm%d %s -> %s (was %s)",
                level,
                upstream_date,
                target_path,
                prior_date or "<absent>",
            )
            _atomic_download(upstream_url, target_path)
            new_metadata["levels"][str(level)] = {
                "date": upstream_date,
                "url": upstream_url,
                "path": str(target_path),
            }

        entries[level] = PcodeCacheEntry(
            level=level,
            path=target_path,
            upstream_date=upstream_date,
            upstream_url=upstream_url,
        )

    new_metadata["manifest_url"] = manifest_url
    new_metadata["manifest_group"] = manifest_group
    _write_local_metadata(metadata_path, new_metadata)

    logger.debug(
        "[pcodes] cache resolved: %s",
        {level: asdict(entry) for level, entry in entries.items()},
    )
    return entries
