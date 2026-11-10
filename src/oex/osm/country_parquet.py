"""The country.parquet contract, feature_id, tags and geometry, which every OSM engine
writes so the exporter and the published schema do not depend on the engine.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from oex.osm.engines import lifecycle_area_rules
from oex.sql import sql_string


@dataclass(frozen=True)
class LiveSnapshot:
    """What a live engine returns: the parquet it wrote and when the data was current."""

    parquet: Path
    timestamp: datetime
    label: str
    feature_count: int


PARQUET_CONTRACT = [
    ("feature_id", "VARCHAR"),
    ("tags", "MAP(VARCHAR, VARCHAR)"),
    ("geometry", "GEOMETRY('OGC:CRS84')"),
]

Row = tuple[str, str, str]


def write_country_parquet(rows: list[Row], out_path: Path, source: str) -> None:
    """Write (feature_id, tags_json, wkt) rows to the contract, raising if it drifts."""
    import duckdb

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial; INSTALL json; LOAD json;")
    conn.execute("CREATE TEMP TABLE rows (feature_id VARCHAR, tags_json VARCHAR, wkt VARCHAR)")
    if rows:
        conn.executemany("INSERT INTO rows VALUES (?, ?, ?)", rows)
    # Single-part multis are downcast so geometry types match across engines; a live database
    # stores a closed way with only a lifecycle or helipad key as a line.
    conn.execute(f"""
        COPY (
            SELECT feature_id, tags,
                   CASE WHEN ST_GeometryType(single) = 'LINESTRING' AND ST_IsClosed(single)
                             AND ST_NPoints(single) >= 4
                             AND coalesce(tags['area'], '') <> 'no'
                             AND ({_lifecycle_area_predicate()})
                        THEN ST_MakePolygon(single)
                        ELSE single
                   END AS geometry
            FROM (
                SELECT feature_id,
                       CAST(json(tags_json) AS MAP(VARCHAR, VARCHAR)) AS tags,
                       CASE WHEN ST_NumGeometries(parsed) = 1
                            THEN ST_Dump(parsed)[1].geom
                            ELSE parsed
                       END AS single
                FROM (SELECT feature_id, tags_json, ST_GeomFromText(wkt) AS parsed FROM rows)
            )
        ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    written = [
        (name, column_type)
        for name, column_type, *_ in conn.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{out_path}')"
        ).fetchall()
    ]
    conn.close()
    if written != PARQUET_CONTRACT:
        raise RuntimeError(
            f"{source} parquet schema {written} does not match the contract "
            f"{PARQUET_CONTRACT} the exporter expects from every OSM engine"
        )


def _lifecycle_area_predicate() -> str:
    """`lifecycle_area_rules` over the `tags` map, matched the way quackosm matches its rules."""
    rules = lifecycle_area_rules()
    keys = ", ".join(sql_string(key) for key in rules["all"])
    clauses = [f"list_has_any(map_keys(tags), [{keys}])"]
    for key, values in rules["allowlist"].items():
        listed = ", ".join(sql_string(value) for value in values)
        clauses.append(f"tags[{sql_string(key)}] IN ({listed})")
    for key, values in rules["denylist"].items():
        listed = ", ".join(sql_string(value) for value in values)
        clauses.append(
            f"(tags[{sql_string(key)}] IS NOT NULL AND tags[{sql_string(key)}] NOT IN ({listed}))"
        )
    return " OR ".join(clauses)
