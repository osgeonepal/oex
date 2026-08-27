"""Tests for reading `*_metadata.json` resources off an HDX dataset."""

from typing import Any
from unittest.mock import patch

from oex.config.schema import CategoryConfig
from oex.hdx_publisher import (
    _load_source_metadata,
    _metadata_entries,
    _slug_from_metadata_name,
)
from oex.metadata import ColumnReport, MetadataReport
from oex.report import SourceMetadata

PREFIX = "hot_flood_npl_"
CATEGORIES = [CategoryConfig(name="buildings"), CategoryConfig(name="points_of_interest")]


def _payload(source_name: str, category: str) -> dict[str, Any]:
    source = SourceMetadata(
        source_name=source_name,
        snapshot_label="2026-08-24",
        dataset_source="OpenStreetMap",
        generated_utc="2026-08-24T00:00:00Z",
        oex_version="0.4.10",
        license_label="ODbL 1.0",
        license_url=None,
        pcode_source_date=None,
        boundary="user geom",
        metadata=MetadataReport(
            feature_count=3,
            geometry_types={"POLYGON": 3},
            bbox=(85.0, 27.8, 85.4, 28.3),
            columns=[ColumnReport("id", "VARCHAR", 0, 0.0, 3, [])],
            summary="3 features.",
            temporal=None,
        ),
    )
    return {"category": category, **source.to_payload()}


def _envelope() -> dict[str, Any]:
    return {
        "dataset": "hot_flood_npl",
        "layers": [
            _payload("osm", "buildings"),
            _payload("overture", "points_of_interest"),
        ],
    }


def test_metadata_entries_passes_through_a_single_payload() -> None:
    """A per-category resource yields itself under its own name."""
    name = "hot_flood_npl_buildings_osm_metadata.json"
    payload = _payload("osm", "buildings")

    assert _metadata_entries(name, payload) == [(name, payload)]


def test_metadata_entries_expands_a_combined_envelope() -> None:
    """A combined dataset's envelope yields one entry per layer it describes."""
    entries = _metadata_entries("hot_flood_npl_metadata.json", _envelope())

    assert [name for name, _ in entries] == [
        "hot_flood_npl_buildings_osm_metadata.json",
        "hot_flood_npl_points_of_interest_overture_metadata.json",
    ]


def test_expanded_entry_names_resolve_to_their_category_slug() -> None:
    """The synthesised names are the form the slug matcher reads, underscores included."""
    entries = _metadata_entries("hot_flood_npl_metadata.json", _envelope())

    slugs = [_slug_from_metadata_name(name, PREFIX, CATEGORIES) for name, _ in entries]
    assert slugs == ["buildings", "points_of_interest"]


def test_load_source_metadata_reads_a_combined_envelope() -> None:
    """The envelope is not a source payload, so parsing it whole raised KeyError."""
    resources = [
        {
            "name": "Layer Metadata, JSON",
            "url": "https://example.invalid/hot_flood_npl_metadata.json",
        }
    ]

    with patch("oex.hdx_publisher._download_json", return_value=_envelope()):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [source.source_name for _, source in loaded] == ["osm", "overture"]


def test_expanded_names_ignore_the_resource_filename() -> None:
    """Per-source metadata resources must still resolve to their category slug.

    A combined dataset names one metadata resource per source, so the entry names
    come from the envelope rather than from the file carrying it.
    """
    entries = _metadata_entries("hot_flood_npl_overture_metadata.json", _envelope())

    slugs = [_slug_from_metadata_name(name, PREFIX, CATEGORIES) for name, _ in entries]
    assert slugs == ["buildings", "points_of_interest"]


def test_both_source_envelopes_contribute_their_layers() -> None:
    """Each source keeps its own resource, so the pair covers every layer."""
    resources = [
        {
            "name": "Layer Metadata (OSM), JSON",
            "url": "https://example.invalid/hot_flood_npl_osm_metadata.json",
        },
        {
            "name": "Layer Metadata (Overture), JSON",
            "url": "https://example.invalid/hot_flood_npl_overture_metadata.json",
        },
    ]
    osm = {"dataset": "hot_flood_npl", "layers": [_payload("osm", "buildings")]}
    overture = {"dataset": "hot_flood_npl", "layers": [_payload("overture", "buildings")]}

    with patch("oex.hdx_publisher._download_json", side_effect=[osm, overture]):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [source.source_name for _, source in loaded] == ["osm", "overture"]
    assert {_slug_from_metadata_name(n, PREFIX, CATEGORIES) for n, _ in loaded} == {"buildings"}


def test_resource_names_read_as_layer_and_source() -> None:
    """Resource names are what people scan on HDX, so they must not be filenames."""
    from oex.hdx_publisher import FORMAT_LABEL, RESOURCE_SOURCE, category_label

    category = CategoryConfig(name="points_of_interest")
    stem = "hot_flood_npl_points_of_interest_osm_gpkg"
    parts = stem.rsplit("_", 2)
    source, fmt = parts[-2], parts[-1]

    name = f"{category_label(category)} ({RESOURCE_SOURCE[source]}), {FORMAT_LABEL[fmt]}"
    assert name == "Points of Interest (OSM), GeoPackage"


def test_format_is_part_of_the_name() -> None:
    """Same-named resources differing only by format get collapsed by ResourceMatcher."""
    from oex.hdx_publisher import FORMAT_LABEL, RESOURCE_SOURCE, category_label

    category = CategoryConfig(name="buildings")
    names = {
        f"{category_label(category)} ({RESOURCE_SOURCE['osm']}), {FORMAT_LABEL[fmt]}"
        for fmt in ("gpkg", "shp", "kml", "geojson")
    }
    assert len(names) == 4


def test_license_ids_render_as_names() -> None:
    """HDX needs the licence id; the report must not show that id to readers."""
    from oex.config.schema import license_label

    assert license_label("hdx-odc-odbl") == "Open Database License (ODC-ODbL)"
    assert license_label("cc-by") == "Creative Commons Attribution 4.0"
    # Anything already human-readable, or unknown, passes through untouched.
    assert license_label("ODbL 1.0") == "ODbL 1.0"


def test_metadata_is_found_by_filename_not_display_name() -> None:
    """Resources carry human-readable names, so the filename is what identifies them."""
    resources = [
        {
            "name": "Layer Metadata (OSM), JSON",
            "url": "https://example.invalid/a/hot_flood_npl_osm_metadata.json",
        }
    ]
    with patch("oex.hdx_publisher._download_json", return_value=_envelope()):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [s.source_name for _, s in loaded] == ["osm", "overture"]


def test_unrelated_resources_are_ignored() -> None:
    """A renamed zip must not be mistaken for a metadata document."""
    resources = [
        {
            "name": "Buildings (OSM), GeoPackage",
            "url": "https://example.invalid/a/hot_flood_npl_buildings_osm_gpkg.zip",
        }
    ]
    assert _load_source_metadata(resources, "hot_flood_npl") == []


def test_tileset_is_found_by_filename_not_display_name() -> None:
    """The landing map looks up its tileset; renaming the resource must not hide it."""
    from oex.hdx_publisher import _resource_url

    resources = [
        {
            "name": "Vector Tiles, All Layers (PMTiles)",
            "url": "https://example.invalid/a/hot_flood_npl.pmtiles",
        }
    ]
    assert _resource_url(resources, "hot_flood_npl.pmtiles") == resources[0]["url"]
    assert _resource_url(resources, "other.pmtiles") is None


def test_slug_prefix_follows_a_custom_dataset_name() -> None:
    """`hdx.combined.name` can differ from key + iso3; resources follow the dataset name."""
    dt_name = "hot_flood_npl_corridor"  # key is hot_flood_corridor, so they differ
    envelope = {"dataset": dt_name, "layers": [_payload("osm", "buildings")]}
    entries = _metadata_entries(f"{dt_name}_osm_metadata.json", envelope)

    name = entries[0][0]
    assert name == f"{dt_name}_buildings_osm_metadata.json"
    assert (
        _slug_from_metadata_name(name, f"{dt_name}_", [CategoryConfig(name="buildings")])
        == "buildings"
    )
    # The old key+iso3 prefix would have matched nothing and dropped every panel.
    assert (
        _slug_from_metadata_name(
            name, "hot_flood_corridor_npl_", [CategoryConfig(name="buildings")]
        )
        is None
    )
