"""GIS format writers (gpkg, shp, geojson) over materialised DuckDB tables."""

import time
from dataclasses import dataclass
from pathlib import Path

import duckdb

from oex.logging_setup import get_logger

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
    # Shapefiles only support one geometry type per file. Group source types
    # by destination label so POLYGON + MULTIPOLYGON share one polygons.shp
    # rather than overwriting each other.
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


def write_pmtiles(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    out_path: Path,
    *,
    min_zoom: int,
    max_zoom: int,
) -> Path:
    """Write a single-layer PMTiles archive via GDAL. The layer name is the file stem."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    # The options must be a list. A single comma-joined string parses as one
    # unknown option, which GDAL ignores, falling back to its default MAXZOOM of 5.
    conn.execute(
        f"COPY {table_name} TO '{out_path}' "
        f"WITH (FORMAT GDAL, SRS 'EPSG:4326', DRIVER 'PMTiles', "
        f"DATASET_CREATION_OPTIONS ('MINZOOM={int(min_zoom)}', 'MAXZOOM={int(max_zoom)}'))"
    )
    size_mb = out_path.stat().st_size / (1024 * 1024)
    logger.info("Wrote %s (%.0f MB) in %.2fs", out_path, size_mb, time.time() - start)
    return out_path


def write_geoparquet(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    out_path: Path,
) -> Path:
    """Write the full table as GeoParquet. DuckDB emits GeoParquet metadata for the
    GEOMETRY column, so the file round-trips as GEOMETRY and reads back for tiling."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    conn.execute(f"COPY {table_name} TO '{out_path}' (FORMAT PARQUET)")
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
) -> Path:
    """Merge per-layer GeoParquets (in the given order) into one PMTiles layer.

    `category` and `source` are injected per layer so the single tileset stays
    styleable by both. Each layer's parquet only needs a `geom` column; `name`
    is carried when present.
    """
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
            conn, "__combined_tiles", out_path, min_zoom=min_zoom, max_zoom=max_zoom
        )
    finally:
        conn.execute("DROP TABLE IF EXISTS __combined_tiles")
