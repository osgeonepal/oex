"""The artifact stem is parsed back into (slug, source, format) in three places."""

import pytest

from oex.hdx_publisher import _split_artifact_stem

CASES = [
    ("buildings_osm_gpkg", ("buildings", "osm", "gpkg")),
    ("buildings_overture_geojson", ("buildings", "overture", "geojson")),
    # output.s3.name_include_source: false drops the source token
    ("buildings_gpkg", ("buildings", "", "gpkg")),
    # split_by_geometry puts a geometry segment where the source would sit
    ("buildings_polygons_gpkg", ("buildings_polygons", "", "gpkg")),
    ("buildings_polygons_osm_gpkg", ("buildings_polygons", "osm", "gpkg")),
]


@pytest.mark.parametrize(("stem", "expected"), CASES)
def test_the_source_token_is_recognised_by_name_not_position(stem, expected):
    assert _split_artifact_stem(stem) == expected


def test_a_category_named_like_a_source_is_not_mistaken_for_one():
    assert _split_artifact_stem("roads_osm_osm_shp") == ("roads_osm", "osm", "shp")
