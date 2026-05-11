"""Per-dataset metadata report."""

import duckdb
import pytest

from oex.metadata import compute_metadata


@pytest.fixture
def populated_table() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial")
    conn.execute(
        """
        CREATE TABLE features AS SELECT * FROM (VALUES
            (1, 'A',  'cafe',     ST_Point(83.5, 28.5)),
            (2, 'B',  'cafe',     ST_Point(83.6, 28.6)),
            (3, 'C',  NULL,       ST_Point(83.7, 28.7)),
            (4, NULL, 'restaurant', ST_GeomFromText('POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))')),
            (5, 'E',  'bar',      ST_GeomFromText('LINESTRING(0 0, 1 1)'))
        ) AS t(id, name, amenity, geom)
        """
    )
    return conn


def test_metadata_basic_counts(populated_table: duckdb.DuckDBPyConnection) -> None:
    report = compute_metadata(populated_table, "features")
    assert report.feature_count == 5
    assert report.geometry_types == {"POINT": 3, "POLYGON": 1, "LINESTRING": 1}
    assert report.bbox is not None
    minx, miny, maxx, maxy = report.bbox
    assert minx == 0.0 and maxx >= 83.7
    assert miny == 0.0 and maxy >= 28.7


def test_metadata_per_column_null_share(populated_table: duckdb.DuckDBPyConnection) -> None:
    report = compute_metadata(populated_table, "features")
    cols = {c.name: c for c in report.columns}
    assert "geom" not in cols, "geometry column must not appear in column report"

    assert cols["id"].null_count == 0
    assert cols["id"].null_percent == 0.0
    assert cols["id"].distinct_count == 5

    assert cols["name"].null_count == 1
    assert cols["name"].null_percent == 20.0

    assert cols["amenity"].null_count == 1
    assert cols["amenity"].distinct_count == 3


def test_metadata_top_values(populated_table: duckdb.DuckDBPyConnection) -> None:
    report = compute_metadata(populated_table, "features")
    cols = {c.name: c for c in report.columns}
    amenity_top = cols["amenity"].top_values
    assert amenity_top[0]["value"] == "cafe"
    assert amenity_top[0]["count"] == 2


def test_metadata_summary_string(populated_table: duckdb.DuckDBPyConnection) -> None:
    report = compute_metadata(populated_table, "features")
    assert "5 features" in report.summary
    assert "POINT" in report.summary


def test_metadata_empty_table() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial")
    conn.execute("CREATE TABLE empty (id INT, name VARCHAR, geom GEOMETRY)")
    report = compute_metadata(conn, "empty")
    assert report.feature_count == 0
    assert report.bbox is None
    assert report.summary == "Empty dataset."


def test_metadata_temporal_bounds_when_column_exists() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial")
    conn.execute(
        """
        CREATE TABLE features AS SELECT * FROM (VALUES
            (1, TIMESTAMP '2023-01-15 10:00:00', ST_Point(1, 1)),
            (2, TIMESTAMP '2024-06-30 12:30:00', ST_Point(2, 2)),
            (3, TIMESTAMP '2025-12-01 08:00:00', ST_Point(3, 3)),
            (4, NULL,                            ST_Point(4, 4))
        ) AS t(id, update_time, geom)
        """
    )
    report = compute_metadata(conn, "features", temporal_column="update_time")
    assert report.temporal is not None
    assert report.temporal.column == "update_time"
    assert report.temporal.min is not None and report.temporal.min.startswith("2023-01-15")
    assert report.temporal.max is not None and report.temporal.max.startswith("2025-12-01")
    assert report.temporal.non_null_count == 3


def test_metadata_temporal_skipped_when_column_missing() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial")
    conn.execute(
        "CREATE TABLE features AS SELECT 1 AS id, ST_Point(1, 1) AS geom",
    )
    report = compute_metadata(conn, "features", temporal_column="does_not_exist")
    assert report.temporal is None


def test_metadata_temporal_skipped_when_column_not_requested() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial")
    conn.execute(
        """
        CREATE TABLE features AS SELECT 1 AS id,
            TIMESTAMP '2024-01-01 00:00:00' AS update_time,
            ST_Point(1, 1) AS geom
        """
    )
    report = compute_metadata(conn, "features")
    assert report.temporal is None
