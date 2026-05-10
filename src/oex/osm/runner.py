"""OSM source runner.

Two engines supported:

- ``geofabrik``: download per-country PBF from Geofabrik, build per-theme parquets
  via quackosm with one tags_filter per category. Output cache layout:
  ``<cache>/geofabrik/<iso3>/<snapshot>/<theme>.parquet``.

- ``planet``: clip a country PBF out of a local planet PBF via osmium-tool,
  then run quackosm once with the union of all category tag filters
  (``keep_all_tags=True``). Per-category extraction is a tag-predicate WHERE
  at query time (no per-category PBF reparse, no spatial test). Output:
  ``<cache>/planet/<iso3>/<snapshot>/country.parquet``.

The two cache layouts coexist; ``query_for`` dispatches on the active engine.
"""

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
from oex.osm.category_filter import category_where_predicate, union_tag_filter
from oex.osm.extract import osmium_polygon_extract
from oex.osm.fetch_planet import download_pbf
from oex.osm.geofabrik import GeofabrikUnavailableError, lookup_country
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
        self._engine: str | None = None
        self._snapshot_dir: Path | None = None
        self._snapshot_label: str | None = None
        self._snapshot_date: datetime | None = None
        self._dataset_source: str = "OpenStreetMap"
        self._country_parquet: Path | None = None

    def prepare(self, cfg: RootConfig) -> None:
        src = cast(OsmSourceConfig, cfg.source["osm"])
        if not src.enabled:
            raise RuntimeError("OSM source is disabled in config")

        engine = (src.engine or "geofabrik").lower()
        if engine == "geofabrik":
            try:
                self._prepare_geofabrik(cfg, src)
            except GeofabrikUnavailableError as exc:
                if not src.planet_fallback:
                    raise
                logger.warning(
                    "Geofabrik unavailable for %s (%s); falling back to planet engine",
                    cfg.iso3,
                    exc,
                )
                self._prepare_planet(cfg, src)
        elif engine == "planet":
            self._prepare_planet(cfg, src)
        else:
            raise ValueError(f"Unknown osm.engine={engine!r}; expected 'geofabrik' or 'planet'")

    def _prepare_planet(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        if not cfg.iso3:
            raise ValueError("osm.engine=planet requires `iso3` in the config")
        if not src.pbf_path:
            raise ValueError(
                "osm.engine=planet requires source.osm.pbf_path "
                "(absolute path to a local planet PBF)"
            )
        planet_pbf = Path(src.pbf_path)
        if not planet_pbf.is_file():
            if not src.auto_download_planet:
                raise FileNotFoundError(
                    f"Planet PBF not found at {planet_pbf}. "
                    "Download it with `oex-cli osm-build-cache`, "
                    "set source.osm.auto_download_planet=true, "
                    "or pass --download-if-missing on the CLI."
                )
            logger.warning(
                "Planet PBF missing at %s; auto_download_planet is on, downloading from %s",
                planet_pbf,
                src.pbf_url,
            )
            result = download_pbf(
                src.pbf_url,
                planet_pbf.parent,
                md5_url=src.md5_url,
                filename=planet_pbf.name,
            )
            planet_pbf = result.path

        snapshot_label = self._planet_snapshot_label(planet_pbf, src.snapshot)
        country_root = Path(src.cache_dir) / "planet" / cfg.iso3.lower()
        snapshot_dir = country_root / snapshot_label
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        country_pbf = snapshot_dir / "country.osm.pbf"
        country_parquet = snapshot_dir / "country.parquet"

        if not country_parquet.exists():
            if not country_pbf.exists():
                boundary = resolve_boundary(cfg.iso3, cfg.boundary)
                osmium_polygon_extract(planet_pbf, json.loads(boundary.geojson), country_pbf)
            else:
                logger.info("Reusing existing country PBF %s", country_pbf)
            self._build_country_parquet(cfg, country_pbf, country_parquet, snapshot_dir)
        else:
            logger.info("Reusing existing country parquet %s", country_parquet)

        if not src.keep_pbf and country_pbf.exists():
            try:
                country_pbf.unlink()
            except OSError as exc:
                logger.warning("Could not remove %s: %s", country_pbf, exc)

        self._engine = "planet"
        self._snapshot_dir = snapshot_dir
        self._snapshot_label = snapshot_label
        self._snapshot_date = self._infer_snapshot_date(snapshot_dir, snapshot_label)
        self._dataset_source = f"OpenStreetMap planet ({snapshot_label})"
        self._country_parquet = country_parquet
        logger.info("OSM source: planet, snapshot=%s, parquet=%s", snapshot_label, country_parquet)

    def _build_country_parquet(
        self,
        cfg: RootConfig,
        country_pbf: Path,
        country_parquet: Path,
        snapshot_dir: Path,
    ) -> None:
        from quackosm.functions import convert_pbf_to_parquet

        union_filter = union_tag_filter(cfg.categories)
        if not union_filter:
            raise ValueError("planet engine requires at least one enabled category with osm.filter")

        work_dir = snapshot_dir / "_qosm_work"
        if work_dir.exists():
            import shutil

            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

        logger.info(
            "Building country.parquet from %s (union of %d category filters: %s)",
            country_pbf.name,
            len(union_filter),
            sorted(union_filter.keys()),
        )
        convert_pbf_to_parquet(
            pbf_path=country_pbf,
            tags_filter=union_filter,
            geometry_filter=None,
            result_file_path=country_parquet,
            keep_all_tags=True,
            sort_result=True,
            compression="zstd",
            compression_level=3,
            row_group_size=100_000,
            working_directory=work_dir,
            ignore_cache=True,
            verbosity_mode="silent",
        )
        manifest = {
            "snapshot": snapshot_dir.name,
            "iso3": cfg.iso3.upper(),
            "engine": "planet",
            "country_parquet": country_parquet.name,
            "filter_keys": sorted(union_filter.keys()),
        }
        (snapshot_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _planet_snapshot_label(planet_pbf: Path, requested: str) -> str:
        """Snapshot label for planet engine: explicit override, else PBF mtime ISO date."""
        if requested and requested != "latest":
            return requested
        ts = datetime.fromtimestamp(planet_pbf.stat().st_mtime, tz=UTC)
        return ts.date().isoformat()

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

        self._engine = "geofabrik"
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

        snapshot_label = self._snapshot_label or "unknown"
        snapshot_date = self._snapshot_date or datetime.now(UTC)
        select_fields = _inject_local_name(list(category.osm.select), cfg.iso3)

        if self._engine == "planet":
            return self._planet_query(category, select_fields, snapshot_label, snapshot_date)
        return self._geofabrik_query(category, select_fields, snapshot_label, snapshot_date)

    def _planet_query(
        self,
        category: CategoryConfig,
        select_fields: list[str],
        snapshot_label: str,
        snapshot_date: datetime,
    ) -> SourceQuery:
        if self._country_parquet is None or not self._country_parquet.exists():
            raise CategorySkippedError(
                f"{category.name}: planet country parquet missing at {self._country_parquet}"
            )
        tag_predicate = category_where_predicate(category)
        where = list(category.osm.where)
        if tag_predicate != "TRUE":
            where.append(tag_predicate)
        return SourceQuery(
            source_expr=f"read_parquet('{self._country_parquet}')",
            select_fields=select_fields,
            where_conditions=where,
            bbox_cols="geom",
            dataset_source=self._dataset_source,
            source_url="https://www.openstreetmap.org/",
            source_description=(
                "OpenStreetMap is a community-edited geographic dataset of the world. "
                "Country features extracted from a local planet PBF via osmium-tool, "
                "then converted to GeoParquet by quackosm with the union of all category filters."
            ),
            snapshot_date=snapshot_date,
            snapshot_label=snapshot_label,
            extra_readme_lines=["Engine: planet (osmium polygon extract + quackosm)"],
        )

    def _geofabrik_query(
        self,
        category: CategoryConfig,
        select_fields: list[str],
        snapshot_label: str,
        snapshot_date: datetime,
    ) -> SourceQuery:
        slug = theme_slug(category)
        assert self._snapshot_dir is not None
        parquet = self._snapshot_dir / f"{slug}.parquet"
        if not parquet.exists():
            raise CategorySkippedError(f"{category.name}: no parquet at {parquet}. Skipping.")
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
