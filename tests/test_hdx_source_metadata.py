from typing import Any
from unittest.mock import patch

from oex.config.schema import BoundaryConfig, CategoryConfig, RootConfig
from oex.hdx_publisher import (
    _entry_name,
    _item_hrefs,
    _load_source_metadata,
    _resource_url,
    _slug_from_metadata_name,
)
from oex.metadata import ColumnReport, MetadataReport
from oex.report import SourceMetadata
from oex.stac import LayerAssets, build_collection, build_item, to_payload

PREFIX = "hot_flood_npl_"
CATEGORIES = [CategoryConfig(name="buildings"), CategoryConfig(name="points_of_interest")]
BASE = "https://example.invalid/a"


def _cfg() -> RootConfig:
    cfg = RootConfig(iso3="NPL", key="hot_flood", boundary=BoundaryConfig())
    cfg.output.s3.enabled = True
    cfg.output.s3.bucket = "example-bucket"
    cfg.output.s3.region = "us-east-1"
    return cfg


def _layer(source_name: str, category: str) -> LayerAssets:
    source = SourceMetadata(
        source_name=source_name,
        snapshot_label="2026-08-24",
        dataset_source="OpenStreetMap",
        generated_utc="2026-08-24T00:00:00Z",
        oex_version="0.4.10",
        license_label="Open Database License (ODC-ODbL)",
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
    return LayerAssets(
        category=CategoryConfig(name=category),
        source_metadata=source,
        dataset_name=f"hot_flood_npl_{category}",
    )


def _published(
    *pairs: tuple[str, str], dataset_name: str = "hot_flood_npl"
) -> list[dict[str, Any]]:
    """What a reader fetches: the Collection first, then the Item behind each item link."""
    layers = [_layer(source, category) for source, category in pairs]
    collection = build_collection(
        dataset_name=dataset_name,
        title="Nepal flood affected area data",
        description="Vector data for the flood affected area.",
        layers=layers,
        cfg=_cfg(),
    )
    items = [build_item(layer, _cfg()) for layer in layers]
    return [to_payload(collection), *(to_payload(item) for item in items)]


def test_the_collection_names_where_every_item_lives() -> None:
    """The reader walks rel=item; a Collection that names none describes nothing."""
    collection, *_ = _published(("osm", "buildings"), ("overture", "points_of_interest"))

    assert [href.rsplit("/", 1)[-1] for href in _item_hrefs(collection, f"{BASE}/c.json")] == [
        "hot_flood_npl_buildings_osm_item.json",
        "hot_flood_npl_points_of_interest_overture_item.json",
    ]


def test_an_entry_name_resolves_to_its_category_slug() -> None:
    """The dataset page matches layers by name; an unresolvable one drops a layer."""
    collection, *items = _published(("osm", "buildings"), ("overture", "points_of_interest"))
    names = [_entry_name(collection, item) for item in items]

    assert names == [
        "hot_flood_npl_buildings_osm_metadata.json",
        "hot_flood_npl_points_of_interest_overture_metadata.json",
    ]
    assert [_slug_from_metadata_name(n, PREFIX, CATEGORIES) for n in names] == [
        "buildings",
        "points_of_interest",
    ]


def test_entry_names_come_from_the_collection_not_the_resource_filename() -> None:
    """A combined dataset names one metadata resource per source, and the resource name
    is a human-readable title that carries no filename convention."""
    collection, item = _published(("overture", "buildings"))

    assert _entry_name(collection, item) == "hot_flood_npl_buildings_overture_metadata.json"


def test_a_custom_dataset_name_carries_into_the_entry_names() -> None:
    """`hdx.combined.name` can differ from key + iso3; resources follow the dataset name."""
    dt_name = "hot_flood_npl_corridor"  # key is hot_flood_corridor, so they differ
    collection, item = _published(("osm", "buildings"), dataset_name=dt_name)
    name = _entry_name(collection, item)

    assert name == f"{dt_name}_buildings_osm_metadata.json"
    assert (
        _slug_from_metadata_name(name, f"{dt_name}_", [CategoryConfig(name="buildings")])
        == "buildings"
    )
    # A key+iso3 prefix matches nothing and would drop every panel.
    assert (
        _slug_from_metadata_name(
            name, "hot_flood_corridor_npl_", [CategoryConfig(name="buildings")]
        )
        is None
    )


def test_load_source_metadata_follows_the_collection_to_its_items() -> None:
    """The Collection carries no layer detail, so a reader that stops there sees nothing."""
    resources = [
        {
            "name": "Layer Metadata, JSON",
            "url": f"{BASE}/hot_flood_npl_metadata.json",
        }
    ]
    fetched = _published(("osm", "buildings"), ("overture", "points_of_interest"))

    with patch("oex.hdx_publisher._download_json", side_effect=fetched):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [layer.source.source_name for layer in loaded] == ["osm", "overture"]
    assert [layer.source.metadata.feature_count for layer in loaded] == [3, 3]
    assert [_slug_from_metadata_name(layer.name, PREFIX, CATEGORIES) for layer in loaded] == [
        "buildings",
        "points_of_interest",
    ]


def test_every_source_on_a_dataset_contributes_its_layers() -> None:
    """Each source keeps its own resource, so the pair covers every layer."""
    resources = [
        {"name": "Layer Metadata (OSM), JSON", "url": f"{BASE}/hot_flood_npl_osm_metadata.json"},
        {
            "name": "Layer Metadata (Overture), JSON",
            "url": f"{BASE}/hot_flood_npl_overture_metadata.json",
        },
    ]
    fetched = [*_published(("osm", "buildings")), *_published(("overture", "buildings"))]

    with patch("oex.hdx_publisher._download_json", side_effect=fetched):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [layer.source.source_name for layer in loaded] == ["osm", "overture"]
    assert {_slug_from_metadata_name(layer.name, PREFIX, CATEGORIES) for layer in loaded} == {
        "buildings"
    }


def test_metadata_is_found_by_filename_not_display_name() -> None:
    """Resources carry human-readable names, so the filename is what identifies them."""
    resources = [
        {
            "name": "Layer Metadata (OSM), JSON",
            "url": f"{BASE}/hot_flood_npl_osm_metadata.json",
        }
    ]
    with patch("oex.hdx_publisher._download_json", side_effect=_published(("osm", "buildings"))):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [layer.source.source_name for layer in loaded] == ["osm"]


def test_unrelated_resources_are_ignored() -> None:
    """A renamed zip must not be mistaken for a metadata document."""
    resources = [
        {
            "name": "Buildings (OSM), GeoPackage",
            "url": f"{BASE}/hot_flood_npl_buildings_osm_gpkg.zip",
        }
    ]
    assert _load_source_metadata(resources, "hot_flood_npl") == []


def test_a_broken_metadata_resource_fails_the_publish_rather_than_dropping_layers() -> None:
    """A silently skipped resource publishes a report missing half the dataset."""
    import pytest

    resources = [{"name": "Layer Metadata, JSON", "url": f"{BASE}/hot_flood_npl_metadata.json"}]
    with patch("oex.hdx_publisher._download_json", return_value={"id": "x"}):
        with pytest.raises(RuntimeError, match="could not parse"):
            _load_source_metadata(resources, "hot_flood_npl")


def test_resource_names_read_as_layer_and_source() -> None:
    """Resource names are what people scan on HDX, so they must not be filenames."""
    from oex.hdx_publisher import FORMAT_LABEL, RESOURCE_SOURCE
    from oex.naming import category_label

    category = CategoryConfig(name="points_of_interest")
    stem = "hot_flood_npl_points_of_interest_osm_gpkg"
    parts = stem.rsplit("_", 2)
    source, fmt = parts[-2], parts[-1]

    name = f"{category_label(category)} ({RESOURCE_SOURCE[source]}), {FORMAT_LABEL[fmt]}"
    assert name == "Points of Interest (OSM), GeoPackage"


def test_format_is_part_of_the_name() -> None:
    """Same-named resources differing only by format get collapsed by ResourceMatcher."""
    from oex.hdx_publisher import FORMAT_LABEL, RESOURCE_SOURCE
    from oex.naming import category_label

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


def test_tileset_is_found_by_filename_not_display_name() -> None:
    """The dataset page map looks up its tileset; renaming the resource must not hide it."""
    resources = [
        {
            "name": "Vector Tiles, All Layers (PMTiles)",
            "url": "https://example.invalid/a/hot_flood_npl.pmtiles",
        }
    ]
    assert _resource_url(resources, "hot_flood_npl.pmtiles") == resources[0]["url"]
    assert _resource_url(resources, "other.pmtiles") is None


class _FakeResource(dict):
    pass


class _FakeDataset:
    """Just enough of the hdx Dataset surface for the ordering check."""

    def __init__(self, names):
        self._resources = [_FakeResource({"id": str(i), "name": n}) for i, n in enumerate(names)]
        self.reordered = None

    def get_resources(self):
        return self._resources

    def reorder_resources(self, ids):
        self.reordered = list(ids)
        by_id = {r["id"]: r for r in self._resources}
        self._resources = [by_id[i] for i in ids]


def test_resources_are_ordered_by_name() -> None:
    from oex.hdx_publisher import HdxPublisher

    dataset = _FakeDataset(
        ["Waterways (OSM), KML", "Area of Interest, GeoJSON", "Buildings (OSM), KML"]
    )
    HdxPublisher._sort_resources_by_name(dataset, "ds")
    assert [r["name"] for r in dataset.get_resources()] == [
        "Area of Interest, GeoJSON",
        "Buildings (OSM), KML",
        "Waterways (OSM), KML",
    ]


def test_ordering_is_skipped_when_already_sorted() -> None:
    """Reordering is an extra HDX write, so it only happens when it changes something."""
    from oex.hdx_publisher import HdxPublisher

    dataset = _FakeDataset(["Alpha", "Beta", "Gamma"])
    HdxPublisher._sort_resources_by_name(dataset, "ds")
    assert dataset.reordered is None


def test_ordering_ignores_case() -> None:
    from oex.hdx_publisher import HdxPublisher

    dataset = _FakeDataset(["beta", "Alpha"])
    HdxPublisher._sort_resources_by_name(dataset, "ds")
    assert [r["name"] for r in dataset.get_resources()] == ["Alpha", "beta"]


def test_only_layers_on_a_shared_tileset_filter_it_by_category_and_source() -> None:
    """Only the combined tileset carries `category` and `source`; filtering a layer's own
    tileset on them draws nothing."""
    from oex.hdx_publisher import PublishedLayer, _page_layers

    published = {"buildings": [PublishedLayer("b", _layer("osm", "buildings").source_metadata, [])]}
    cfg = _cfg()
    cfg.categories = [CategoryConfig(name="buildings")]
    (own,) = _page_layers(cfg, published, None)
    (shared,) = _page_layers(cfg, published, ("all.pmtiles", "all"))
    assert own.match_key is None
    assert shared.match_key == "buildings|osm"


def test_a_category_tileset_is_read_with_the_category_layer() -> None:
    from oex.hdx_publisher import _category_tilesets

    url = "https://example.invalid/NPL/buildings/hotosm_npl_buildings_osm.pmtiles"
    resources = [{"name": "Buildings (OSM), Vector Tiles (PMTiles)", "url": url}]
    assert _category_tilesets(resources, "buildings") == {"osm": (url, "buildings")}
