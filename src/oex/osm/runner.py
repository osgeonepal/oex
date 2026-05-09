"""OSM source runner: geofabrik (per-country PBF) or planet_parquet engine."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from oex.boundary import resolve_boundary
from oex.config.schema import (
    CategoryConfig,
    OsmSourceConfig,
    RootConfig,
)
from oex.locale import local_osm_languages
from oex.logging_setup import get_logger
from oex.osm.build_cache import build_cache, theme_slug
from oex.osm.fetch_planet import download_pbf
from oex.osm.geofabrik import lookup_country
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner

logger = get_logger(__name__)


def _inject_local_name(select_fields: list[str], iso3: str) -> list[str]:
    """Append `tags['name:<lang>'] AS name_<lang>` for each local OSM language.

    Skips a language if its alias is already present (per-category YAML wins).
    """
    languages = local_osm_languages(iso3)
    if not languages:
        return select_fields

    new_fields = list(select_fields)
    insert_at = len(new_fields)
    for index, field in enumerate(new_fields):
        if "AS name_en" in field or field.endswith(" name_en"):
            insert_at = index + 1
            break

    existing = "\n".join(new_fields)
    for lang in languages:
        alias = f"name_{lang}"
        if alias in existing:
            continue
        new_fields.insert(insert_at, f"tags['name:{lang}'] AS {alias}")
        insert_at += 1
    return new_fields


def _resolve_snapshot(cache_root: Path, requested: str) -> str:
    if requested and requested != "latest":
        if not (cache_root / requested).is_dir():
            raise FileNotFoundError(
                f"No OSM cache snapshot {requested!r} under {cache_root}. "
                "Run 'oex-cli osm-build-cache' first."
            )
        return requested
    snapshots = sorted(p.name for p in cache_root.iterdir() if p.is_dir())
    if not snapshots:
        raise FileNotFoundError(
            f"No OSM cache snapshots found in {cache_root}. Run 'oex-cli osm-build-cache' first."
        )
    return snapshots[-1]


class OsmRunner(SourceRunner):
    name = "osm"

    def __init__(self) -> None:
        self._snapshot_dir: Path | None = None
        self._snapshot_label: str | None = None
        self._snapshot_date: datetime | None = None
        self._dataset_source: str = "OpenStreetMap"

    def prepare(self, cfg: RootConfig) -> None:
        src = cast(OsmSourceConfig, cfg.source["osm"])
        if not src.enabled:
            raise RuntimeError("OSM source is disabled in config")

        engine = (src.engine or "geofabrik").lower()
        if engine == "geofabrik":
            self._prepare_geofabrik(cfg, src)
        elif engine == "planet_parquet":
            self._prepare_planet_parquet(cfg, src)
        else:
            raise ValueError(
                f"Unknown osm.engine={engine!r}; expected 'geofabrik' or 'planet_parquet'"
            )

    def _prepare_planet_parquet(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        cache_root = Path(src.cache_dir) / "planet"
        if not cache_root.is_dir():
            raise FileNotFoundError(
                f"OSM planet cache directory {cache_root} does not exist. "
                "Run 'oex-cli osm-build-cache --planet' first."
            )
        snapshot = _resolve_snapshot(cache_root, src.snapshot)
        self._snapshot_dir = cache_root / snapshot
        self._snapshot_label = snapshot
        self._snapshot_date = self._infer_snapshot_date(self._snapshot_dir, snapshot)
        self._dataset_source = f"OpenStreetMap planet {snapshot}"
        logger.info(
            "OSM source: planet_parquet, snapshot=%s, cache=%s", snapshot, self._snapshot_dir
        )

    def _prepare_geofabrik(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        if not cfg.iso3:
            raise ValueError("osm.engine=geofabrik requires `iso3` in the config")

        country_root = Path(src.cache_dir) / "geofabrik" / cfg.iso3.lower()
        country_root.mkdir(parents=True, exist_ok=True)

        snapshot = self._resolve_or_create_snapshot(country_root, src.snapshot)
        snapshot_dir = country_root / snapshot
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        expected = self._expected_themes(cfg)
        existing = {p.stem for p in snapshot_dir.glob("*.parquet")}
        missing = expected - existing

        if missing:
            logger.info(
                "Geofabrik %s snapshot %s missing themes: %s; building",
                cfg.iso3,
                snapshot,
                sorted(missing),
            )
            self._build_geofabrik_themes(cfg, src, country_root, snapshot)
        else:
            logger.info(
                "Geofabrik %s snapshot %s already has all required themes; reusing",
                cfg.iso3,
                snapshot,
            )

        self._snapshot_dir = snapshot_dir
        self._snapshot_label = snapshot
        self._snapshot_date = self._infer_snapshot_date(self._snapshot_dir, snapshot)
        self._dataset_source = f"OpenStreetMap (Geofabrik {cfg.iso3.upper()} {snapshot})"
        logger.info(
            "OSM source: geofabrik %s, snapshot=%s, cache=%s",
            cfg.iso3.upper(),
            snapshot,
            self._snapshot_dir,
        )

    @staticmethod
    def _expected_themes(cfg: RootConfig) -> set[str]:
        return {theme_slug(c) for c in cfg.categories if c.osm.enabled}

    @staticmethod
    def _resolve_or_create_snapshot(country_root: Path, requested: str) -> str:
        if requested and requested != "latest":
            return requested
        existing = sorted(p.name for p in country_root.iterdir() if p.is_dir() and p.name != "_pbf")
        if existing:
            return existing[-1]
        return datetime.now(UTC).date().isoformat()

    def _build_geofabrik_themes(
        self,
        cfg: RootConfig,
        src: OsmSourceConfig,
        country_root: Path,
        snapshot: str,
    ) -> None:
        extract = lookup_country(cfg.iso3, index_url=src.geofabrik_index_url)
        pbf_dir = country_root / "_pbf"
        pbf_path = pbf_dir / f"{extract.geofabrik_id}-latest.osm.pbf"

        if not pbf_path.exists():
            logger.info(
                "Geofabrik extract for %s: %s (%s)",
                cfg.iso3,
                extract.geofabrik_id,
                extract.pbf_url,
            )
            result = download_pbf(
                extract.pbf_url,
                pbf_dir,
                md5_url=extract.md5_url,
                filename=pbf_path.name,
            )
            pbf_path = result.path
        else:
            logger.info("Reusing already-downloaded PBF: %s", pbf_path)

        geometry_filter = None
        if src.geofabrik_clip_to_boundary:
            from shapely.geometry import shape

            boundary = resolve_boundary(cfg.iso3, cfg.boundary)
            geometry_filter = shape(json.loads(boundary.geojson))

        build_cache(
            cfg,
            pbf_path,
            cache_root=country_root,
            snapshot=snapshot,
            geometry_filter=geometry_filter,
        )

        if not src.keep_pbf:
            try:
                pbf_path.unlink()
                logger.info("Removed PBF after cache build: %s", pbf_path)
            except OSError as exc:
                logger.warning("Could not remove PBF %s: %s", pbf_path, exc)

    def query_for(self, cfg: RootConfig, category: CategoryConfig) -> SourceQuery:
        if self._snapshot_dir is None:
            raise RuntimeError("OsmRunner.prepare must run before query_for")
        if not category.osm.enabled:
            raise CategorySkippedError(f"{category.name}: osm disabled for category")
        slug = theme_slug(category)
        parquet = self._snapshot_dir / f"{slug}.parquet"
        if not parquet.exists():
            raise CategorySkippedError(f"{category.name}: no parquet at {parquet}. Skipping.")

        snapshot_label = self._snapshot_label or "unknown"
        snapshot_date = self._snapshot_date or datetime.now(UTC)
        select_fields = _inject_local_name(list(category.osm.select), cfg.iso3)
        return SourceQuery(
            source_expr=f"read_parquet('{parquet}')",
            select_fields=select_fields,
            where_conditions=list(category.osm.where),
            bbox_cols="geom",
            dataset_source=self._dataset_source,
            source_url="https://www.openstreetmap.org/",
            source_description=(
                "OpenStreetMap is a community-edited geographic dataset of the world. "
                "Tag-based features (highway, building, amenity, ...) are extracted "
                "from the country PBF via quackosm."
            ),
            snapshot_date=snapshot_date,
            snapshot_label=snapshot_label,
            extra_readme_lines=[f"Cache slug: {slug}"],
        )

    @staticmethod
    def _infer_snapshot_date(snapshot_dir: Path, snapshot_label: str) -> datetime:
        manifest = snapshot_dir / "manifest.json"
        if manifest.exists():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                snap = payload.get("snapshot", snapshot_label)
                return datetime.fromisoformat(snap).replace(tzinfo=UTC)
            except (ValueError, KeyError, OSError) as exc:
                logger.warning("manifest.json at %s is malformed: %s", manifest, exc)
        try:
            return datetime.fromisoformat(snapshot_label).replace(tzinfo=UTC)
        except ValueError:
            return datetime.now(UTC)
