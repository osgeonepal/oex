"""PBF -> per-theme GeoParquet cache via quackosm."""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from shapely.geometry.base import BaseGeometry

from oex.config.schema import CategoryConfig, RootConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ThemeOutput:
    theme: str
    path: str
    duration_s: float
    row_count: int | None = None


@dataclass
class CacheManifest:
    snapshot: str
    pbf_source: str
    pbf_size_bytes: int
    themes: list[ThemeOutput] = field(default_factory=list)

    def write(self, out_dir: Path) -> Path:
        target = out_dir / "manifest.json"
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return target


def _build_filter(category: CategoryConfig) -> dict[str, list[str] | str | bool]:
    raw = dict(category.osm.filter)
    out: dict[str, list[str] | str | bool] = {}
    for key, value in raw.items():
        if value is True or value == "*":
            out[key] = True
        elif isinstance(value, str):
            out[key] = value
        elif isinstance(value, list):
            out[key] = [str(v) for v in value]
        else:
            raise ValueError(
                f"Category {category.name!r} tag {key!r} has unsupported value: {value!r}"
            )
    if not out:
        raise ValueError(f"Category {category.name!r} has no osm.filter")
    return out


def theme_slug(category: CategoryConfig) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", category.name).lower().strip("_")


def build_cache(
    cfg: RootConfig,
    pbf_path: str | Path,
    *,
    cache_root: Path,
    snapshot: str | None = None,
    themes_filter: list[str] | None = None,
    geometry_filter: BaseGeometry | None = None,
) -> CacheManifest:
    """Materialise <cache_root>/<snapshot>/<theme>.parquet for each enabled category."""
    from quackosm import convert_pbf_to_parquet

    pbf = Path(pbf_path)
    if not pbf.exists():
        raise FileNotFoundError(pbf)

    snap = snapshot or date.today().isoformat()
    snapshot_dir = cache_root / snap
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    manifest = CacheManifest(
        snapshot=snap,
        pbf_source=str(pbf),
        pbf_size_bytes=pbf.stat().st_size,
    )

    selected_names: set[str] | None = None
    if themes_filter:
        selected_names = {n.lower().replace(" ", "_") for n in themes_filter}

    for category in cfg.categories:
        if not category.osm.enabled:
            continue
        slug = theme_slug(category)
        if selected_names is not None and slug not in selected_names:
            continue

        target = snapshot_dir / f"{slug}.parquet"
        try:
            tag_filter = _build_filter(category)
        except ValueError as exc:
            logger.warning("Skipping %s: %s", category.name, exc)
            continue

        if target.exists():
            logger.info("[%s] cache already exists, skipping: %s", slug, target)
            manifest.themes.append(ThemeOutput(theme=slug, path=str(target), duration_s=0.0))
            continue

        logger.info("[%s] converting PBF -> %s", slug, target)
        start = time.time()
        out_path = convert_pbf_to_parquet(
            pbf_path=str(pbf),
            tags_filter=tag_filter,
            geometry_filter=geometry_filter,
            result_file_path=str(target),
            # False here drops every tag not in the filter, so tags['name'] returns NULL.
            keep_all_tags=True,
            explode_tags=False,
            sort_result=True,
            working_directory=str(snapshot_dir / "_work"),
        )
        duration = time.time() - start
        manifest.themes.append(ThemeOutput(theme=slug, path=str(out_path), duration_s=duration))
        logger.info("[%s] done in %.1fs -> %s", slug, duration, out_path)

    manifest.write(snapshot_dir)
    logger.info("Wrote cache manifest: %s", snapshot_dir / "manifest.json")
    return manifest
