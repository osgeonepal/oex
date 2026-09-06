"""GeoParquet is published unzipped, pointing at the layer object already on S3."""

from oex.config.schema import CategoryConfig
from oex.hdx_publisher import (
    _layer_parquet_slug,
    _resource_slug,
    geoparquet_resource_payload,
)

URL = "https://bucket.s3.amazonaws.com/ISO3/NPL/_layers/osm/buildings.parquet"


def test_the_resource_points_at_the_staged_object_rather_than_a_copy():
    """Uploading again under the category folder would store the same bytes twice."""
    payload = geoparquet_resource_payload(CategoryConfig(name="buildings"), URL, "osm")
    assert payload["url"] == URL
    assert payload["format"] == "geoparquet"


def test_the_name_carries_the_source_like_every_other_resource():
    assert (
        geoparquet_resource_payload(CategoryConfig(name="buildings"), URL, "osm")["name"]
        == "Buildings (OSM), GeoParquet"
    )
    assert (
        geoparquet_resource_payload(CategoryConfig(name="buildings"), URL, "overture")["name"]
        == "Buildings (Overture), GeoParquet"
    )


def test_a_staged_parquet_is_matched_back_to_its_category_and_source():
    """Its key carries no dataset prefix, so the filename parser alone never sees it."""
    assert _layer_parquet_slug({"url": URL}) == ("buildings", "osm")


def test_a_parquet_from_an_unknown_source_is_not_claimed():
    url = URL.replace("/_layers/osm/", "/_layers/somethingelse/")
    assert _layer_parquet_slug({"url": url}) is None


def test_a_zip_is_not_mistaken_for_a_staged_parquet():
    assert _layer_parquet_slug({"url": "https://b/ISO3/NPL/buildings/x_osm_gpkg.zip"}) is None


def test_prune_can_see_a_geoparquet_resource():
    """Without this a dropped layer keeps a GeoParquet pointing at a stale object."""
    assert _resource_slug({"url": URL}, "hot_npl") == ("buildings", "osm")
