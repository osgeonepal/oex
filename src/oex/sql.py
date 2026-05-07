"""SELECT/WHERE clause builders + materialise(); shared by both sources."""

import duckdb

from oex.boundary import Boundary
from oex.logging_setup import get_logger

logger = get_logger(__name__)


def build_select_clause(select_fields: list[str]) -> str:
    if not select_fields:
        return "geometry AS geom"
    return ",\n       ".join([*select_fields, "geometry AS geom"])


def build_where_clause(boundary: Boundary, where_conditions: list[str], bbox_cols: str) -> str:
    """Combine bbox prune + boundary intersect + caller-supplied conditions.

    `bbox_cols="bbox"` uses an upstream `bbox` struct (Overture); `"geom"`
    derives the bbox from the geometry column (OSM cache).
    """
    minx, miny, maxx, maxy = boundary.bbox
    if bbox_cols == "geom":
        bbox_filter = (
            f"ST_XMin(geometry) <= {maxx} AND ST_XMax(geometry) >= {minx} AND "
            f"ST_YMin(geometry) <= {maxy} AND ST_YMax(geometry) >= {miny}"
        )
    else:
        bbox_filter = (
            f"bbox.xmin <= {maxx} AND bbox.xmax >= {minx} AND "
            f"bbox.ymin <= {maxy} AND bbox.ymax >= {miny}"
        )

    geojson_literal = boundary.geojson.replace("'", "''")
    intersect = f"ST_Intersects(geometry, ST_GeomFromGeoJSON('{geojson_literal}'))"
    parts = [bbox_filter, intersect]
    parts.extend(f"({cond})" for cond in where_conditions if cond)
    return " AND ".join(parts)


def materialise(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    source_expr: str,
    select_clause: str,
    where_clause: str,
) -> int:
    sql = f"""
    CREATE OR REPLACE TABLE {table_name} AS (
        SELECT {select_clause}
        FROM {source_expr}
        WHERE {where_clause}
    )
    """
    logger.debug("Materialising %s with SQL:\n%s", table_name, sql)
    conn.execute(sql)
    count_row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(count_row[0]) if count_row else 0
