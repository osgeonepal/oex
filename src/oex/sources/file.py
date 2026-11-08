"""A user-supplied spatial file read through DuckDB's ST_Read, reprojected to OGC:CRS84
when it declares another CRS, since oex clips and filters in lon/lat.
"""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import requests

from oex.config.schema import CategoryConfig, FileSourceConfig, RootConfig
from oex.logging_setup import get_logger
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner
from oex.sql import sql_identifier, sql_string
from oex.timestamps import parse_utc

logger = get_logger(__name__)

# What ST_Read_Meta reports for data already in lon/lat, in which case no transform runs.
_LONLAT_CRS = {"OGC:CRS84", "EPSG:4326"}


class FileSourceError(RuntimeError):
    """The configured file cannot be used as a source."""


def layer_names(conn: duckdb.DuckDBPyConnection, path: str) -> list[str]:
    row = conn.execute(
        f"SELECT [l.name for l in layers] FROM ST_Read_Meta({sql_string(path)})"
    ).fetchone()
    return list(row[0]) if row and row[0] else []


def resolve_layer(available: list[str], configured: str) -> str:
    """Which layer to read. ST_Read silently takes the first, which would drop the rest."""
    if configured:
        if available and configured not in available:
            raise FileSourceError(
                f"layer {configured!r} is not in the file; it holds {sorted(available)}"
            )
        return configured
    if len(available) > 1:
        raise FileSourceError(
            f"the file holds {len(available)} layers {sorted(available)}; "
            "set `layer` on the category to say which one to export"
        )
    return ""


def read_expr(path: str, source_crs: str, layer: str = "") -> str:
    """Exactly one `geometry` column in lon/lat; the file's own geometry column is dropped so
    a second one cannot reach the writer."""
    layer_arg = f", layer={sql_string(layer)}" if layer else ""
    return (
        f"(SELECT * EXCLUDE (geom), {geometry_expr(source_crs)} AS geometry "
        f"FROM ST_Read({sql_string(path)}{layer_arg}))"
    )


def declared_crs(conn: duckdb.DuckDBPyConnection, path: str) -> str | None:
    """`AUTH:CODE` the file declares, or None when it carries no CRS at all."""
    row = conn.execute(
        f"SELECT layers[1].geometry_fields[1].crs FROM ST_Read_Meta({sql_string(path)})"
    ).fetchone()
    crs = row[0] if row else None
    if not crs or not crs.get("auth_name") or not crs.get("auth_code"):
        return None
    return f"{crs['auth_name']}:{crs['auth_code']}"


def resolve_crs(declared: str | None, configured: str) -> str:
    """A file with no CRS is ambiguous, and assuming lon/lat would misplace the data, so the
    config has to say."""
    if configured:
        return configured
    if declared:
        return declared
    raise FileSourceError(
        "the file declares no CRS; set `crs` on the category (or source.file.crs) "
        "to say what its coordinates are in"
    )


def geometry_expr(source_crs: str) -> str:
    """`always_xy` is required: ST_Transform otherwise honours authority axis order, so a
    latitude-first CRS such as EPSG:4258 would come out swapped."""
    if source_crs.upper() in _LONLAT_CRS:
        return "geom"
    return f"ST_Transform(geom, {sql_string(source_crs)}, 'OGC:CRS84', always_xy := true)"


def file_timestamp(path: str) -> datetime:
    """When the data was last written, which becomes the HDX time period."""
    parsed = urlparse(path)
    if parsed.scheme in ("http", "https"):
        response = requests.head(path, timeout=60, allow_redirects=True)
        if response.status_code != 200:
            raise FileSourceError(f"HEAD {path} returned HTTP {response.status_code}")
        last_modified = response.headers.get("Last-Modified")
        if not last_modified:
            raise FileSourceError(
                f"{path} sends no Last-Modified header; set source.file.snapshot explicitly"
            )
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(last_modified).astimezone(UTC)
    if parsed.scheme == "s3":
        raise FileSourceError(
            f"oex does not read timestamps from S3 objects; set source.file.snapshot "
            f"explicitly for {path}"
        )
    local = Path(path)
    if not local.exists():
        raise FileSourceError(f"{path} does not exist")
    return datetime.fromtimestamp(local.stat().st_mtime, tz=UTC)


def category_path(category: CategoryConfig, file_config: FileSourceConfig) -> str:
    path = category.file.path or file_config.path
    if not path:
        raise FileSourceError(
            f"category {category.name!r} has no `file.path` and source.file.path is empty"
        )
    return path


class FileRunner(SourceRunner):
    name = "file"

    def __init__(self) -> None:
        self._snapshot_date: datetime | None = None
        self._snapshot_label = ""
        self._crs_by_path: dict[str, str] = {}
        self._layer_by_path: dict[str, str] = {}

    def prepare(self, cfg: RootConfig) -> None:
        file_config = _file_config(cfg)
        if not file_config.enabled:
            raise FileSourceError("source.file.enabled is false")
        paths = sorted(
            {
                category_path(category, file_config)
                for category in cfg.categories
                if category.file.enabled and (category.file.path or file_config.path)
            }
        )
        if not paths:
            raise FileSourceError("no category has source `file` enabled with a path")

        conn = duckdb.connect()
        conn.execute("INSTALL spatial; LOAD spatial;")
        if any(urlparse(path).scheme in ("http", "https", "s3") for path in paths):
            conn.execute("INSTALL httpfs; LOAD httpfs;")

        for path in paths:
            configured_crs = _first_category_setting(
                cfg, file_config, path, "crs", default=file_config.crs
            )
            self._crs_by_path[path] = resolve_crs(declared_crs(conn, path), configured_crs)
            layer = _first_category_setting(cfg, file_config, path, "layer", default="")
            self._layer_by_path[path] = resolve_layer(layer_names(conn, path), layer)
            logger.info(
                "file source %s: reading %sas %s",
                path,
                f"layer {self._layer_by_path[path]!r} " if self._layer_by_path[path] else "",
                self._crs_by_path[path],
            )
        conn.close()

        if file_config.snapshot:
            self._snapshot_date = parse_utc(file_config.snapshot)
        else:
            self._snapshot_date = max(file_timestamp(path) for path in paths)
        self._snapshot_label = self._snapshot_date.strftime("%Y-%m-%d")

    def query_for(self, cfg: RootConfig, category: CategoryConfig) -> SourceQuery:
        if not category.file.enabled:
            raise CategorySkippedError(f"{category.name}: file source disabled")
        file_config = _file_config(cfg)
        path = category_path(category, file_config)
        if not category.file.select:
            raise FileSourceError(
                f"category {category.name!r} needs `file.select` mapping output columns "
                "to columns in the file"
            )
        if self._snapshot_date is None:
            raise FileSourceError("prepare() must run before query_for()")

        select_fields = [
            f"{sql_identifier(source_column)} AS {sql_identifier(output_column)}"
            for output_column, source_column in category.file.select.items()
        ]
        return SourceQuery(
            source_expr=read_expr(path, self._crs_by_path[path], self._layer_by_path[path]),
            select_fields=select_fields,
            where_conditions=list(category.file.where),
            bbox_mode="geom",
            dataset_source=f"{Path(urlparse(path).path).name} ({self._snapshot_label})",
            source_url=path if urlparse(path).scheme else "",
            source_description=(
                "Supplied as a spatial data file and clipped to the export boundary; "
                "attributes are the file's own, mapped by the export config."
            ),
            snapshot_date=self._snapshot_date,
            snapshot_label=self._snapshot_label,
            extra_readme_lines=[f"Source file: {Path(urlparse(path).path).name}"],
        )

    def peek_snapshot_label(self, cfg: RootConfig) -> str | None:
        file_config = _file_config(cfg)
        if not file_config.snapshot:
            return None
        return parse_utc(file_config.snapshot).strftime("%Y-%m-%d")


def _file_config(cfg: RootConfig) -> FileSourceConfig:
    file_config = cfg.source.get("file")
    if file_config is None:
        raise FileSourceError("source.file is not configured")
    if isinstance(file_config, dict):
        return FileSourceConfig(**file_config)
    return file_config


def _first_category_setting(
    cfg: RootConfig, file_config: FileSourceConfig, path: str, setting: str, *, default: str
) -> str:
    """The `crs` or `layer` the first enabled category reading `path` sets, else `default`."""
    return next(
        (
            getattr(category.file, setting)
            for category in cfg.categories
            if category.file.enabled
            and category_path(category, file_config) == path
            and getattr(category.file, setting)
        ),
        default,
    )
