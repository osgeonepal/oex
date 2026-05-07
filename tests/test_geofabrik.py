"""Geofabrik index lookup with mocked HTTP."""

import json
from typing import Any
from unittest.mock import patch

import pytest

from oex.osm import geofabrik


def _mock_index() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {
                    "id": "nepal",
                    "parent": "asia",
                    "iso3166-1:alpha2": ["NP"],
                    "name": "Nepal",
                    "urls": {
                        "pbf": "https://download.geofabrik.de/asia/nepal-latest.osm.pbf",
                    },
                }
            },
            {
                "properties": {
                    "id": "us",
                    "parent": "north-america",
                    "iso3166-1:alpha2": ["US"],
                    "name": "USA",
                    "urls": {
                        "pbf": "https://download.geofabrik.de/north-america/us-latest.osm.pbf",
                    },
                }
            },
            {
                "properties": {
                    "id": "us/california",
                    "parent": "us",
                    "iso3166-2": ["US-CA"],
                    "iso3166-1:alpha2": ["US"],
                    "name": "California",
                    "urls": {
                        "pbf": "https://download.geofabrik.de/north-america/us/california-latest.osm.pbf",
                    },
                }
            },
        ],
    }


@pytest.fixture(autouse=True)
def reset_index_cache() -> None:
    geofabrik._index_cache.clear()


@patch("oex.osm.geofabrik.requests.get")
def test_lookup_country_nepal(mock_get) -> None:
    mock_get.return_value.raise_for_status = lambda: None
    mock_get.return_value.json = lambda: _mock_index()
    extract = geofabrik.lookup_country("NPL")
    assert extract.iso3 == "NPL"
    assert extract.iso2 == "NP"
    assert extract.geofabrik_id == "nepal"
    assert extract.pbf_url.endswith("nepal-latest.osm.pbf")
    assert extract.md5_url == extract.pbf_url + ".md5"


@patch("oex.osm.geofabrik.requests.get")
def test_lookup_country_prefers_country_over_state(mock_get) -> None:
    mock_get.return_value.raise_for_status = lambda: None
    mock_get.return_value.json = lambda: _mock_index()
    extract = geofabrik.lookup_country("USA")
    assert extract.geofabrik_id == "us"
    assert "us/california" not in extract.pbf_url


def test_lookup_country_unknown_iso3() -> None:
    with pytest.raises(geofabrik.GeofabrikLookupError):
        geofabrik.lookup_country("XYZ")


@patch("oex.osm.geofabrik.requests.get")
def test_lookup_country_no_geofabrik_extract(mock_get) -> None:
    mock_get.return_value.raise_for_status = lambda: None
    mock_get.return_value.json = lambda: {"type": "FeatureCollection", "features": []}
    with pytest.raises(geofabrik.GeofabrikLookupError):
        geofabrik.lookup_country("NPL")


@patch("oex.osm.geofabrik.requests.get")
def test_index_cached_per_url(mock_get) -> None:
    payload = _mock_index()
    mock_get.return_value.raise_for_status = lambda: None
    mock_get.return_value.json = lambda: payload
    geofabrik.lookup_country("NPL")
    geofabrik.lookup_country("NPL")
    # Cached after first call: only one HTTP request.
    assert mock_get.call_count == 1


@patch("oex.osm.geofabrik.requests.get")
def test_index_rejects_non_featurecollection(mock_get) -> None:
    mock_get.return_value.raise_for_status = lambda: None
    mock_get.return_value.json = lambda: {"type": "Feature"}
    with pytest.raises(RuntimeError):
        geofabrik.lookup_country("NPL")
    # Sanity: payload not used elsewhere
    json.dumps({"ok": True})
