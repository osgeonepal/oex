"""SQL the exporter assembles to materialise each category."""

import duckdb

from oex.boundary import Boundary
from oex.logging_setup import get_logger

logger = get_logger(__name__)


def sql_string(value: str) -> str:
    """`value` as a single-quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(name: str) -> str:
    """`name` as a double-quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def build_select_clause(select_fields: list[str]) -> str:
    if not select_fields:
        return "geometry AS geom"
    return ",\n       ".join([*select_fields, "geometry AS geom"])


def build_where_clause(boundary: Boundary, where_conditions: list[str], bbox_mode: str) -> str:
    """`bbox_mode="bbox"` prunes on Overture's own bbox struct; `"geom"` derives it from the
    geometry for the OSM cache."""
    minx, miny, maxx, maxy = boundary.bbox
    if bbox_mode == "geom":
        bbox_filter = (
            f"ST_XMin(geometry) <= {maxx} AND ST_XMax(geometry) >= {minx} AND "
            f"ST_YMin(geometry) <= {maxy} AND ST_YMax(geometry) >= {miny}"
        )
    else:
        bbox_filter = (
            f"bbox.xmin <= {maxx} AND bbox.xmax >= {minx} AND "
            f"bbox.ymin <= {maxy} AND bbox.ymax >= {miny}"
        )

    # Some PBFs carry vertices off the Earth (lat=115), which GDAL rejects mid-COPY,
    # killing the category.
    earth_filter = (
        "ST_YMin(geometry) >= -90 AND ST_YMax(geometry) <= 90 AND "
        "ST_XMin(geometry) >= -180 AND ST_XMax(geometry) <= 180"
    )

    intersect = f"ST_Intersects(geometry, ST_GeomFromGeoJSON({sql_string(boundary.geojson)}))"
    parts = [bbox_filter, earth_filter, intersect]
    parts.extend(f"({condition})" for condition in where_conditions if condition)
    return " AND ".join(parts)


def materialise(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    source_expr: str,
    select_clause: str,
    where_clause: str,
) -> int:
    query = f"SELECT {select_clause}\n        FROM {source_expr}\n        WHERE {where_clause}"
    sql = f"CREATE OR REPLACE TABLE {table_name} AS ({_trimmed_text(conn, query)})"
    logger.debug("Materialising %s with SQL:\n%s", table_name, sql)
    conn.execute(sql)
    count_row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(count_row[0]) if count_row else 0


def _trimmed_text(conn: duckdb.DuckDBPyConnection, query: str) -> str:
    """`query` with its text trimmed and blanks, such as an Overture address of "", made
    missing, since a blank would count as filled and as a common value."""
    text_columns = [
        sql_identifier(name)
        for name, kind, *_ in conn.execute(f"DESCRIBE {query}").fetchall()
        if kind == "VARCHAR"
    ]
    if not text_columns:
        return query
    # DuckDB's trim strips spaces only, so tabs and line breaks are named.
    cleaned = ", ".join(f"nullif(trim({name}, ' \t\n\r'), '') AS {name}" for name in text_columns)
    return f"SELECT * REPLACE ({cleaned}) FROM ({query})"
