"""OSM source runner.

Two engines, one unified pipeline. Both produce a single
``country.parquet`` per (iso3, snapshot) by running quackosm once with the
union of all category tag filters and ``keep_all_tags=True``. Per-category
extraction is a tag-predicate ``WHERE`` at query time, no per-category PBF
reparse.

- ``geofabrik``: download per-country PBF from Geofabrik, then build the
  country parquet. Cache:
  ``<cache>/geofabrik/<iso3>/<snapshot>/country-<fingerprint>.parquet``.

- ``planet``: clip a country PBF out of a local planet PBF via osmium-tool,
  then build the country parquet. Cache:
  ``<cache>/planet/<iso3>/<snapshot>/country-<fingerprint>.parquet``.

The fingerprint covers the boundary and the category tag filters, so changing
either builds a fresh parquet instead of silently reusing one that was clipped
to a different area or filtered to different tags.
"""

import hashlib
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import requests
from upath import UPath

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

from oex.boundary import resolve_boundary
from oex.config.schema import (
    CategoryConfig,
    OsmSourceConfig,
    RootConfig,
    dataset_identity,
)
from oex.locale import local_osm_languages
from oex.logging_setup import get_logger
from oex.osm.category_filter import category_where_predicate, union_tag_filter
from oex.osm.extract import osmium_polygon_extract
from oex.osm.fetch_planet import download_pbf
from oex.osm.geofabrik import GeofabrikUnavailableError, lookup_country
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner

logger = get_logger(__name__)

_GEOFABRIK_DOWNLOAD_ATTEMPTS = 2
_GEOFABRIK_RETRY_BACKOFF_SECONDS = 5
_GeofabrikFallbackError = (GeofabrikUnavailableError, requests.RequestException)


def _ensure_local_pbf(pbf_path: str, cache_dir: Path) -> Path:
    """Return a local PBF, downloading it first when the path is remote (s3://, ...).

    A cached download is reused when its size matches the remote object.
    """
    remote = UPath(pbf_path)
    if remote.protocol in ("", "file", "local"):
        return Path(pbf_path)
    local = cache_dir / "_pbf" / remote.name
    if local.is_file() and local.stat().st_size == remote.stat().st_size:
        logger.info("Using cached PBF %s", local)
        return local
    local.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading PBF %s -> %s", pbf_path, local)
    with remote.open("rb") as reader, local.open("wb") as writer:
        shutil.copyfileobj(reader, writer)
    return local


def _parquet_fingerprint(cfg: RootConfig, *, clip: bool) -> str:
    """Identify the parquet by everything that shapes its contents.

    The cached parquet is clipped to the boundary and holds only the union of the
    categories' tag filters. Keying on (iso3, snapshot) alone would reuse it after
    either changes, silently dropping features the new config asks for.

    The boundary *config* is hashed rather than the resolved geometry, so deciding
    whether a local parquet can be reused never has to hit the network.
    """
    boundary = cfg.boundary
    clip_key: object = "noclip"
    if clip:
        clip_key = {
            "geom": boundary.geom,
            "release": boundary.geoboundaries_release,
            "level": boundary.geoboundaries_level,
            "buffer": boundary.buffer_meters,
        }
    payload = json.dumps(
        {"clip": clip_key, "filter": union_tag_filter(cfg.categories)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


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
        self._tags_present: bool | None = None

    def peek_snapshot_label(self, cfg: RootConfig) -> str | None:
        src = cast(OsmSourceConfig, cfg.source["osm"])
        if not src.enabled or not dataset_identity(cfg):
            return None
        engine = (src.engine or "geofabrik").lower()
        if engine == "geofabrik":
            country_root = Path(src.cache_dir) / "geofabrik" / cfg.iso3.lower()
            if not country_root.exists():
                return None
            return self._resolve_or_create_snapshot(country_root, src.snapshot)
        if engine == "planet":
            if not src.pbf_path:
                return None
            pbf = Path(src.pbf_path)
            if not pbf.exists():
                return None
            return self._planet_snapshot_label(pbf, src.snapshot)
        return None

    def prepare(self, cfg: RootConfig) -> None:
        src = cast(OsmSourceConfig, cfg.source["osm"])
        if not src.enabled:
            raise RuntimeError("OSM source is disabled in config")

        engine = (src.engine or "geofabrik").lower()
        if engine == "geofabrik":
            try:
                self._prepare_geofabrik(cfg, src)
            except _GeofabrikFallbackError as exc:
                if not src.planet_fallback:
                    raise
                logger.warning(
                    "Geofabrik failed for %s (%s); falling back to planet engine",
                    cfg.iso3,
                    exc,
                )
                self._prepare_planet(cfg, src)
        elif engine == "planet":
            self._prepare_planet(cfg, src)
        else:
            raise ValueError(f"Unknown osm.engine={engine!r}; expected 'geofabrik' or 'planet'")

    def _prepare_planet(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        if not dataset_identity(cfg):
            raise ValueError(
                "osm.engine=planet requires `iso3`, or `output.s3.folder` when the "
                "export has no country code"
            )
        if not src.pbf_path:
            raise ValueError(
                "osm.engine=planet requires source.osm.pbf_path "
                "(a local path or an s3:// URL to a planet PBF)"
            )
        planet_pbf = _ensure_local_pbf(src.pbf_path, Path(src.cache_dir))
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
        country_root = Path(src.cache_dir) / "planet" / dataset_identity(cfg)
        snapshot_dir = country_root / snapshot_label
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        clip = src.planet_clip_to_boundary
        country_pbf = snapshot_dir / "country.osm.pbf"
        country_parquet = snapshot_dir / f"country-{_parquet_fingerprint(cfg, clip=clip)}.parquet"

        if not country_parquet.exists():
            if clip:
                if not country_pbf.exists():
                    boundary = resolve_boundary(cfg.iso3, cfg.boundary)
                    osmium_polygon_extract(planet_pbf, json.loads(boundary.geojson), country_pbf)
                else:
                    logger.info("Reusing existing country PBF %s", country_pbf)
                source_pbf = country_pbf
            else:
                logger.info(
                    "planet_clip_to_boundary=false; building parquet from the whole planet "
                    "%s (no clip)",
                    planet_pbf,
                )
                source_pbf = planet_pbf
            self._build_country_parquet(
                cfg, source_pbf, country_parquet, snapshot_dir, engine="planet"
            )
        else:
            logger.info("Reusing existing country parquet %s", country_parquet)

        if clip and not src.keep_pbf and country_pbf.exists():
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
        *,
        engine: str,
        geometry_filter: "BaseGeometry | None" = None,
    ) -> None:
        from quackosm.functions import convert_pbf_to_parquet

        union_filter = union_tag_filter(cfg.categories)
        if not union_filter:
            raise ValueError(
                f"{engine} engine requires at least one enabled category with osm.filter"
            )

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
            geometry_filter=geometry_filter,
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
            "engine": engine,
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
        fingerprint = _parquet_fingerprint(cfg, clip=src.geofabrik_clip_to_boundary)
        country_parquet = snapshot_dir / f"country-{fingerprint}.parquet"

        if not country_parquet.exists():
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
                result = self._download_geofabrik_with_retry(
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

            self._build_country_parquet(
                cfg,
                pbf_path,
                country_parquet,
                snapshot_dir,
                engine="geofabrik",
                geometry_filter=geometry_filter,
            )

            if not src.keep_pbf:
                try:
                    pbf_path.unlink()
                    logger.info("Removed PBF after parquet build: %s", pbf_path)
                except OSError as exc:
                    logger.warning("Could not remove PBF %s: %s", pbf_path, exc)
        else:
            logger.info("Reusing existing country parquet %s", country_parquet)

        self._engine = "geofabrik"
        self._snapshot_dir = snapshot_dir
        self._snapshot_label = snapshot
        self._snapshot_date = self._infer_snapshot_date(snapshot_dir, snapshot)
        self._dataset_source = f"OpenStreetMap (Geofabrik {cfg.iso3.upper()} {snapshot})"
        self._country_parquet = country_parquet
        logger.info(
            "OSM source: geofabrik %s, snapshot=%s, parquet=%s",
            cfg.iso3.upper(),
            snapshot,
            country_parquet,
        )

    @staticmethod
    def _download_geofabrik_with_retry(
        url: str,
        dest_dir: Path,
        *,
        md5_url: str | None,
        filename: str,
    ):  # noqa: ANN202 - returns whatever download_pbf returns
        last_exc: requests.RequestException | None = None
        for attempt in range(1, _GEOFABRIK_DOWNLOAD_ATTEMPTS + 1):
            try:
                return download_pbf(url, dest_dir, md5_url=md5_url, filename=filename)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < _GEOFABRIK_DOWNLOAD_ATTEMPTS:
                    logger.warning(
                        "Geofabrik download attempt %d/%d failed (%s); retrying in %ds",
                        attempt,
                        _GEOFABRIK_DOWNLOAD_ATTEMPTS,
                        exc,
                        _GEOFABRIK_RETRY_BACKOFF_SECONDS,
                    )
                    time.sleep(_GEOFABRIK_RETRY_BACKOFF_SECONDS)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _resolve_or_create_snapshot(country_root: Path, requested: str) -> str:
        if requested and requested != "latest":
            return requested
        existing = sorted(p.name for p in country_root.iterdir() if p.is_dir() and p.name != "_pbf")
        if existing:
            return existing[-1]
        return datetime.now(UTC).date().isoformat()

    def _country_source_expr(self) -> str:
        """Read expression for the country parquet.

        An empty extract makes quackosm omit the tags column that category
        selects reference, so inject an empty one when it is missing.
        """
        parquet = str(self._country_parquet)
        if self._tags_present is None:
            import duckdb

            con = duckdb.connect()
            try:
                con.execute(f"SELECT * FROM read_parquet('{parquet}') LIMIT 0")
                self._tags_present = any(col[0] == "tags" for col in con.description)
            finally:
                con.close()
        if self._tags_present:
            return f"read_parquet('{parquet}')"
        return (
            "(SELECT *, CAST(NULL AS MAP(VARCHAR, VARCHAR)) AS tags "
            f"FROM read_parquet('{parquet}')) AS src"
        )

    def query_for(self, cfg: RootConfig, category: CategoryConfig) -> SourceQuery:
        if self._country_parquet is None or not self._country_parquet.exists():
            raise CategorySkippedError(
                f"{category.name}: country parquet missing at {self._country_parquet}"
            )
        if not category.osm.enabled:
            raise CategorySkippedError(f"{category.name}: osm disabled for category")

        snapshot_label = self._snapshot_label or "unknown"
        snapshot_date = self._snapshot_date or datetime.now(UTC)
        select_fields = _inject_local_name(list(category.osm.select), cfg.iso3)

        tag_predicate = category_where_predicate(category)
        where = list(category.osm.where)
        if tag_predicate != "TRUE":
            where.append(tag_predicate)

        engine_label = self._engine or "osm"
        return SourceQuery(
            source_expr=self._country_source_expr(),
            select_fields=select_fields,
            where_conditions=where,
            bbox_cols="geom",
            dataset_source=self._dataset_source,
            source_url="https://www.openstreetmap.org/",
            source_description=(
                "OpenStreetMap is a community-edited geographic dataset of the world. "
                "Country features are extracted from the source PBF via quackosm with "
                "the union of all category tag filters; per-category exports apply "
                "tag predicates at query time."
            ),
            snapshot_date=snapshot_date,
            snapshot_label=snapshot_label,
            extra_readme_lines=[f"Engine: {engine_label}"],
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
