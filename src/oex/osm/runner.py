"""OSM source runner: one quackosm pass per country over every category's tags, after
which each category is a WHERE over the resulting country.parquet.
"""

import hashlib
import json
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import requests
from upath import UPath

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

from oex import http
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
from oex.osm.country_parquet import LiveSnapshot
from oex.osm.engines import ENGINE_NAMES
from oex.osm.errors import OsmEngineUnavailableError
from oex.osm.extract import osmium_polygon_extract
from oex.osm.fetch_planet import download_pbf
from oex.osm.geofabrik import GeofabrikExtract, GeofabrikUnavailableError, lookup_country
from oex.osm.postpass import boundary_area_sq_km, fetch_country_parquet
from oex.osm.rawdata import fetch_country_parquet as fetch_rawdata_parquet
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner
from oex.system import cpu_count, default_memory_limit_gb
from oex.timestamps import parse_utc

logger = get_logger(__name__)

_GEOFABRIK_DOWNLOAD_ATTEMPTS = 2
_GEOFABRIK_RETRY_BACKOFF_SECONDS = 5
_GEOFABRIK_FALLBACK_ERRORS = (
    GeofabrikUnavailableError,
    OsmEngineUnavailableError,
    requests.RequestException,
)


def _ensure_local_pbf(pbf_path: str, cache_dir: Path) -> Path:
    """`pbf_path` as a local file, downloaded when remote and reused while its size matches
    the remote object."""
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


def _row_group_size(memory_gb: int) -> int:
    """Parquet row group size, scaled to the memory budget: quackosm holds one row
    group per worker as an Arrow table, outside the DuckDB budget."""
    return 100_000 if memory_gb >= 16 else max(10_000, memory_gb * 5_000)


# quackosm's default drops source, ele, area and other tags the layers select.
_IGNORE_METADATA_TAGS = False


def _parquet_fingerprint(cfg: RootConfig, *, clip: bool) -> str:
    """Keyed on the boundary config and tag filters too, or changing either would reuse a stale
    parquet; the config is hashed rather than the geometry so reuse never needs the network."""
    boundary = cfg.boundary
    clip_key: object = "noclip"
    if clip:
        clip_key = {
            "geom": boundary.geom,
            "level": boundary.geoboundaries_level,
            "buffer": boundary.buffer_meters,
            "simplify": boundary.simplify_meters,
        }
    payload = json.dumps(
        {
            "clip": clip_key,
            "filter": union_tag_filter(cfg.categories),
            "ignore_metadata_tags": _IGNORE_METADATA_TAGS,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _inject_local_name(select_fields: list[str], iso3: str) -> list[str]:
    """Add `tags['name:<lang>'] AS name_<lang>` after `name_en` for each local language the
    category's own select does not already alias."""
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
    for language in languages:
        alias = f"name_{language}"
        if alias in existing:
            continue
        new_fields.insert(insert_at, f"tags['name:{language}'] AS {alias}")
        insert_at += 1
    return new_fields


def _planet_published_at(pbf_url: str) -> float:
    """The planet is republished weekly, so a copy kept on disk falls behind between runs."""
    head = http.session.head(pbf_url, allow_redirects=True, timeout=60)
    head.raise_for_status()
    if "Last-Modified" not in head.headers:
        raise RuntimeError(f"{pbf_url} sent no Last-Modified, so the local planet cannot be dated")
    return parsedate_to_datetime(head.headers["Last-Modified"]).timestamp()


def _drop_older_partial(planet_pbf: Path, published_at: float) -> None:
    """A partial download of an older planet would otherwise be resumed into the new one."""
    partial = planet_pbf.with_name(f"{planet_pbf.name}.part")
    if partial.is_file() and partial.stat().st_mtime < published_at:
        partial.unlink()


@contextmanager
def _download_lock(target: Path) -> Iterator[None]:
    """Parallel country exports share one planet file, so only one of them downloads it."""
    import fcntl  # POSIX only; imported here so the module still loads on Windows

    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / f"{target.name}.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _refuse_stale_planet(planet_pbf: Path, frequency: str) -> None:
    """Data older than the advertised frequency lands on HDX already past its due date."""
    from hdx.data.dataset import Dataset

    days = Dataset.transform_update_frequency(frequency)
    if days is None or int(days) <= 0:
        return
    age_days = (time.time() - planet_pbf.stat().st_mtime) / 86400
    if age_days > int(days):
        raise RuntimeError(
            f"{planet_pbf} is {age_days:.0f} days old, older than the {frequency} update "
            f"frequency; refresh the planet PBF before exporting"
        )


def _refreshes_planet(osm_config: OsmSourceConfig) -> bool:
    """A remote pbf_path is copied from its own source, so only a local planet is refreshed."""
    is_local = bool(osm_config.pbf_path) and UPath(osm_config.pbf_path).protocol in (
        "",
        "file",
        "local",
    )
    return osm_config.auto_download_planet and osm_config.snapshot in ("", "latest") and is_local


class OsmRunner(SourceRunner):
    name = "osm"

    def __init__(self) -> None:
        self._engine: str | None = None
        self._snapshot_dir: Path | None = None
        self._snapshot_label: str | None = None
        self._snapshot_date: datetime | None = None
        self._dataset_source: str = "OpenStreetMap"
        self._source_description: str = "Country features are extracted from the source PBF via quackosm with the union of all category tag filters; per-category exports apply tag predicates at query time."
        self._country_parquet: Path | None = None
        self._tags_present: bool | None = None

    def peek_snapshot_label(self, cfg: RootConfig) -> str | None:
        osm_config = cast(OsmSourceConfig, cfg.source["osm"])
        if not osm_config.enabled or not dataset_identity(cfg):
            return None
        engine = (osm_config.engine or "geofabrik").lower()
        if engine == "geofabrik":
            country_root = Path(osm_config.cache_dir) / "geofabrik" / cfg.iso3.lower()
            if not country_root.exists():
                return None
            try:
                extract = lookup_country(cfg.iso3, index_url=osm_config.geofabrik_index_url)
                return self._resolve_geofabrik_snapshot(osm_config.snapshot, extract.pbf_url)
            except _GEOFABRIK_FALLBACK_ERRORS as exc:
                logger.debug("Cannot peek the %s snapshot (%s); will run it", cfg.iso3, exc)
                return None
        if engine == "planet":
            if not osm_config.pbf_path:
                return None
            pbf = Path(osm_config.pbf_path)
            if not pbf.exists():
                return None
            if (
                _refreshes_planet(osm_config)
                and _planet_published_at(osm_config.pbf_url) > pbf.stat().st_mtime
            ):
                return None
            return self._planet_snapshot_label(pbf, osm_config.snapshot)
        return None

    def prepare(self, cfg: RootConfig) -> None:
        osm_config = cast(OsmSourceConfig, cfg.source["osm"])
        if not osm_config.enabled:
            raise RuntimeError("OSM source is disabled in config")

        engine = (osm_config.engine or "geofabrik").lower()
        fallback = (osm_config.fallback_engine or "").lower()
        try:
            self._prepare_engine(cfg, osm_config, engine)
        except (OsmEngineUnavailableError, requests.RequestException) as error:
            if not fallback:
                raise
            logger.warning(
                "osm.engine=%s failed (%s); falling back to %s for this run",
                engine,
                error,
                fallback,
            )
            try:
                self._prepare_engine(cfg, osm_config, fallback)
            except Exception as fallback_error:
                raise fallback_error from error

    def _prepare_engine(self, cfg: RootConfig, osm_config: OsmSourceConfig, engine: str) -> None:
        if engine == "geofabrik":
            try:
                self._prepare_geofabrik(cfg, osm_config)
            except _GEOFABRIK_FALLBACK_ERRORS as exc:
                if not osm_config.planet_fallback:
                    raise
                logger.warning(
                    "Geofabrik failed for %s (%s); falling back to planet engine",
                    cfg.iso3,
                    exc,
                )
                self._prepare_planet(cfg, osm_config)
        elif engine == "planet":
            self._prepare_planet(cfg, osm_config)
        elif engine == "postpass":
            self._prepare_postpass(cfg, osm_config)
        elif engine == "rawdata":
            self._prepare_rawdata(cfg, osm_config)
        else:
            raise ValueError(
                f"Unknown osm.engine={engine!r}; expected one of {sorted(ENGINE_NAMES)}"
            )

    def _prepare_live_engine(
        self,
        cfg: RootConfig,
        osm_config: OsmSourceConfig,
        *,
        engine: str,
        fetch: Callable[..., LiveSnapshot],
        endpoint: str,
        timeout: int,
        dataset_source: str,
        source_description: str,
    ) -> None:
        """Shared path for the live engines. Always refetches, since freshness is the reason to
        use one."""
        if not cfg.iso3:
            raise ValueError(f"osm.engine={engine} requires `iso3` in the config")

        boundary = resolve_boundary(cfg.iso3, cfg.boundary)
        self._check_area(engine, osm_config, boundary.geojson)
        union_filter = union_tag_filter(cfg.categories)
        snapshot_dir = Path(osm_config.cache_dir) / engine / cfg.iso3.lower()
        country_parquet = snapshot_dir / f"country-{_parquet_fingerprint(cfg, clip=True)}.parquet"

        logger.info("%s fetch for %s: %d filter keys", engine, cfg.iso3.upper(), len(union_filter))
        snapshot = fetch(
            boundary_geojson=boundary.geojson,
            tag_filter=union_filter,
            out_path=country_parquet,
            endpoint=endpoint,
            timeout=timeout,
        )

        self._adopt_snapshot(
            engine=engine,
            snapshot_dir=snapshot_dir,
            label=snapshot.label,
            date=snapshot.timestamp,
            dataset_source=dataset_source.format(label=snapshot.label),
            country_parquet=country_parquet,
        )
        self._source_description = source_description

    def _adopt_snapshot(
        self,
        *,
        engine: str,
        snapshot_dir: Path,
        label: str,
        date: datetime,
        dataset_source: str,
        country_parquet: Path,
    ) -> None:
        """Record the prepared snapshot that every category query reads."""
        self._engine = engine
        self._snapshot_dir = snapshot_dir
        self._snapshot_label = label
        self._snapshot_date = date
        self._dataset_source = dataset_source
        self._country_parquet = country_parquet

    @staticmethod
    def _check_area(engine: str, osm_config: OsmSourceConfig, boundary_geojson: str) -> None:
        """Postpass is a shared Geofabrik service and refuses large areas server-side."""
        if engine != "postpass":
            return
        area_sq_km = boundary_area_sq_km(boundary_geojson)
        if area_sq_km > osm_config.postpass_max_area_sq_km:
            raise ValueError(
                f"Boundary covers {area_sq_km:,.0f} km2, above the "
                f"{osm_config.postpass_max_area_sq_km:,.0f} km2 ceiling for osm.engine=postpass. "
                "Postpass is a shared Geofabrik service; use engine=geofabrik for an area "
                "this size, or raise osm.postpass_max_area_sq_km deliberately."
            )

    def _prepare_rawdata(self, cfg: RootConfig, osm_config: OsmSourceConfig) -> None:
        self._prepare_live_engine(
            cfg,
            osm_config,
            engine="rawdata",
            fetch=fetch_rawdata_parquet,
            endpoint=osm_config.rawdata_endpoint,
            timeout=osm_config.rawdata_timeout_s,
            dataset_source="OpenStreetMap (HOT Raw Data API {label})",
            source_description=(
                "Features are exported from the HOT Raw Data API, which tracks OpenStreetMap "
                "continuously, as one job per run; per-category exports apply tag predicates "
                "at query time."
            ),
        )

    def _prepare_postpass(self, cfg: RootConfig, osm_config: OsmSourceConfig) -> None:
        self._prepare_live_engine(
            cfg,
            osm_config,
            engine="postpass",
            fetch=fetch_country_parquet,
            endpoint=osm_config.postpass_endpoint,
            timeout=osm_config.postpass_timeout_s,
            dataset_source="OpenStreetMap (Postpass {label})",
            source_description=(
                "Features are queried from Geofabrik's Postpass database, which tracks "
                "OpenStreetMap continuously."
            ),
        )

    def _prepare_planet(self, cfg: RootConfig, osm_config: OsmSourceConfig) -> None:
        if not dataset_identity(cfg):
            raise ValueError(
                "osm.engine=planet requires `iso3`, or `output.s3.folder` when the "
                "export has no country code"
            )
        if not osm_config.pbf_path:
            raise ValueError(
                "osm.engine=planet requires source.osm.pbf_path "
                "(a local path or an s3:// URL to a planet PBF)"
            )
        planet_pbf = _ensure_local_pbf(osm_config.pbf_path, Path(osm_config.cache_dir))
        if not planet_pbf.is_file() and not osm_config.auto_download_planet:
            raise FileNotFoundError(
                f"Planet PBF not found at {planet_pbf}. "
                "Download it with `oex-cli osm-build-cache`, "
                "set source.osm.auto_download_planet=true, "
                "or pass --download-if-missing on the CLI."
            )
        if osm_config.auto_download_planet:
            planet_pbf = _refresh_planet_pbf(osm_config, planet_pbf)

        if not osm_config.snapshot or osm_config.snapshot == "latest":
            _refuse_stale_planet(planet_pbf, cfg.frequency)
        snapshot_label = self._planet_snapshot_label(planet_pbf, osm_config.snapshot)
        snapshot_dir = (
            Path(osm_config.cache_dir) / "planet" / dataset_identity(cfg) / snapshot_label
        )
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        clip = osm_config.planet_clip_to_boundary
        country_pbf = snapshot_dir / "country.osm.pbf"
        country_parquet = snapshot_dir / f"country-{_parquet_fingerprint(cfg, clip=clip)}.parquet"

        if not country_parquet.exists():
            if clip:
                source_pbf = _clip_planet(cfg, planet_pbf, country_pbf)
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

        if clip and not osm_config.keep_pbf and country_pbf.exists():
            _remove_pbf(country_pbf)

        self._adopt_snapshot(
            engine="planet",
            snapshot_dir=snapshot_dir,
            label=snapshot_label,
            date=self._infer_snapshot_date(snapshot_label),
            dataset_source=f"OpenStreetMap planet ({snapshot_label})",
            country_parquet=country_parquet,
        )
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
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

        logger.info(
            "Building country.parquet from %s (union of %d category filters: %s)",
            country_pbf.name,
            len(union_filter),
            sorted(union_filter.keys()),
        )
        # quackosm's own DuckDB sizes itself from the host unless told otherwise.
        threads = cpu_count()
        memory_gb = cfg.parallel.memory_gb or default_memory_limit_gb()
        convert_pbf_to_parquet(
            pbf_path=country_pbf,
            tags_filter=union_filter,
            geometry_filter=geometry_filter,
            result_file_path=country_parquet,
            keep_all_tags=True,
            ignore_metadata_tags=_IGNORE_METADATA_TAGS,
            sort_result=True,
            compression="zstd",
            compression_level=3,
            row_group_size=_row_group_size(memory_gb),
            working_directory=work_dir,
            ignore_cache=True,
            verbosity_mode="silent",
            cpu_limit=threads,
            duckdb_conn_kwargs={
                "config_kwargs": {"memory_limit": f"{memory_gb}GB", "threads": threads}
            },
        )
        _write_manifest(cfg, snapshot_dir, engine, country_parquet, sorted(union_filter.keys()))
        shutil.rmtree(work_dir)

    @staticmethod
    def _planet_snapshot_label(planet_pbf: Path, requested: str) -> str:
        """Snapshot label for planet engine: explicit override, else PBF mtime ISO date."""
        if requested and requested != "latest":
            return requested
        modified_at = datetime.fromtimestamp(planet_pbf.stat().st_mtime, tz=UTC)
        return modified_at.date().isoformat()

    def _prepare_geofabrik(self, cfg: RootConfig, osm_config: OsmSourceConfig) -> None:
        if not cfg.iso3:
            raise ValueError("osm.engine=geofabrik requires `iso3` in the config")

        country_root = Path(osm_config.cache_dir) / "geofabrik" / cfg.iso3.lower()
        country_root.mkdir(parents=True, exist_ok=True)

        extract = lookup_country(cfg.iso3, index_url=osm_config.geofabrik_index_url)
        snapshot = self._resolve_geofabrik_snapshot(osm_config.snapshot, extract.pbf_url)
        snapshot_dir = country_root / snapshot
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = _parquet_fingerprint(cfg, clip=osm_config.geofabrik_clip_to_boundary)
        country_parquet = snapshot_dir / f"country-{fingerprint}.parquet"

        if not country_parquet.exists():
            pbf_path = self._geofabrik_pbf(cfg, extract, country_root / "_pbf")
            geometry_filter = None
            if osm_config.geofabrik_clip_to_boundary:
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
            if not osm_config.keep_pbf:
                _remove_pbf(pbf_path)
        else:
            logger.info("Reusing existing country parquet %s", country_parquet)

        self._adopt_snapshot(
            engine="geofabrik",
            snapshot_dir=snapshot_dir,
            label=snapshot,
            date=self._infer_snapshot_date(snapshot),
            dataset_source=f"OpenStreetMap (Geofabrik {cfg.iso3.upper()} {snapshot})",
            country_parquet=country_parquet,
        )
        logger.info(
            "OSM source: geofabrik %s, snapshot=%s, parquet=%s",
            cfg.iso3.upper(),
            snapshot,
            country_parquet,
        )

    def _geofabrik_pbf(self, cfg: RootConfig, extract: GeofabrikExtract, pbf_dir: Path) -> Path:
        """The country's Geofabrik PBF, downloaded unless an earlier run left it."""
        pbf_path = pbf_dir / f"{extract.geofabrik_id}-latest.osm.pbf"
        if pbf_path.exists():
            logger.info("Reusing already-downloaded PBF: %s", pbf_path)
            return pbf_path
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
        return result.path

    @staticmethod
    def _download_geofabrik_with_retry(
        url: str,
        dest_dir: Path,
        *,
        md5_url: str | None,
        filename: str,
    ):  # noqa: ANN202 - returns whatever download_pbf returns
        last_error: requests.RequestException | None = None
        for attempt in range(1, _GEOFABRIK_DOWNLOAD_ATTEMPTS + 1):
            try:
                return download_pbf(url, dest_dir, md5_url=md5_url, filename=filename)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < _GEOFABRIK_DOWNLOAD_ATTEMPTS:
                    logger.warning(
                        "Geofabrik download attempt %d/%d failed (%s); retrying in %ds",
                        attempt,
                        _GEOFABRIK_DOWNLOAD_ATTEMPTS,
                        exc,
                        _GEOFABRIK_RETRY_BACKOFF_SECONDS,
                    )
                    time.sleep(_GEOFABRIK_RETRY_BACKOFF_SECONDS)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _resolve_geofabrik_snapshot(requested: str, pbf_url: str) -> str:
        """The date Geofabrik last rebuilt the extract, so `latest` tracks upstream."""
        if requested and requested != "latest":
            return requested
        head = http.session.head(pbf_url, allow_redirects=True, timeout=60)
        head.raise_for_status()
        last_modified = head.headers.get("Last-Modified")
        if not last_modified:
            raise OsmEngineUnavailableError(f"Geofabrik sent no Last-Modified for {pbf_url}")
        return parsedate_to_datetime(last_modified).astimezone(UTC).date().isoformat()

    def _country_source_expr(self) -> str:
        """An empty extract makes quackosm omit the tags column that category selects reference,
        so one is injected when missing."""
        parquet = str(self._country_parquet)
        if self._tags_present is None:
            import duckdb

            conn = duckdb.connect()
            try:
                conn.execute(f"SELECT * FROM read_parquet('{parquet}') LIMIT 0")
                self._tags_present = any(column[0] == "tags" for column in conn.description)
            finally:
                conn.close()
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
            bbox_mode="geom",
            dataset_source=self._dataset_source,
            source_url="https://www.openstreetmap.org/",
            source_description=(
                "OpenStreetMap is a community-edited geographic dataset of the world. "
                f"{self._source_description}"
            ),
            snapshot_date=snapshot_date,
            snapshot_label=snapshot_label,
            extra_readme_lines=[f"Engine: {engine_label}"],
        )

    @staticmethod
    def _infer_snapshot_date(snapshot_label: str) -> datetime:
        try:
            return parse_utc(snapshot_label)
        except ValueError as exc:
            raise ValueError(
                f"OSM snapshot label {snapshot_label!r} is not an ISO date; set source.osm.snapshot "
                "to a date such as 2026-05-01, or to latest"
            ) from exc


def _refresh_planet_pbf(osm_config: OsmSourceConfig, planet_pbf: Path) -> Path:
    """Download the planet when it is missing or older than pbf_url's, under a lock so
    concurrent runs share one download."""
    with _download_lock(planet_pbf):
        published_at = (
            _planet_published_at(osm_config.pbf_url) if _refreshes_planet(osm_config) else None
        )
        outdated = (
            published_at is not None
            and planet_pbf.is_file()
            and published_at > planet_pbf.stat().st_mtime
        )
        if planet_pbf.is_file() and not outdated:
            return planet_pbf
        if published_at is not None:
            _drop_older_partial(planet_pbf, published_at)
        logger.warning(
            "Planet PBF at %s is missing or older than %s; downloading it",
            planet_pbf,
            osm_config.pbf_url,
        )
        result = download_pbf(
            osm_config.pbf_url,
            planet_pbf.parent,
            md5_url=osm_config.md5_url,
            filename=planet_pbf.name,
        )
        return result.path


def _clip_planet(cfg: RootConfig, planet_pbf: Path, country_pbf: Path) -> Path:
    """The planet clipped to the boundary, reusing a clip an earlier run left."""
    if country_pbf.exists():
        logger.info("Reusing existing country PBF %s", country_pbf)
        return country_pbf
    boundary = resolve_boundary(cfg.iso3, cfg.boundary)
    osmium_polygon_extract(planet_pbf, json.loads(boundary.geojson), country_pbf)
    return country_pbf


def _remove_pbf(pbf_path: Path) -> None:
    # The parquet is already built, so a PBF left on disk costs space but not correctness.
    try:
        pbf_path.unlink()
        logger.info("Removed PBF after parquet build: %s", pbf_path)
    except OSError as exc:
        logger.warning("Could not remove PBF %s: %s", pbf_path, exc)


def _write_manifest(
    cfg: RootConfig,
    snapshot_dir: Path,
    engine: str,
    country_parquet: Path,
    filter_keys: list[str],
) -> None:
    manifest = {
        "snapshot": snapshot_dir.name,
        "iso3": cfg.iso3.upper(),
        "engine": engine,
        "country_parquet": country_parquet.name,
        "filter_keys": filter_keys,
    }
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
