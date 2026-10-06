"""Writer dispatch for supported formats."""

from pathlib import Path

import duckdb
import pytest

from oex.writers import geometry_labels, write_format


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
    result = write_pmtiles(
        conn_with_table,
        "features",
        out,
        layer="features",
        min_zoom=0,
        max_zoom=8,
        max_extra_zooms=4,
    )
    assert result == out
    assert out.stat().st_size > 0
    # PMTiles archives start with the ASCII magic "PMTiles".
    assert out.read_bytes()[:7] == b"PMTiles"

    # Two points never overflow a tile, so the requested top zoom stands as given.
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
    build_combined_pmtiles(conn, layers, out, min_zoom=0, max_zoom=8, max_extra_zooms=4)
    assert out.read_bytes()[:7] == b"PMTiles"
    # The per-layer parquet itself carries no category column; it is injected at merge time.
    cols_a = {r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM read_parquet('{pa}')").fetchall()}
    assert "category" not in cols_a and "geom" in cols_a


@pytest.fixture
def conn_mixed_geometry() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute(
        """
        CREATE TABLE mixed AS SELECT * FROM (VALUES
            (1, ST_Point(83.5, 28.5)),
            (2, ST_GeomFromText('LINESTRING (83 28, 84 29)')),
            (3, ST_GeomFromText('POLYGON ((83 28, 84 28, 84 29, 83 29, 83 28))')),
            (4, ST_GeomFromText('MULTIPOLYGON (((83 28, 84 28, 84 29, 83 28)))'))
        ) AS t(id, geom)
        """
    )
    return conn


def test_geometry_labels_groups_source_types_by_label(conn_mixed_geometry) -> None:
    labels = geometry_labels(conn_mixed_geometry, "mixed")
    assert sorted(labels) == ["lines", "points", "polygons"]
    assert sorted(labels["polygons"]) == ["MULTIPOLYGON", "POLYGON"]


def test_geometry_labels_is_empty_for_an_empty_table(conn_mixed_geometry) -> None:
    conn_mixed_geometry.execute("CREATE TABLE none_ AS SELECT * FROM mixed WHERE false")
    assert geometry_labels(conn_mixed_geometry, "none_") == {}


def _geo(path: Path) -> dict:
    import json

    import pyarrow.parquet as pq

    return json.loads(pq.ParquetFile(path).schema_arrow.metadata[b"geo"])


def _row_groups(path: Path) -> list[dict[str, float]]:
    """Each row group's bbox, read from the Parquet statistics a reader filters on."""
    import pyarrow.parquet as pq

    metadata = pq.ParquetFile(path).metadata
    boxes = []
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        box: dict[str, float] = {}
        for column in range(group.num_columns):
            stats = group.column(column)
            if stats.path_in_schema.startswith("bbox."):
                edge = stats.path_in_schema.split(".")[1]
                box[edge] = stats.statistics.min if "min" in edge else stats.statistics.max
        boxes.append(box)
    return boxes


def _spread(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, Path]:
    """Points spread over a degree, which is what makes row group bounds meaningful."""
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute(
        "CREATE TABLE features AS "
        "SELECT i AS id, ST_Point(84.0 + (i % 100) / 100.0, 27.0 + (i / 100) / 100.0) AS geom "
        "FROM range(4000) t(i)"
    )
    from oex.writers import write_geoparquet

    out = tmp_path / "spread.parquet"
    write_geoparquet(conn, "features", out)
    return conn, out


def test_the_geoparquet_declares_the_version_that_carries_a_covering() -> None:
    """1.0.0 has no covering, and without it a reader cannot skip anything."""
    from oex.writers import BBOX_COL, geo_metadata

    geo = geo_metadata({"POLYGON": 3}, (1.0, 2.0, 3.0, 4.0))
    assert geo["version"] == "1.1.0"
    covering = geo["columns"]["geom"]["covering"]["bbox"]
    assert covering == {
        "xmin": [BBOX_COL, "xmin"],
        "ymin": [BBOX_COL, "ymin"],
        "xmax": [BBOX_COL, "xmax"],
        "ymax": [BBOX_COL, "ymax"],
    }


def test_geometry_types_are_written_the_way_the_spec_spells_them() -> None:
    """DuckDB reports MULTIPOLYGON; a reader looking for MultiPolygon finds nothing."""
    from oex.writers import geo_metadata

    geo = geo_metadata({"MULTIPOLYGON": 2, "POINT": 1}, None)
    assert geo["columns"]["geom"]["geometry_types"] == ["MultiPolygon", "Point"]


def test_a_layer_with_no_features_still_describes_itself() -> None:
    """An empty layer must not write a bbox it does not have."""
    from oex.writers import geo_metadata

    geo = geo_metadata({}, None)
    assert geo["columns"]["geom"]["geometry_types"] == []
    assert "bbox" not in geo["columns"]["geom"]


def test_the_row_group_size_suits_the_layer_it_is_given() -> None:
    """One size cannot serve three features and three million."""
    from oex.writers import row_group_size

    assert row_group_size(3) == 1_000
    assert row_group_size(2_000_000) == 31_250
    # Never one giant group, which is what makes a file unskippable.
    assert row_group_size(200_000) < 200_000


def test_a_written_layer_is_split_into_row_groups_a_reader_can_skip(tmp_path: Path) -> None:
    """One row group means every query reads the whole file however small its area."""
    _conn, out = _spread(tmp_path)
    boxes = _row_groups(out)
    assert len(boxes) > 1

    layer_width = max(b["xmax"] for b in boxes) - min(b["xmin"] for b in boxes)
    widths = [b["xmax"] - b["xmin"] for b in boxes]
    # Hilbert ordering is the point: without it each group spans the whole layer.
    assert sum(widths) / len(widths) < layer_width


def test_a_query_window_reads_only_the_row_groups_that_hold_it(tmp_path: Path) -> None:
    """This is the behaviour the covering exists for, measured the way a reader sees it."""
    _conn, out = _spread(tmp_path)
    boxes = _row_groups(out)
    window = (84.02, 27.02, 84.06, 27.06)
    touched = [
        b
        for b in boxes
        if not (
            b["xmax"] < window[0]
            or b["xmin"] > window[2]
            or b["ymax"] < window[1]
            or b["ymin"] > window[3]
        )
    ]
    assert len(touched) < len(boxes)


def test_the_bbox_column_stays_out_of_the_tiles(tmp_path: Path) -> None:
    """It is a storage detail; carrying it into the tileset would bloat every feature."""
    from unittest.mock import patch

    from oex.writers import BBOX_COL, TileLayer, build_combined_pmtiles

    conn, out = _spread(tmp_path)
    assert BBOX_COL in {
        row[0] for row in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
    }
    seen: dict[str, set[str]] = {}

    def capture(connection, table, out_path, **kwargs):  # noqa: ANN001, ANN202
        seen["columns"] = {
            row[0] for row in connection.execute(f"DESCRIBE SELECT * FROM {table}").fetchall()
        }
        out_path.write_bytes(b"PMTiles")
        return out_path

    with patch("oex.writers.write_pmtiles", side_effect=capture):
        build_combined_pmtiles(
            conn,
            [TileLayer(path=str(out), category="spread", source="osm")],
            tmp_path / "tiles.pmtiles",
            min_zoom=0,
            max_zoom=4,
            max_extra_zooms=4,
        )
    assert BBOX_COL not in seen["columns"]
    assert {"category", "source", "geom"} <= seen["columns"]


def test_the_file_carries_exactly_one_geoparquet_metadata_block(tmp_path: Path) -> None:
    """DuckDB writes its own `geo` alongside ours unless the geometry is handed to it as
    plain WKB, and a reader taking the last one would see 1.0.0 with no covering."""
    import re

    _conn, out = _spread(tmp_path)
    blocks = re.findall(rb'\{"version"\s*:\s*"[0-9.]+"', out.read_bytes())
    assert len(blocks) == 1, [b.decode() for b in blocks]
    assert b'"1.1.0"' in blocks[0]


def test_the_written_geometry_still_reads_back_as_geometry(tmp_path: Path) -> None:
    """Handing DuckDB plain WKB must not cost the round trip the tiler depends on."""
    conn, out = _spread(tmp_path)
    typed = conn.execute(f"SELECT typeof(geom) FROM read_parquet('{out}') LIMIT 1").fetchone()
    assert typed is not None and typed[0].startswith("GEOMETRY")
    assert (
        conn.execute(f"SELECT ST_X(geom) FROM read_parquet('{out}') LIMIT 1").fetchone() is not None
    )


def test_a_source_column_called_bbox_is_refused(tmp_path: Path) -> None:
    """Overture carries its own `bbox`, and SELECT would emit two columns of that name
    while the covering names one of them, silently pointing at the wrong data."""
    from oex.writers import write_geoparquet

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute(
        "CREATE TABLE features AS SELECT 1 AS id, {xmin: 1.0} AS bbox, ST_Point(84.0, 27.0) AS geom"
    )
    with pytest.raises(ValueError, match="already has a 'bbox' column"):
        write_geoparquet(conn, "features", tmp_path / "x.parquet")


def _decoded_features(path: Path, zoom: int) -> list[dict]:
    """Every feature tippecanoe-decode finds at one zoom, as GeoJSON."""
    import json
    import subprocess

    decoded = subprocess.run(
        ["tippecanoe-decode", f"--minimum-zoom={zoom}", f"--maximum-zoom={zoom}", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    features: list[dict] = []
    for tile in json.loads(decoded.stdout)["features"]:
        for layer in tile["features"]:
            features.extend(layer["features"])
    return features


def _top_zoom(path: Path) -> int:
    from pmtiles.reader import MmapSource, Reader

    with path.open("rb") as handle:
        return Reader(MmapSource(handle)).header()["max_zoom"]


def test_every_feature_survives_at_the_top_zoom(tmp_path: Path) -> None:
    """A tile past its size budget must not shed features at the zoom readers inspect."""
    from oex.writers import write_pmtiles

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    # Hashes do not compress, so these overflow a zoom 2 tile without needing 200,000 rows.
    conn.execute(
        "CREATE TABLE dense AS SELECT i AS id, md5(i::VARCHAR) || md5((i * 7)::VARCHAR) AS note, "
        "ST_Point(85.30 + (i % 200) / 20000.0, 27.70 + (i // 200) / 20000.0) AS geom "
        "FROM range(20000) t(i)"
    )
    out = write_pmtiles(
        conn,
        "dense",
        tmp_path / "dense.pmtiles",
        layer="features",
        min_zoom=0,
        max_zoom=2,
        max_extra_zooms=20,
    )

    top = _top_zoom(out)
    assert top > 2, "the top zoom must rise when the requested one cannot hold every feature"
    ids = {feature["properties"]["id"] for feature in _decoded_features(out, top)}
    assert len(ids) == 20000


def test_tiles_keep_longitude_before_latitude(
    conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Tiles are staged through GDAL, so a swapped axis would move every feature."""
    from oex.writers import write_pmtiles

    out = write_pmtiles(
        conn_with_table,
        "features",
        tmp_path / "t.pmtiles",
        layer="features",
        min_zoom=0,
        max_zoom=8,
        max_extra_zooms=4,
    )
    point = next(
        f["geometry"]["coordinates"]
        for f in _decoded_features(out, 8)
        if f["properties"]["name"] == "A"
    )
    assert point == pytest.approx([83.5, 28.5], abs=0.01)


@pytest.fixture
def fresh_tile_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A PATH holding only the scripted tippecanoe written into the returned directory."""
    from oex.writers import tile_builder

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    tile_builder.cache_clear()
    yield bin_dir
    tile_builder.cache_clear()


def _fake_tippecanoe(directory: Path, body: str) -> None:
    script = directory / "tippecanoe"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)


def test_a_missing_tile_builder_is_named_in_the_error(fresh_tile_builder: Path) -> None:
    from oex.writers import TileBuilderError, tile_builder

    with pytest.raises(TileBuilderError, match="needs tippecanoe on PATH"):
        tile_builder()


def test_a_tippecanoe_older_than_the_options_oex_passes_is_refused(
    fresh_tile_builder: Path,
) -> None:
    from oex.writers import TileBuilderError, tile_builder

    _fake_tippecanoe(fresh_tile_builder, "echo 'tippecanoe v2.30.9' >&2")
    with pytest.raises(TileBuilderError, match="needs 2.31.0 or later"):
        tile_builder()


def test_a_recent_enough_tippecanoe_is_accepted(fresh_tile_builder: Path) -> None:
    from oex.writers import tile_builder

    _fake_tippecanoe(fresh_tile_builder, "echo 'tippecanoe v2.31.0' >&2")
    assert tile_builder() == str(fresh_tile_builder / "tippecanoe")


def test_a_tippecanoe_without_a_version_number_is_refused(fresh_tile_builder: Path) -> None:
    from oex.writers import TileBuilderError, tile_builder

    _fake_tippecanoe(fresh_tile_builder, "echo tippecanoe")
    with pytest.raises(TileBuilderError, match="cannot read a version"):
        tile_builder()


def test_numbers_and_flags_keep_their_types_in_the_tiles(tmp_path: Path) -> None:
    """A map filter on `levels > 2` matches nothing when the value arrives as a string."""
    from oex.writers import write_pmtiles

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute(
        "CREATE TABLE typed AS SELECT 7::BIGINT AS levels, 2.5::DOUBLE AS height, "
        "false AS covered, ST_Point(85.3, 27.7) AS geom"
    )
    out = write_pmtiles(
        conn,
        "typed",
        tmp_path / "typed.pmtiles",
        layer="features",
        min_zoom=0,
        max_zoom=4,
        max_extra_zooms=4,
    )
    (properties,) = [feature["properties"] for feature in _decoded_features(out, 4)]
    assert properties == {"levels": 7, "height": 2.5, "covered": False}


def test_small_buildings_survive_at_the_top_zoom(tmp_path: Path) -> None:
    """Building footprints are smaller than a pixel at zoom 12 and must not be merged away."""
    from oex.writers import write_pmtiles

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute(
        "CREATE TABLE footprints AS SELECT i AS id, ST_MakeEnvelope(x, y, x + 0.00003, y + 0.00003) "
        "AS geom FROM (SELECT i, 85.30 + (i % 200) / 5000.0 AS x, 27.70 + (i // 200) / 5000.0 AS y "
        "FROM range(20000) t(i))"
    )
    out = write_pmtiles(
        conn,
        "footprints",
        tmp_path / "f.pmtiles",
        layer="features",
        min_zoom=0,
        max_zoom=12,
        max_extra_zooms=4,
    )
    ids = {feature["properties"]["id"] for feature in _decoded_features(out, _top_zoom(out))}
    assert len(ids) == 20000


def _stacked_points(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, Path]:
    """30,000 points on one coordinate, which never separate however far tiles zoom in."""
    from oex.writers import write_geoparquet

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute(
        "CREATE TABLE stacked AS SELECT i AS id, md5(i::VARCHAR) || md5((i * 7)::VARCHAR) AS name, "
        "ST_Point(85.3, 27.7) AS geom FROM range(30000) t(i)"
    )
    parquet = tmp_path / "stacked.parquet"
    write_geoparquet(conn, "stacked", parquet)
    return conn, parquet


def test_added_zooms_stop_at_the_configured_limit(tmp_path: Path) -> None:
    """Without a limit these tiles run to z24."""
    from oex.writers import write_pmtiles

    conn, _ = _stacked_points(tmp_path)
    out = write_pmtiles(
        conn,
        "stacked",
        tmp_path / "s.pmtiles",
        layer="features",
        min_zoom=0,
        max_zoom=2,
        max_extra_zooms=2,
    )
    assert _top_zoom(out) == 4


def test_the_combined_tileset_honours_the_same_limit(tmp_path: Path) -> None:
    from oex.writers import TileLayer, build_combined_pmtiles

    conn, parquet = _stacked_points(tmp_path)
    out = build_combined_pmtiles(
        conn,
        [TileLayer(path=str(parquet), category="stacked", source="osm")],
        tmp_path / "c.pmtiles",
        min_zoom=0,
        max_zoom=2,
        max_extra_zooms=2,
    )
    assert _top_zoom(out) == 4


def test_every_part_of_a_geometry_collection_reaches_the_tiles(tmp_path: Path) -> None:
    """OSM relations come out as collections, and tippecanoe refuses them in FlatGeobuf."""
    from oex.writers import write_pmtiles

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute(
        "CREATE TABLE places AS SELECT * FROM (VALUES "
        "(1, 'a', ST_GeomFromText('GEOMETRYCOLLECTION(POINT(85.3 27.7), "
        "LINESTRING(85.3 27.7, 85.31 27.71))')), "
        "(2, 'b', ST_Point(85.32, 27.72)), (3, 'c', NULL)) t(id, __part, geom)"
    )
    out = write_pmtiles(
        conn,
        "places",
        tmp_path / "p.pmtiles",
        layer="features",
        min_zoom=0,
        max_zoom=8,
        max_extra_zooms=4,
    )
    kinds = sorted(
        (feature["properties"]["id"], feature["geometry"]["type"])
        for feature in _decoded_features(out, 8)
    )
    assert kinds == [(1, "LineString"), (1, "Point"), (2, "Point")]


def test_a_failed_tile_build_reports_tippecanoe_and_leaves_nothing_behind(
    conn_with_table: duckdb.DuckDBPyConnection, fresh_tile_builder: Path, tmp_path: Path
) -> None:
    """tippecanoe writes zooms as it goes, so a failure part way leaves a truncated archive."""
    from oex.writers import TileBuilderError, write_pmtiles

    _fake_tippecanoe(
        fresh_tile_builder,
        '[ "$1" = --version ] && { echo "tippecanoe v2.80.0" >&2; exit 0; }\n'
        ': > "${1#--output=}"\necho "disk full" >&2\nexit 1',
    )
    out_dir = tmp_path / "out"
    with pytest.raises(TileBuilderError, match="disk full"):
        write_pmtiles(
            conn_with_table,
            "features",
            out_dir / "t.pmtiles",
            layer="features",
            min_zoom=0,
            max_zoom=8,
            max_extra_zooms=4,
        )
    assert list(out_dir.iterdir()) == []


def test_a_tileset_carries_the_layer_name_it_is_given(
    conn_with_table: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Named for the category, so one map style serves every country."""
    import json
    import subprocess

    from oex.writers import write_pmtiles

    out = write_pmtiles(
        conn_with_table,
        "features",
        tmp_path / "hotosm_npl_buildings_osm.pmtiles",
        layer="buildings",
        min_zoom=0,
        max_zoom=4,
        max_extra_zooms=0,
    )
    decoded = subprocess.run(
        ["tippecanoe-decode", "--maximum-zoom=0", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    layers = {
        layer["properties"]["layer"]
        for tile in json.loads(decoded.stdout)["features"]
        for layer in tile["features"]
    }
    assert layers == {"buildings"}
