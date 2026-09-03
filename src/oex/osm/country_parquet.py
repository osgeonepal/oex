"""The country.parquet contract every OSM engine writes.

quackosm produces feature_id, tags and geometry for the geofabrik and planet
engines; the live engines assemble the same three columns so the exporter, the
category selects and the published schema stay identical whichever engine ran.
"""

from pathlib import Path

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
    # Upstreams differ on whether a single-part feature arrives as MULTIPOLYGON;
    # downcasting keeps the geometry type in the published files the same across engines.
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
            f"{source} parquet schema {written} does not match the contract "
            f"{PARQUET_CONTRACT} the exporter expects from every OSM engine"
        )
