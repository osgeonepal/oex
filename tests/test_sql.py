"""SELECT/WHERE clause builders."""

from oex.boundary import Boundary
from oex.sql import build_select_clause, build_where_clause


def _boundary() -> Boundary:
    return Boundary(
        iso3="NPL",
        bbox=(83.0, 28.0, 84.0, 29.0),
        geojson='{"type":"Polygon","coordinates":[[[83,28],[84,28],[84,29],[83,29],[83,28]]]}',
        source="test",
    )


def test_select_clause_appends_geom() -> None:
    clause = build_select_clause(["id", "names.primary AS name"])
    assert "geometry AS geom" in clause
    assert clause.startswith("id")


def test_select_clause_with_empty_fields_still_emits_geom() -> None:
    assert build_select_clause([]) == "geometry AS geom"


def test_where_clause_includes_bbox_and_intersect() -> None:
    where = build_where_clause(_boundary(), [], bbox_cols="bbox")
    assert "bbox.xmin" in where
    assert "ST_Intersects" in where


def test_where_clause_geom_fallback() -> None:
    where = build_where_clause(_boundary(), ["class = 'primary'"], bbox_cols="geom")
    assert "ST_XMin(geometry)" in where
    assert "(class = 'primary')" in where
