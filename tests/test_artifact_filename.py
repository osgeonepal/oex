"""The artifact filename is parsed back into (slug, source, format) in three places."""

import pytest

from oex.hdx_publisher import _split_artifact_filename

CASES = [
    # zipped: the format is the last stem segment
    ("buildings_osm_gpkg.zip", ("buildings", "osm", "gpkg")),
    ("buildings_overture_geojson.zip", ("buildings", "overture", "geojson")),
    # output.s3.name_include_source: false drops the source token
    ("buildings_gpkg.zip", ("buildings", "", "gpkg")),
    # split_by_geometry puts a geometry segment where the source would sit
    ("buildings_polygons_gpkg.zip", ("buildings_polygons", "", "gpkg")),
    ("buildings_polygons_osm_gpkg.zip", ("buildings_polygons", "osm", "gpkg")),
    # unzipped: the format is the extension
    ("buildings_osm.geojson", ("buildings", "osm", "geojson")),
    ("buildings_osm.gpkg", ("buildings", "osm", "gpkg")),
    ("buildings_polygons_osm.geojson", ("buildings_polygons", "osm", "geojson")),
    ("buildings.geojson", ("buildings", "", "geojson")),
]


@pytest.mark.parametrize(("filename", "expected"), CASES)
def test_the_source_token_is_recognised_by_name_not_position(filename, expected):
    assert _split_artifact_filename(filename) == expected


def test_a_category_named_like_a_source_is_not_mistaken_for_one():
    assert _split_artifact_filename("roads_osm_osm_shp.zip") == ("roads_osm", "osm", "shp")
    assert _split_artifact_filename("roads_osm_osm.geojson") == ("roads_osm", "osm", "geojson")


def test_zipped_and_unzipped_of_the_same_layer_agree():
    """`metadata --prune` matches resources by this, so the two shapes must not diverge."""
    assert _split_artifact_filename("buildings_osm_geojson.zip") == _split_artifact_filename(
        "buildings_osm.geojson"
    )


NON_LAYERS = ["aoi.geojson", "overview.html", "osm_metadata.json"]


@pytest.mark.parametrize("filename", NON_LAYERS)
def test_a_non_layer_artifact_reports_no_source(filename):
    """An empty source is how the callers tell a layer from the AOI, report and metadata."""
    _, source, _ = _split_artifact_filename(filename)
    assert source == ""


def test_the_file_source_is_a_recognised_source():
    """Without this its resources parse with `file` glued onto the slug."""
    assert _split_artifact_filename("bridge_damage_file.geojson") == (
        "bridge_damage",
        "file",
        "geojson",
    )
    assert _split_artifact_filename("bridge_damage_file_gpkg.zip") == (
        "bridge_damage",
        "file",
        "gpkg",
    )


def test_a_category_named_like_a_source_is_settled_by_the_config():
    """`roads_osm.geojson` is ambiguous: category roads_osm, or roads from osm?"""
    assert _split_artifact_filename("roads_osm.geojson", {"roads_osm"}) == (
        "roads_osm",
        "",
        "geojson",
    )
    assert _split_artifact_filename("roads_osm.geojson", {"roads"}) == ("roads", "osm", "geojson")


def test_without_the_config_the_source_reading_wins():
    """Every caller passes its categories; this is only the shape of the fallback."""
    assert _split_artifact_filename("roads_osm.geojson") == ("roads", "osm", "geojson")
