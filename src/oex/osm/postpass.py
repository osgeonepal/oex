"""Postpass source engine: live OSM through Geofabrik's PostGIS SQL API.

Postpass mirrors the planet in PostGIS and refreshes every five minutes, so a
small area comes back far fresher than a daily country PBF. The fetch writes the
same country.parquet contract quackosm produces: feature_id, tags, geometry.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from pyproj import Geod
from shapely.geometry import shape

from oex.logging_setup import get_logger
from oex.osm.category_filter import OsmTagsFilter

logger = get_logger(__name__)

DEFAULT_ENDPOINT = "https://postpass.geofabrik.de/api/interpreter"
DEFAULT_TIMEOUT_SECONDS = 600

# osm2pgsql spreads OSM over three tables and stores relations as negative ids.
_TABLES = ("planet_osm_point", "planet_osm_line", "planet_osm_polygon")

# The exporter writes one output schema regardless of engine, so this parquet must
# match what quackosm produces for the geofabrik and planet engines.
PARQUET_CONTRACT = [
    ("feature_id", "VARCHAR"),
    ("tags", "MAP(VARCHAR, VARCHAR)"),
    ("geometry", "GEOMETRY('OGC:CRS84')"),
]

_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class PostpassSnapshot:
    parquet: Path
    timestamp: datetime
    label: str
    feature_count: int


def boundary_area_sq_km(boundary_geojson: str) -> float:
    area, _perimeter = _GEOD.geometry_area_perimeter(shape(json.loads(boundary_geojson)))
    return abs(area) / 1_000_000


def _escape(value: str) -> str:
    return value.replace("'", "''")


def hstore_predicate(tag_filter: OsmTagsFilter) -> str:
    """SQL predicate over the hstore `tags` column matching an oex tag filter.

    Mirrors `category_where_predicate`, which targets a DuckDB MAP instead.
    """
    clauses: list[str] = []
    for key, value in sorted(tag_filter.items()):
        if value is True:
            clauses.append(f"exist(tags, '{_escape(key)}')")
        elif isinstance(value, list):
            values = ", ".join(f"'{_escape(v)}'" for v in value)
            clauses.append(f"tags->'{_escape(key)}' IN ({values})")
        elif value:
            clauses.append(f"tags->'{_escape(key)}' = '{_escape(value)}'")
    if not clauses:
        raise ValueError(
            "postpass engine needs at least one enabled category with a non-empty osm.filter"
        )
    return "(" + " OR ".join(clauses) + ")"


def _feature_id_expr(table: str) -> str:
    if table == "planet_osm_point":
        return "'node/' || osm_id"
    return "CASE WHEN osm_id < 0 THEN 'relation/' || (-osm_id) ELSE 'way/' || osm_id END"


def build_sql(table: str, predicate: str, boundary_geojson: str) -> str:
    """One table's query: geometry travels as WKT so `tags` stays a flat JSON string."""
    return (
        f"WITH aoi AS (SELECT ST_GeomFromGeoJSON('{_escape(boundary_geojson)}') AS g) "
        f"SELECT {_feature_id_expr(table)} AS feature_id, "
        f"COALESCE(hstore_to_json(tags)::text, '{{}}') AS tags_json, "
        f"ST_AsText(way) AS wkt, NULL::geometry AS way "
        f"FROM {table}, aoi "
        f"WHERE {predicate} AND ST_Intersects(way, g)"
    )


def _post(endpoint: str, sql: str, timeout: int) -> dict:
    response = requests.post(endpoint, data={"data": sql}, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"Postpass returned HTTP {response.status_code}: {response.text[:500]}")
    return dict(response.json())


def _rows(payload: dict) -> list[tuple[str, str, str]]:
    """Flatten a Postpass FeatureCollection into (feature_id, tags_json, wkt) rows."""
    properties = (feature["properties"] for feature in payload["features"])
    return [(p["feature_id"], p["tags_json"], p["wkt"]) for p in properties if p["wkt"] is not None]


def _write_parquet(rows: list[tuple[str, str, str]], out_path: Path) -> None:
    import duckdb

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial; INSTALL json; LOAD json;")
    conn.execute("CREATE TEMP TABLE rows (feature_id VARCHAR, tags_json VARCHAR, wkt VARCHAR)")
    if rows:
        conn.executemany("INSERT INTO rows VALUES (?, ?, ?)", rows)
    # osm2pgsql returns every areal feature as MULTIPOLYGON and every linear one as
    # MULTILINESTRING; quackosm keeps single-part features singular. Downcasting here
    # keeps the geometry type in the published files the same across engines.
    conn.execute(f"""
        COPY (
            SELECT feature_id,
                   CAST(json(tags_json) AS MAP(VARCHAR, VARCHAR)) AS tags,
                   CASE WHEN ST_NumGeometries(parsed) = 1
                        THEN ST_Dump(parsed)[1].geom
                        ELSE parsed
                   END AS geometry
            FROM (SELECT feature_id, tags_json, ST_GeomFromText(wkt) AS parsed FROM rows)
        ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    written = [
        (name, dtype)
        for name, dtype, *_ in conn.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{out_path}')"
        ).fetchall()
    ]
    conn.close()
    if written != PARQUET_CONTRACT:
        raise RuntimeError(
            f"Postpass parquet schema {written} does not match the contract "
            f"{PARQUET_CONTRACT} the exporter expects from every OSM engine"
        )


def fetch_country_parquet(
    *,
    boundary_geojson: str,
    tag_filter: OsmTagsFilter,
    out_path: Path,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> PostpassSnapshot:
    """Query Postpass for everything matching the filter and write country.parquet."""
    predicate = hstore_predicate(tag_filter)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, str, str]] = []
    label = ""
    for table in _TABLES:
        payload = _post(endpoint, build_sql(table, predicate, boundary_geojson), timeout)
        label = payload["postpass_properties"]["timestamp"]
        table_rows = _rows(payload)
        rows.extend(table_rows)
        logger.info("Postpass %s: %d features", table, len(table_rows))

    if not rows:
        raise RuntimeError(
            "Postpass returned no features for this boundary and tag filter. Check "
            "boundary.geom and the category osm.filter blocks; an empty result cannot "
            "be written as a geometry parquet."
        )

    _write_parquet(rows, out_path)
    logger.info("Postpass snapshot %s: %d features -> %s", label, len(rows), out_path)
    return PostpassSnapshot(
        parquet=out_path,
        timestamp=datetime.fromisoformat(label.replace("Z", "+00:00")),
        label=label,
        feature_count=len(rows),
    )
