"""GIS format writers (gpkg, shp, geojson) over materialised DuckDB tables."""

import time
from pathlib import Path

import duckdb

from oex.logging_setup import get_logger

logger = get_logger(__name__)

# Accept both DuckDB spatial 1.x ("POINT") and legacy ("ST_Point") forms so
# upgrade or downgrade does not silently bucket every geom type into "other".
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
}


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
    target = out_dir / f"{category_slug}.{fmt}"
    start = time.time()
    conn.execute(
        f"COPY {table_name} TO '{target}' "
        f"WITH (FORMAT GDAL, SRS 'EPSG:4326', DRIVER '{driver}', "
        f"LAYER_CREATION_OPTIONS 'ENCODING=UTF-8')"
    )
    logger.info("Wrote %s in %.2fs", target, time.time() - start)
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
        logger.info("Wrote %s (%s) in %.2fs", target, types, time.time() - start)
        written.append(target)
    return written
