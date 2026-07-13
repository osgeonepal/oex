"""Writer dispatch for supported formats."""

from pathlib import Path

import duckdb
import pytest

from oex.writers import write_format


@pytest.fixture
def conn_with_table(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute(
        """
        CREATE TABLE features AS SELECT * FROM (VALUES
            (1, 'A', ST_Point(83.5, 28.5)),
            (2, 'B', ST_Point(83.7, 28.6))
        ) AS t(id, name, geom)
        """
    )
    return conn


def test_geojson_writer(conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    files = write_format(conn_with_table, "features", "demo", "geojson", out_dir)
    assert len(files) == 1
    assert files[0].name == "demo.geojson"
    assert files[0].stat().st_size > 0


def test_gpkg_writer(conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    files = write_format(conn_with_table, "features", "demo", "gpkg", out_dir)
    assert len(files) == 1
    assert files[0].name == "demo.gpkg"


def test_kml_writer(conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    files = write_format(conn_with_table, "features", "demo", "kml", out_dir)
    assert len(files) == 1
    assert files[0].name == "demo.kml"
    assert files[0].stat().st_size > 0
    body = files[0].read_text(encoding="utf-8")
    assert "<kml" in body
    assert "<Placemark>" in body


def test_shp_writer_splits_by_geometry_type(
    conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    out_dir = tmp_path / "out"
    files = write_format(conn_with_table, "features", "demo", "shp", out_dir)
    assert len(files) >= 1
    assert all(f.name.startswith("demo_") for f in files)
    assert all(f.suffix == ".shp" for f in files)


def test_shp_writer_groups_polygon_and_multipolygon(tmp_path: Path) -> None:
    """POLYGON and MULTIPOLYGON must end up in a single polygons.shp."""
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial")
    conn.execute(
        """
        CREATE TABLE mixed AS SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
            (2, ST_GeomFromText('MULTIPOLYGON(((2 2, 3 2, 3 3, 2 3, 2 2)))')),
            (3, ST_GeomFromText('LINESTRING(0 0, 1 1)'))
        ) AS t(id, geom)
        """
    )
    out_dir = tmp_path / "out"
    files = write_format(conn, "mixed", "demo", "shp", out_dir)
    names = sorted(f.name for f in files)
    assert names == ["demo_lines.shp", "demo_polygons.shp"]


def test_shp_writer_handles_lowercase_and_uppercase_types(tmp_path: Path) -> None:
    """Both POINT (modern) and ST_Point (legacy) shapes must be recognised."""
    from oex.writers import _GEOM_TYPE_TO_LABEL

    assert _GEOM_TYPE_TO_LABEL["POINT"] == "points"
    assert _GEOM_TYPE_TO_LABEL["MULTIPOLYGON"] == "polygons"
    assert _GEOM_TYPE_TO_LABEL["ST_Point"] == "points"


def test_unknown_format_raises(conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_format(conn_with_table, "features", "demo", "xyz", tmp_path)


def test_fgb_writer_emits_indexed_file(
    conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    out_dir = tmp_path / "out"
    files = write_format(conn_with_table, "features", "demo", "fgb", out_dir)
    assert len(files) == 1
    assert files[0].name == "demo.fgb"
    # FGB writes a magic "fgb3" header. A spatially-indexed file has the index
    # written at the end; we just confirm the magic + non-empty payload.
    head = files[0].read_bytes()[:4]
    assert head[:3] == b"fgb"
    assert files[0].stat().st_size > 64


def test_write_pmtiles_produces_archive(
    conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    from pmtiles.reader import MmapSource, Reader

    from oex.writers import write_pmtiles

    out = tmp_path / "roads.pmtiles"
    result = write_pmtiles(conn_with_table, "features", out, min_zoom=0, max_zoom=8)
    assert result == out
    assert out.stat().st_size > 0
    # PMTiles archives start with the ASCII magic "PMTiles".
    assert out.read_bytes()[:7] == b"PMTiles"

    # The requested zooms must reach GDAL. Malformed creation options are ignored
    # silently and the archive is built with GDAL's default MAXZOOM of 5.
    with out.open("rb") as handle:
        header = Reader(MmapSource(handle)).header()
    assert header["min_zoom"] == 0
    assert header["max_zoom"] == 8


def test_write_geoparquet_roundtrips_as_geometry(
    conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    from oex.writers import write_geoparquet

    out = tmp_path / "roads.parquet"
    write_geoparquet(conn_with_table, "features", out)
    assert out.stat().st_size > 0
    gtype = conn_with_table.execute(
        f"SELECT typeof(geom) FROM read_parquet('{out}') LIMIT 1"
    ).fetchone()
    assert gtype is not None and gtype[0].startswith("GEOMETRY")


def test_build_combined_pmtiles_injects_category_source_and_handles_missing_name(
    tmp_path: Path,
) -> None:
    import duckdb

    from oex.writers import TileLayer, build_combined_pmtiles, write_geoparquet

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    # Heterogeneous per-layer schemas; layer b intentionally has no `name` column.
    conn.execute(
        "CREATE TABLE a AS SELECT 'ra' AS name, 'x' AS highway, ST_Point(83.5, 28.5) AS geom"
    )
    conn.execute("CREATE TABLE b AS SELECT 'building' AS klass, ST_Point(83.6, 28.6) AS geom")
    pa = write_geoparquet(conn, "a", tmp_path / "roads.parquet")
    pb = write_geoparquet(conn, "b", tmp_path / "buildings.parquet")

    layers = [
        TileLayer(path=str(pa), category="roads", source="osm"),
        TileLayer(path=str(pb), category="buildings", source="overture"),
    ]
    out = tmp_path / "combined.pmtiles"
    build_combined_pmtiles(conn, layers, out, min_zoom=0, max_zoom=8)
    assert out.read_bytes()[:7] == b"PMTiles"
    # The per-layer parquet itself carries no category column; it is injected at merge time.
    cols_a = {r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM read_parquet('{pa}')").fetchall()}
    assert "category" not in cols_a and "geom" in cols_a
