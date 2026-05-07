"""Per-format zip bundles with README, config snapshot, and optional metadata."""

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import yaml

from oex.logging_setup import get_logger

logger = get_logger(__name__)


def make_zip(
    src_dir: Path,
    zip_path: Path,
    *,
    readme_lines: list[str],
    config_snapshot: dict[str, Any] | None = None,
    metadata_report: dict[str, Any] | None = None,
    cleanup_src: bool = True,
) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    # compresslevel=1: gpkg / shp compress poorly, speed beats a marginal size win.
    buffer = 4 * 1024 * 1024
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
        compresslevel=1,
    ) as zf:
        for entry in src_dir.iterdir():
            if entry.is_dir():
                continue
            size_mb = entry.stat().st_size / (1024 * 1024)
            if size_mb > 100:
                with entry.open("rb") as src_fh, zf.open(entry.name, "w", force_zip64=True) as dst:
                    shutil.copyfileobj(src_fh, dst, buffer)
            else:
                zf.write(entry, arcname=entry.name)

        zf.writestr("README.txt", "\n".join(readme_lines))
        if config_snapshot is not None:
            zf.writestr("config.yaml", yaml.safe_dump(config_snapshot, sort_keys=False))
        if metadata_report is not None:
            zf.writestr("metadata.json", json.dumps(metadata_report, indent=2, default=str))

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    logger.info("Created %s (%.2f MB)", zip_path, size_mb)

    if cleanup_src:
        shutil.rmtree(src_dir, ignore_errors=True)
    return zip_path
