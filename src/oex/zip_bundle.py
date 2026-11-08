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
    stage_dir: Path,
    zip_path: Path,
    *,
    readme_lines: list[str],
    config_snapshot: dict[str, Any] | None = None,
    metadata_report: dict[str, Any] | None = None,
) -> Path:
    """Zip every file in `stage_dir` with the README and snapshots, then delete `stage_dir`."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
        compresslevel=1,  # gpkg and shp compress poorly, so speed beats a marginal size win
    ) as archive:
        for entry in stage_dir.iterdir():
            if entry.is_dir():
                continue
            archive.write(entry, arcname=entry.name)

        archive.writestr("README.txt", "\n".join(readme_lines))
        if config_snapshot is not None:
            archive.writestr("config.yaml", yaml.safe_dump(config_snapshot, sort_keys=False))
        if metadata_report is not None:
            archive.writestr(
                "metadata.json",
                json.dumps(metadata_report, indent=2, default=str, ensure_ascii=False),
            )

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    logger.info("Created %s (%.2f MB)", zip_path, size_mb)

    shutil.rmtree(stage_dir)
    return zip_path
