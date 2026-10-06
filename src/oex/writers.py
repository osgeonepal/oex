"""GIS format writers over materialised DuckDB tables."""

import functools
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from oex.logging_setup import get_logger
from oex.metadata import GEOMETRY_COL, bounding_box, geometry_breakdown

logger = get_logger(__name__)

_GEOM_TYPE_TO_LABEL = {
    "POINT": "points",
    "MULTIPOINT": "points",
    "LINESTRING": "lines",
    "MULTILINESTRING": "lines",
    "POLYGON": "polygons",
    "MULTIPOLYGON": "polygons",
    "ST_Point": "points",
    "ST_MultiPoint": "points",
    "ST_LineString": "lines",
    "ST_MultiLineString": "lines",
    "ST_Polygon": "polygons",
    "ST_MultiPolygon": "polygons",
}

_FORMAT_DRIVERS = {
    "geojson": "GeoJSON",
    "gpkg": "GPKG",
    "kml": "KML",
    "shp": "ESRI Shapefile",
    "fgb": "FlatGeobuf",
}

# geoparquet is written by the exporter itself, not through an OGR driver.
SUPPORTED_FORMATS = frozenset(_FORMAT_DRIVERS) | {"geoparquet"}
# Published as bare files whatever the config says: zipping them would defeat reading
# them in place over HTTP, which is the only reason to publish them.
NEVER_ZIPPED = frozenset({"geoparquet"})

_LAYER_CREATION_OPTIONS = {
    "fgb": "ENCODING=UTF-8,SPATIAL_INDEX=YES",
}
_DEFAULT_LAYER_CREATION_OPTIONS = "ENCODING=UTF-8"


GEOMETRY_LABELS = ("points", "lines", "polygons")


def geometry_labels(conn: duckdb.DuckDBPyConnection, table_name: str) -> dict[str, list[str]]:
    """Geometry labels present in a table, mapped to the source types they cover."""
    rows = conn.execute(f"SELECT DISTINCT ST_GeometryType(geom) FROM {table_name}").fetchall()
    labels: dict[str, list[str]] = {}
    for (geom_type,) in rows:
        label = _GEOM_TYPE_TO_LABEL.get(geom_type)
        if label is None:
            logger.warning("unmapped geometry type %s in %s", geom_type, table_name)
            continue
        labels.setdefault(label, []).append(geom_type)
    return labels


def write_format(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    category_slug: str,
    fmt: str,
    out_dir: Path,
) -> list[Path]:
    fmt = fmt.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "shp":
        return _write_shapefiles(conn, table_name, category_slug, out_dir)
    if fmt not in _FORMAT_DRIVERS:
        raise ValueError(f"Unsupported format: {fmt}")
    return _write_single(conn, table_name, category_slug, fmt, out_dir)


def _write_single(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    category_slug: str,
    fmt: str,
    out_dir: Path,
) -> list[Path]:
    driver = _FORMAT_DRIVERS[fmt]
    layer_options = _LAYER_CREATION_OPTIONS.get(fmt, _DEFAULT_LAYER_CREATION_OPTIONS)
    target = out_dir / f"{category_slug}.{fmt}"
    start = time.time()
    conn.execute(
        f"COPY {table_name} TO '{target}' "
        f"WITH (FORMAT GDAL, SRS 'EPSG:4326', DRIVER '{driver}', "
        f"LAYER_CREATION_OPTIONS '{layer_options}')"
    )
    size_mb = target.stat().st_size / (1024 * 1024)
    logger.info("Wrote %s (%.0f MB) in %.2fs", target, size_mb, time.time() - start)
    return [target]


def _write_shapefiles(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    category_slug: str,
    out_dir: Path,
) -> list[Path]:
    # A shapefile holds one geometry type, so POLYGON and MULTIPOLYGON share one
    # polygons.shp rather than overwriting each other.
    rows = conn.execute(f"SELECT DISTINCT ST_GeometryType(geom) FROM {table_name}").fetchall()
    geom_types = [r[0] for r in rows]
    if not geom_types:
        logger.warning("No geometries to export for %s", category_slug)
        return []

    label_to_types: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for geom_type in geom_types:
        label = _GEOM_TYPE_TO_LABEL.get(geom_type)
        if label is None:
            unmapped.append(geom_type)
            continue
        label_to_types.setdefault(label, []).append(geom_type)

    if unmapped:
        logger.warning(
            "shp writer: skipping unmapped geometry type(s) %s for %s",
            unmapped,
            category_slug,
        )

    written: list[Path] = []
    for label, types in label_to_types.items():
        target = out_dir / f"{category_slug}_{label}.shp"
        in_list = ", ".join(f"'{t}'" for t in types)
        start = time.time()
        conn.execute(
            f"""
            COPY (
                SELECT * FROM {table_name}
                WHERE ST_GeometryType(geom) IN ({in_list})
            ) TO '{target}'
            WITH (FORMAT GDAL, SRS 'EPSG:4326', DRIVER 'ESRI Shapefile',
                  LAYER_CREATION_OPTIONS 'ENCODING=UTF-8,2GB_LIMIT=NO')
            """
        )
        size_mb = target.stat().st_size / (1024 * 1024)
        logger.info(
            "Wrote %s (%s, %.0f MB) in %.2fs",
            target,
            types,
            size_mb,
            time.time() - start,
        )
        written.append(target)
    return written


# Per the tippecanoe CHANGELOG: 2.31 added --no-tiny-polygon-reduction-at-maximum-zoom.
TIPPECANOE_MIN_VERSION = (2, 31, 0)
# Per the tippecanoe CHANGELOG, FlatGeobuf integers read as numbers from 2.81, booleans from 2.66.
_TIPPECANOE_TYPES = {
    "BOOLEAN": "bool",
    **dict.fromkeys(
        (
            "TINYINT",
            "SMALLINT",
            "INTEGER",
            "BIGINT",
            "HUGEINT",
            "UTINYINT",
            "USMALLINT",
            "UINTEGER",
            "UBIGINT",
            "UHUGEINT",
        ),
        "int",
    ),
}


class TileBuilderError(RuntimeError):
    """tippecanoe is missing, too old, or failed to build a tileset."""


@functools.cache
def tile_builder() -> str:
    """Path to a tippecanoe recent enough for the options oex passes, checked once per process."""
    path = shutil.which("tippecanoe")
    if path is None:
        raise TileBuilderError(
            "output.pmtiles needs tippecanoe on PATH: install it with your package manager "
            "(Debian bookworm-backports, Debian trixie and Ubuntu 24.04 ship it) "
            "or from github.com/felt/tippecanoe"
        )
    reported = subprocess.run([path, "--version"], capture_output=True, text=True, check=True)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", reported.stdout + reported.stderr)
    if match is None:
        raise TileBuilderError(f"cannot read a version from {path} --version")
    version = tuple(int(part) for part in match.groups())
    if version < TIPPECANOE_MIN_VERSION:
        wanted = ".".join(map(str, TIPPECANOE_MIN_VERSION))
        raise TileBuilderError(f"{path} is {match.group(0)}; oex needs {wanted} or later")
    return path


def attribute_types(conn: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    """`--attribute-type` options for every column tippecanoe would otherwise mistype."""
    columns = conn.execute(f"DESCRIBE SELECT * FROM {table_name}").fetchall()
    return [
        f"--attribute-type={name}:{_TIPPECANOE_TYPES[kind]}"
        for name, kind, *_ in columns
        if kind in _TIPPECANOE_TYPES
    ]


def _collections_split(table_name: str) -> str:
    """Rows in order with each GeometryCollection split into one row per part, which
    tippecanoe's FlatGeobuf reader needs. Rows without a geometry have nothing to draw."""
    parts = (
        f"CASE WHEN ST_GeometryType({GEOMETRY_COL}) = 'GEOMETRYCOLLECTION' "
        f"THEN list_transform(ST_Dump({GEOMETRY_COL}), part -> part.geom) "
        f"ELSE [{GEOMETRY_COL}] END"
    )
    return (
        f"SELECT * REPLACE (UNNEST({parts}) AS {GEOMETRY_COL}) FROM {table_name} "
        f"WHERE {GEOMETRY_COL} IS NOT NULL"
    )


def write_pmtiles(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    out_path: Path,
    *,
    layer: str,
    min_zoom: int,
    max_zoom: int,
    max_extra_zooms: int,
) -> Path:
    """Single-layer PMTiles. tippecanoe adds up to `max_extra_zooms` zooms while features are
    still dropped, then drops the densest at the top zoom."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    with tempfile.TemporaryDirectory(dir=out_path.parent, prefix=".tiles_") as work:
        staged = Path(work) / f"{out_path.stem}.fgb"
        building = Path(work) / out_path.name
        quoted = str(staged).replace("'", "''")
        # tippecanoe reads the file in order and never uses its index.
        conn.execute(
            f"COPY ({_collections_split(table_name)}) TO '{quoted}' WITH (FORMAT GDAL, SRS 'EPSG:4326', "
            "DRIVER 'FlatGeobuf', LAYER_CREATION_OPTIONS ('SPATIAL_INDEX=NO'))"
        )
        built = subprocess.run(
            [
                tile_builder(),
                f"--output={building}",
                f"--layer={layer}",
                f"--minimum-zoom={int(min_zoom)}",
                f"--maximum-zoom={int(max_zoom)}",
                f"--temporary-directory={work}",
                "--drop-densest-as-needed",
                # Alone this both enables and caps extension; with the uncapped flag it is ignored.
                f"--extend-zooms-if-still-dropping-maximum={int(max_extra_zooms)}",
                "--no-tiny-polygon-reduction-at-maximum-zoom",
                *attribute_types(conn, table_name),
                "--force",
                "--quiet",
                str(staged),
            ],
            capture_output=True,
            text=True,
            # Per the tippecanoe CHANGELOG, sorting honours --temporary-directory only from 2.81.
            env={**os.environ, "TMPDIR": work},
        )
        if built.returncode != 0:
            raise TileBuilderError(f"tippecanoe failed on {out_path.name}: {built.stderr.strip()}")
        shutil.move(building, out_path)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    logger.info("Wrote %s (%.0f MB) in %.2fs", out_path, size_mb, time.time() - start)
    return out_path


BBOX_COL = "bbox"
# Keeps the footer a reader fetches before anything else in the tens of kilobytes.
TARGET_ROW_GROUPS = 64
MIN_ROW_GROUP_SIZE = 1_000
# DuckDB reports SQL/MM type names; GeoParquet takes the GeoJSON spelling.
_GEOJSON_TYPES = {
    "POINT": "Point",
    "LINESTRING": "LineString",
    "POLYGON": "Polygon",
    "MULTIPOINT": "MultiPoint",
    "MULTILINESTRING": "MultiLineString",
    "MULTIPOLYGON": "MultiPolygon",
    "GEOMETRYCOLLECTION": "GeometryCollection",
}


def row_group_size(row_count: int) -> int:
    """A layer of three features and one of three million both need a sensible split."""
    return max(MIN_ROW_GROUP_SIZE, math.ceil(row_count / TARGET_ROW_GROUPS))


def geo_metadata(
    geometry_types: dict[str, int], bbox: tuple[float, float, float, float] | None
) -> dict:
    """The `geo` key of a GeoParquet 1.1 file. Written by hand because DuckDB emits 1.0.0
    with no `covering`, the field that lets a reader skip row groups. Omitted `crs` is CRS84."""
    column: dict[str, Any] = {
        "encoding": "WKB",
        "geometry_types": sorted(
            {_GEOJSON_TYPES[name] for name in geometry_types if name in _GEOJSON_TYPES}
        ),
        "covering": {"bbox": {edge: [BBOX_COL, edge] for edge in ("xmin", "ymin", "xmax", "ymax")}},
    }
    if bbox is not None:
        column["bbox"] = list(bbox)
    return {"version": "1.1.0", "primary_column": GEOMETRY_COL, "columns": {GEOMETRY_COL: column}}


def write_geoparquet(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    out_path: Path,
) -> Path:
    """Write the table as GeoParquet 1.1, Hilbert ordered over the layer's own extent so
    each row group covers a small area and its bbox statistics exclude most queries."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    existing = {row[0] for row in conn.execute(f"DESCRIBE SELECT * FROM {table_name}").fetchall()}
    if BBOX_COL in existing:
        raise ValueError(
            f"{table_name} already has a {BBOX_COL!r} column, which the GeoParquet covering "
            f"names; rename it in the category's select so the covering is unambiguous"
        )
    counted = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    row_count = int(counted[0]) if counted else 0
    empty = row_count == 0
    geo = geo_metadata(
        geometry_breakdown(conn, table_name), None if empty else bounding_box(conn, table_name)
    )
    extent = f"(SELECT ST_Extent(ST_Extent_Agg({GEOMETRY_COL})) FROM {table_name})"
    order_by = "" if empty else f"ORDER BY ST_Hilbert({GEOMETRY_COL}, {extent})"
    payload = json.dumps(geo).replace("'", "''")
    conn.execute(f"""
        COPY (
            SELECT * EXCLUDE ({GEOMETRY_COL}),
                   struct_pack(
                       xmin := ST_XMin({GEOMETRY_COL}), ymin := ST_YMin({GEOMETRY_COL}),
                       xmax := ST_XMax({GEOMETRY_COL}), ymax := ST_YMax({GEOMETRY_COL})
                   ) AS {BBOX_COL},
                   ST_AsWKB({GEOMETRY_COL}) AS {GEOMETRY_COL}
            FROM {table_name} {order_by}
        ) TO '{out_path}'
        (FORMAT PARQUET, ROW_GROUP_SIZE {row_group_size(row_count)},
         KV_METADATA {{geo: '{payload}'}})
    """)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    logger.info("Wrote %s (%.0f MB) in %.2fs", out_path, size_mb, time.time() - start)
    return out_path


@dataclass(frozen=True)
class TileLayer:
    # `path` is a local path or an https/s3 URL DuckDB can read via httpfs.
    path: str
    category: str
    source: str


def build_combined_pmtiles(
    conn: duckdb.DuckDBPyConnection,
    layers: list[TileLayer],
    out_path: Path,
    *,
    min_zoom: int,
    max_zoom: int,
    max_extra_zooms: int,
) -> Path:
    """One tileset over every layer, in the given order, with `category` and `source` injected
    so each stays styleable; `name` is carried when present."""
    if not layers:
        raise ValueError("build_combined_pmtiles needs at least one layer")
    selects = []
    for layer in layers:
        cols = {
            row[0]
            for row in conn.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{layer.path}')"
            ).fetchall()
        }
        name_expr = "name" if "name" in cols else "NULL"
        selects.append(
            f"SELECT '{layer.category}' AS category, '{layer.source}' AS source, "
            f"{name_expr} AS name, geom FROM read_parquet('{layer.path}')"
        )
    conn.execute(f"CREATE TABLE __combined_tiles AS {' UNION ALL '.join(selects)}")
    try:
        return write_pmtiles(
            conn,
            "__combined_tiles",
            out_path,
            layer=out_path.stem,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            max_extra_zooms=max_extra_zooms,
        )
    finally:
        conn.execute("DROP TABLE IF EXISTS __combined_tiles")
