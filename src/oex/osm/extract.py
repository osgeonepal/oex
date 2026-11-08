"""osmium-tool wrappers for clipping a country out of a planet PBF. pyosmium does not
expose `extract --strategy=complete_ways`, so this shells out.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

from oex.logging_setup import get_logger

logger = get_logger(__name__)

ExtractStrategy = Literal["simple", "complete_ways", "smart"]

_INSTALL_HINTS = {
    "linux": "Fedora/RHEL: sudo dnf install osmium-tool\n  Debian/Ubuntu: sudo apt install osmium-tool",
    "darwin": "macOS: brew install osmium-tool",
}


class OsmiumNotInstalledError(RuntimeError):
    pass


class OsmiumExtractError(RuntimeError):
    pass


def osmium_polygon_extract(
    pbf_path: Path,
    polygon_geojson: dict[str, Any],
    out_pbf: Path,
    *,
    strategy: ExtractStrategy = "complete_ways",
) -> None:
    """osmium's banded algorithm makes vertex count cheap, so the boundary goes in at full
    precision."""
    osmium = shutil.which("osmium")
    if osmium is None:
        platform = sys.platform if sys.platform in _INSTALL_HINTS else "linux"
        raise OsmiumNotInstalledError(
            f"osmium-tool binary not found on PATH. Install it:\n  {_INSTALL_HINTS[platform]}"
        )

    if not pbf_path.is_file():
        raise FileNotFoundError(f"input PBF missing: {pbf_path}")
    out_pbf.parent.mkdir(parents=True, exist_ok=True)
    if out_pbf.exists():
        out_pbf.unlink()

    feature_collection = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": polygon_geojson, "properties": {}}],
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".geojson", delete=False, encoding="utf-8"
    ) as geojson_file:
        json.dump(feature_collection, geojson_file)
        geojson_path = Path(geojson_file.name)
    try:
        command = [
            osmium,
            "extract",
            "-p",
            str(geojson_path),
            "--strategy",
            strategy,
            "-o",
            str(out_pbf),
            str(pbf_path),
        ]
        logger.info("osmium extract: %s", " ".join(command))
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            tail = completed.stderr.strip().splitlines()[-10:]
            raise OsmiumExtractError(
                f"osmium extract failed (rc={completed.returncode}): " + " | ".join(tail)
            )
    finally:
        geojson_path.unlink(missing_ok=True)

    if not out_pbf.is_file() or out_pbf.stat().st_size == 0:
        raise OsmiumExtractError(f"osmium extract produced no output at {out_pbf}")
    logger.info(
        "osmium extract done: %s -> %s (%.1f MiB)",
        pbf_path.name,
        out_pbf.name,
        out_pbf.stat().st_size / 1024**2,
    )
