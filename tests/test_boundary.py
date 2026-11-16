import json
import math

import duckdb
import pytest
from shapely.geometry import Point, mapping, shape

from oex.boundary import area_sq_km, resolve_boundary
from oex.config.schema import BoundaryConfig


def test_a_user_featurecollection_resolves_to_its_bbox_and_user_source() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[80, 27], [85, 27], [85, 30], [80, 30], [80, 27]]],
                },
            }
        ],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc))
    boundary = resolve_boundary("NPL", cfg)
    assert boundary.iso3 == "NPL"
    assert boundary.bbox == (80, 27, 85, 30)
    assert boundary.source == "user-provided"


def test_resolve_world_shorthand_returns_full_globe() -> None:
    cfg = BoundaryConfig(geom="world")
    boundary = resolve_boundary("WLD", cfg)
    assert boundary.iso3 == "WLD"
    assert boundary.bbox == (-180.0, -90.0, 180.0, 90.0)
    assert "whole planet" in boundary.source


def test_resolve_caches_repeat_calls() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]],
                },
            }
        ],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc))
    a = resolve_boundary("ABC", cfg)
    b = resolve_boundary("ABC", cfg)
    assert a is b


def test_buffer_widens_bbox_and_changes_cache_key() -> None:
    fc = {
        "type": "Polygon",
        # 1 deg square at the equator: ~111 km wide.
        "coordinates": [[[10, 0], [11, 0], [11, 1], [10, 1], [10, 0]]],
    }
    base_cfg = BoundaryConfig(geom=json.dumps(fc))
    base = resolve_boundary("BUF", base_cfg)

    buffered_cfg = BoundaryConfig(geom=json.dumps(fc), buffer_meters=1000.0)
    buffered = resolve_boundary("BUF", buffered_cfg)

    assert buffered.bbox[0] < base.bbox[0]
    assert buffered.bbox[1] < base.bbox[1]
    assert buffered.bbox[2] > base.bbox[2]
    assert buffered.bbox[3] > base.bbox[3]

    # 1 km at the equator is about 0.009 degrees; the bounds catch a metres/degrees mixup.
    dx = buffered.bbox[2] - base.bbox[2]
    dy = buffered.bbox[3] - base.bbox[3]
    assert 0.005 < dx < 0.02
    assert 0.005 < dy < 0.02

    # The README shows the source, so the buffer has to be visible there.
    assert "buffered" in buffered.source
    assert "+1000m" in buffered.source

    assert buffered is not base


def test_a_zero_buffer_leaves_bbox_and_source_unchanged() -> None:
    fc = {
        "type": "Polygon",
        "coordinates": [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc), buffer_meters=0.0)
    boundary = resolve_boundary("ZRO", cfg)
    assert boundary.bbox == (20.0, 20.0, 21.0, 21.0)
    assert "buffered" not in boundary.source


def test_negative_buffer_meters_raises() -> None:
    fc = {
        "type": "Polygon",
        "coordinates": [[[30, 30], [31, 30], [31, 31], [30, 31], [30, 30]]],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc), buffer_meters=-100.0)
    with pytest.raises(ValueError, match="buffer_meters must be >= 0"):
        resolve_boundary("NEG", cfg)


def test_user_geom_resolves_without_an_iso3() -> None:
    """Sub-national exports (Tasking Manager projects) carry a polygon, not a country."""
    geom = {
        "type": "Polygon",
        "coordinates": [[[80, 27], [85, 27], [85, 30], [80, 30], [80, 27]]],
    }
    boundary = resolve_boundary("", BoundaryConfig(geom=json.dumps(geom)))
    assert boundary.iso3 == ""
    assert boundary.bbox == (80, 27, 85, 30)
    assert boundary.source == "user-provided"


def test_missing_iso3_without_a_geom_still_raises() -> None:
    with pytest.raises(ValueError, match="iso3"):
        resolve_boundary("", BoundaryConfig(geom=None))


def test_buffer_distance_is_true_on_the_ground_not_in_mercator() -> None:
    """EPSG:3857 metres are inflated by 1/cos(latitude), so buffering there would
    fall roughly 12% short at this latitude."""
    from pyproj import Geod
    from shapely.geometry import Point, shape

    from oex.boundary import Boundary, _buffered

    centre = Point(85.0, 28.0)
    boundary = Boundary(
        iso3="NPL",
        bbox=centre.buffer(0.01).bounds,
        geojson=json.dumps(mapping(centre.buffer(0.01))),
        source="test",
    )
    widened = shape(json.loads(_buffered(boundary, 200.0, 0.0).geojson))

    geod = Geod(ellps="WGS84")
    before = abs(geod.geometry_area_perimeter(shape(json.loads(boundary.geojson)))[0])
    after = abs(geod.geometry_area_perimeter(widened)[0])
    # A disc grown by 200 m: compare the implied radii rather than the areas.
    radius_before = math.sqrt(before / math.pi)
    radius_after = math.sqrt(after / math.pi)
    assert 195 < (radius_after - radius_before) < 205


def test_a_buffer_across_180_degrees_reaches_its_distance_on_both_sides() -> None:
    """Islands either side of the line, as in Fiji: a planar centroid would centre the
    projection near Greenwich, and the reprojected rings would wrap the globe."""
    islands = {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [
                    [179.90, -17.01],
                    [179.91, -17.01],
                    [179.91, -17.0],
                    [179.90, -17.0],
                    [179.90, -17.01],
                ]
            ],
            [
                [
                    [-179.91, -17.01],
                    [-179.90, -17.01],
                    [-179.90, -17.0],
                    [-179.91, -17.0],
                    [-179.91, -17.01],
                ]
            ],
        ],
    }
    boundary = resolve_boundary(
        "FJX", BoundaryConfig(geom=json.dumps(islands), buffer_meters=10000.0)
    )
    widened = shape(json.loads(boundary.geojson))
    km_in_degrees = 1 / (111.32 * math.cos(math.radians(17.005)))

    assert widened.covers(Point(-179.90 + 9 * km_in_degrees, -17.005))
    assert widened.covers(Point(179.90 - 9 * km_in_degrees, -17.005))
    assert not widened.covers(Point(-179.90 + 11 * km_in_degrees, -17.005))
    assert not widened.covers(Point(0.0, -17.005))
    # Each square of about 1.06 by 1.11 km gains 10 km strips along its sides and quarter
    # discs at its corners; the two widened squares barely overlap.
    one_square = 1.06 * 1.11 + 2 * (1.06 + 1.11) * 10 + math.pi * 10**2
    assert area_sq_km(mapping(widened)) == pytest.approx(2 * one_square, rel=0.02)


_JAGGED = {
    "type": "Polygon",
    "coordinates": [
        [
            [
                85.0 + 0.05 * math.cos(i * math.pi / 180) * (1 + 0.1 * (i % 2)),
                28.0 + 0.05 * math.sin(i * math.pi / 180) * (1 + 0.1 * (i % 2)),
            ]
            for i in range(360)
        ]
        + [[85.0 + 0.05, 28.0]]
    ],
}


def test_simplify_cuts_vertices_and_still_covers_the_land() -> None:
    def outline(simplify_meters: float):
        cfg = BoundaryConfig(
            geom=json.dumps(_JAGGED), buffer_meters=1000.0, simplify_meters=simplify_meters
        )
        return shape(json.loads(resolve_boundary("JAG", cfg).geojson))

    exact, simplified = outline(0.0), outline(400.0)

    assert len(simplified.exterior.coords) < len(exact.exterior.coords) / 4
    assert simplified.covers(shape(_JAGGED))


@pytest.mark.parametrize(
    ("buffer_meters", "simplify_meters"), [(1000.0, -1.0), (0.0, 500.0), (1000.0, 500.0)]
)
def test_simplify_must_be_positive_and_follow_a_buffer(
    buffer_meters: float, simplify_meters: float
) -> None:
    cfg = BoundaryConfig(
        geom=json.dumps(_JAGGED), buffer_meters=buffer_meters, simplify_meters=simplify_meters
    )
    with pytest.raises(ValueError, match="simplify_meters"):
        resolve_boundary("JAG", cfg)


_DEGREE_SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


def test_a_degree_square_at_the_equator_is_measured_on_the_ellipsoid() -> None:
    assert area_sq_km(_DEGREE_SQUARE) == pytest.approx(12308.78, abs=0.01)


def test_a_hole_is_removed_whatever_its_winding() -> None:
    """Drawn boundaries do not reliably follow the GeoJSON right-hand rule."""
    hole_same_winding = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
            [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75], [0.25, 0.25]],
        ],
    }
    assert area_sq_km(hole_same_winding) == pytest.approx(9231.61, abs=0.01)


def test_every_part_of_a_multipolygon_counts() -> None:
    two_squares = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]],
        ],
    }
    assert area_sq_km(two_squares) == pytest.approx(2 * 12308.78, abs=0.02)


@pytest.fixture
def fieldmaps_parquet(tmp_path) -> str:
    path = tmp_path / "adm0_polygons.parquet"
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial")
    conn.execute(
        f"""
        COPY (
            SELECT iso_3, iso_3_grp, status_nm, wld_update, ST_GeomFromText(wkt) AS geometry
            FROM (VALUES
                ('AAA', 'AAA', 'Member State', DATE '2025-07-29',
                 'POLYGON((0 0, 4 0, 4 4, 0 4, 0 0), (1 1, 2 1, 2 2, 1 2, 1 1))'),
                ('AAA', 'AAA', 'Member State', DATE '2025-07-29',
                 'POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))'),
                ('BBB', 'BBB', 'Member State', DATE '2025-07-29',
                 'POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))'),
                ('DDD', 'DDD', 'Member State', NULL, 'POLYGON((20 0, 21 0, 21 1, 20 1, 20 0))'),
                ('XTO', 'XTO', 'Sovereignty unsettled', DATE '2025-07-29',
                 'POLYGON((4 0, 6 0, 6 4, 4 4, 4 0))'),
                ('XIS', 'AAA', 'Sovereignty unsettled', DATE '2025-07-29',
                 'POLYGON((30 0, 31 0, 31 1, 30 1, 30 0))'),
                ('XFA', 'XFA', 'Sovereignty unsettled', DATE '2025-07-29',
                 'POLYGON((40 0, 41 0, 41 1, 40 1, 40 0))')
            ) AS rows(iso_3, iso_3_grp, status_nm, wld_update, wkt)
        ) TO '{path}' (FORMAT parquet)
        """
    )
    return str(path)


def test_fieldmaps_joins_every_unit_of_the_country_and_leaves_an_enclaved_one_out(
    fieldmaps_parquet,
) -> None:
    boundary = resolve_boundary(
        "aaa", BoundaryConfig(source="fieldmaps", fieldmaps_url=fieldmaps_parquet)
    )
    outline = shape(json.loads(boundary.geojson))
    assert not outline.contains(Point(1.5, 1.5))
    assert outline.contains(Point(10.5, 0.5))
    assert (
        boundary.source
        == "FieldMaps, U.S. Department of State, OpenStreetMap (adm0 intl, 2025-07-29)"
    )


def test_fieldmaps_adds_the_disputed_areas_the_country_touches_or_is_grouped_with(
    fieldmaps_parquet,
) -> None:
    boundary = resolve_boundary(
        "AAA", BoundaryConfig(source="fieldmaps", fieldmaps_url=fieldmaps_parquet)
    )
    outline = shape(json.loads(boundary.geojson))
    assert outline.contains(Point(5, 2))
    assert outline.contains(Point(30.5, 0.5))
    assert not outline.contains(Point(40.5, 0.5))
    assert boundary.bbox == (0, 0, 31, 4)


def test_fieldmaps_rows_without_a_release_date_are_refused(fieldmaps_parquet) -> None:
    with pytest.raises(ValueError, match="no wld_update date"):
        resolve_boundary("DDD", BoundaryConfig(source="fieldmaps", fieldmaps_url=fieldmaps_parquet))


def test_the_boundary_cache_keeps_sources_apart(fieldmaps_parquet, monkeypatch) -> None:
    resolve_boundary("AAA", BoundaryConfig(source="fieldmaps", fieldmaps_url=fieldmaps_parquet))

    def refuse_network(*_args, **_kwargs):
        raise ConnectionError("geoBoundaries reached")

    monkeypatch.setattr("oex.boundary.requests.get", refuse_network)
    with pytest.raises(ConnectionError, match="geoBoundaries reached"):
        resolve_boundary(
            "AAA", BoundaryConfig(source="geoboundaries", fieldmaps_url=fieldmaps_parquet)
        )


def test_a_country_missing_from_fieldmaps_is_refused(fieldmaps_parquet) -> None:
    with pytest.raises(ValueError, match="fieldmaps has no adm0 for CCC"):
        resolve_boundary("CCC", BoundaryConfig(source="fieldmaps", fieldmaps_url=fieldmaps_parquet))


def test_an_inline_geom_wins_over_the_fieldmaps_source(tmp_path) -> None:
    square = {"type": "Polygon", "coordinates": [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]]}
    cfg = BoundaryConfig(
        geom=json.dumps(square), source="fieldmaps", fieldmaps_url=str(tmp_path / "absent.parquet")
    )
    assert resolve_boundary("AAA", cfg).source == "user-provided"


def test_an_unknown_boundary_source_is_refused() -> None:
    with pytest.raises(ValueError, match="boundary.source"):
        resolve_boundary("AAA", BoundaryConfig(source="naturalearth"))


def test_the_boundary_cache_keeps_the_disputed_switch_apart(fieldmaps_parquet) -> None:
    joined = BoundaryConfig(source="fieldmaps", fieldmaps_url=fieldmaps_parquet)
    alone = BoundaryConfig(
        source="fieldmaps", fieldmaps_url=fieldmaps_parquet, fieldmaps_join_disputed=False
    )
    resolve_boundary("AAA", joined)
    assert not shape(json.loads(resolve_boundary("AAA", alone).geojson)).contains(Point(5, 2))


def test_fieldmaps_leaves_disputed_areas_out_when_joining_is_off(fieldmaps_parquet) -> None:
    cfg = BoundaryConfig(
        source="fieldmaps", fieldmaps_url=fieldmaps_parquet, fieldmaps_join_disputed=False
    )
    outline = shape(json.loads(resolve_boundary("AAA", cfg).geojson))
    assert not outline.contains(Point(5, 2))
    assert not outline.contains(Point(30.5, 0.5))
