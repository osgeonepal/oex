"""SELECT/WHERE clause builders."""

import duckdb
import pytest

from oex.boundary import Boundary
from oex.sql import build_select_clause, build_where_clause, sql_identifier, sql_string

TRIANGLE = Boundary(
    iso3="NPL",
    bbox=(83.0, 28.0, 84.0, 29.0),
    geojson='{"type":"Polygon","coordinates":[[[83,28],[84,28],[83,29],[83,28]]]}',
    source="test",
)


def test_select_clause_appends_geometry_as_geom_after_the_fields() -> None:
    clause = build_select_clause(["id", "names.primary AS name"])
    assert [field.strip() for field in clause.split(",")] == [
        "id",
        "names.primary AS name",
        "geometry AS geom",
    ]


def test_select_clause_with_empty_fields_still_emits_geom() -> None:
    assert build_select_clause([]) == "geometry AS geom"


@pytest.mark.parametrize("bbox_mode", ["bbox", "geom"])
def test_where_clause_keeps_only_rows_inside_the_boundary_on_earth_matching_the_conditions(
    spatial_conn: duckdb.DuckDBPyConnection, bbox_mode: str
) -> None:
    """Row 2 sits inside the bbox but outside the triangle; row 5 runs off the Earth."""
    spatial_conn.execute(
        """
        CREATE TABLE features AS
        SELECT id, class, geometry,
               {xmin: ST_XMin(geometry), ymin: ST_YMin(geometry),
                xmax: ST_XMax(geometry), ymax: ST_YMax(geometry)} AS bbox
        FROM (VALUES
            (1, 'primary', ST_Point(83.2, 28.2)),
            (2, 'primary', ST_Point(83.9, 28.9)),
            (3, 'primary', ST_Point(85.0, 28.5)),
            (4, 'secondary', ST_Point(83.2, 28.2)),
            (5, 'primary', ST_GeomFromText('LINESTRING(83.2 28.2, 83.2 115)'))
        ) AS t(id, class, geometry)
        """
    )
    where = build_where_clause(TRIANGLE, ["class = 'primary'", ""], bbox_mode=bbox_mode)
    rows = spatial_conn.execute(f"SELECT id FROM features WHERE {where} ORDER BY id").fetchall()
    assert rows == [(1,)]


def test_sql_string_round_trips_a_value_with_single_quotes_through_duckdb() -> None:
    value = "O'Brien's ''road''"
    row = duckdb.connect().execute(f"SELECT {sql_string(value)}").fetchone()
    assert row == (value,)


def test_sql_identifier_round_trips_a_column_name_with_double_quotes_through_duckdb() -> None:
    name = 'name "local"'
    conn = duckdb.connect()
    conn.execute(f"CREATE TABLE t ({sql_identifier(name)} VARCHAR)")
    assert [column[0] for column in conn.execute("SELECT * FROM t").description] == [name]


def test_a_materialised_layer_has_no_blank_text(spatial_conn) -> None:
    from oex.sql import materialise

    spatial_conn.execute(
        "CREATE TABLE src AS SELECT * FROM (VALUES ('', 'Bidur ', 3), (chr(9), 'x', 4)) "
        "AS t(address, city, levels)"
    )
    materialise(spatial_conn, "layer", "src", "address, city, levels", "levels > 0")
    assert spatial_conn.execute("SELECT * FROM layer ORDER BY levels").fetchall() == [
        (None, "Bidur", 3),
        (None, "x", 4),
    ]
