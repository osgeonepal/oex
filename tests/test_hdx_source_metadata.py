from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hdx.data.resource import Resource

from oex.config.schema import BoundaryConfig, CategoryConfig, RootConfig, license_label
from oex.hdx_publisher import (
    HdxPublisher,
    PublishContext,
    PublishedLayer,
    _category_tilesets,
    _entry_name,
    _item_hrefs,
    _load_source_metadata,
    _page_layers,
    _resource_url,
    _slug_from_metadata_name,
    layer_resource_text,
)
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


Published = Callable[..., list[dict[str, Any]]]


@pytest.fixture
def layer(make_source_metadata: Callable[..., SourceMetadata]) -> Callable[[str, str], LayerAssets]:
    def make(source_name: str, category: str) -> LayerAssets:
        return LayerAssets(
            category=CategoryConfig(name=category),
            source_metadata=make_source_metadata(source_name),
            dataset_name=f"hot_flood_npl_{category}",
        )

    return make


@pytest.fixture
def published(layer: Callable[[str, str], LayerAssets]) -> Published:
    """What a reader fetches: the Collection first, then the Item behind each item link."""

    def publish(
        *pairs: tuple[str, str], dataset_name: str = "hot_flood_npl"
    ) -> list[dict[str, Any]]:
        layers = [layer(source, category) for source, category in pairs]
        collection = build_collection(
            dataset_name=dataset_name,
            title="Nepal flood affected area data",
            description="Vector data for the flood affected area.",
            layers=layers,
            cfg=_cfg(),
        )
        items = [build_item(each, _cfg()) for each in layers]
        return [to_payload(collection), *(to_payload(item) for item in items)]

    return publish


def test_the_collection_names_where_every_item_lives(published: Published) -> None:
    """The reader walks rel=item; a Collection that names none describes nothing."""
    collection, *_ = published(("osm", "buildings"), ("overture", "points_of_interest"))

    assert [href.rsplit("/", 1)[-1] for href in _item_hrefs(collection, f"{BASE}/c.json")] == [
        "hot_flood_npl_buildings_osm_item.json",
        "hot_flood_npl_points_of_interest_overture_item.json",
    ]


def test_an_entry_name_resolves_to_its_category_slug(published: Published) -> None:
    """The dataset page matches layers by name; an unresolvable one drops a layer."""
    collection, *items = published(("osm", "buildings"), ("overture", "points_of_interest"))
    names = [_entry_name(collection, item) for item in items]

    assert names == [
        "hot_flood_npl_buildings_osm_metadata.json",
        "hot_flood_npl_points_of_interest_overture_metadata.json",
    ]
    assert [_slug_from_metadata_name(n, PREFIX, CATEGORIES) for n in names] == [
        "buildings",
        "points_of_interest",
    ]


def test_entry_names_come_from_the_collection_not_the_resource_filename(
    published: Published,
) -> None:
    """A combined dataset names one metadata resource per source, and the resource name
    is a human-readable title that carries no filename convention."""
    collection, item = published(("overture", "buildings"))

    assert _entry_name(collection, item) == "hot_flood_npl_buildings_overture_metadata.json"


def test_a_custom_dataset_name_carries_into_the_entry_names(published: Published) -> None:
    """`hdx.combined.name` can differ from key + iso3; resources follow the dataset name."""
    dt_name = "hot_flood_npl_corridor"  # key is hot_flood_corridor, so they differ
    collection, item = published(("osm", "buildings"), dataset_name=dt_name)
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


def test_load_source_metadata_follows_the_collection_to_its_items(published: Published) -> None:
    """The Collection carries no layer detail, so a reader that stops there sees nothing."""
    resources = [
        {
            "name": "Layer Metadata, JSON",
            "url": f"{BASE}/hot_flood_npl_metadata.json",
        }
    ]
    fetched = published(("osm", "buildings"), ("overture", "points_of_interest"))

    with patch("oex.hdx_publisher._download_json", side_effect=fetched):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [layer.source.source_name for layer in loaded] == ["osm", "overture"]
    assert [layer.source.metadata.feature_count for layer in loaded] == [3, 3]
    assert [_slug_from_metadata_name(layer.name, PREFIX, CATEGORIES) for layer in loaded] == [
        "buildings",
        "points_of_interest",
    ]


def test_every_source_on_a_dataset_contributes_its_layers(published: Published) -> None:
    """Each source keeps its own resource, so the pair covers every layer."""
    resources = [
        {"name": "Layer Metadata (OSM), JSON", "url": f"{BASE}/hot_flood_npl_osm_metadata.json"},
        {
            "name": "Layer Metadata (Overture), JSON",
            "url": f"{BASE}/hot_flood_npl_overture_metadata.json",
        },
    ]
    fetched = [*published(("osm", "buildings")), *published(("overture", "buildings"))]

    with patch("oex.hdx_publisher._download_json", side_effect=fetched):
        loaded = _load_source_metadata(resources, "hot_flood_npl")

    assert [layer.source.source_name for layer in loaded] == ["osm", "overture"]
    assert {_slug_from_metadata_name(layer.name, PREFIX, CATEGORIES) for layer in loaded} == {
        "buildings"
    }


def test_metadata_is_found_by_filename_not_display_name(published: Published) -> None:
    """Resources carry human-readable names, so the filename is what identifies them."""
    resources = [
        {
            "name": "Layer Metadata (OSM), JSON",
            "url": f"{BASE}/hot_flood_npl_osm_metadata.json",
        }
    ]
    with patch("oex.hdx_publisher._download_json", side_effect=published(("osm", "buildings"))):
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
    resources = [{"name": "Layer Metadata, JSON", "url": f"{BASE}/hot_flood_npl_metadata.json"}]
    with patch("oex.hdx_publisher._download_json", return_value={"id": "x"}):
        with pytest.raises(RuntimeError, match="could not parse"):
            _load_source_metadata(resources, "hot_flood_npl")


def test_resource_names_read_as_layer_and_source() -> None:
    """Resource names are what people scan on HDX, so they must not be filenames."""
    category = CategoryConfig(name="points_of_interest")
    name, description = layer_resource_text(category, "osm", "gpkg")
    assert name == "Points of Interest (OSM), GeoPackage"
    assert "From OpenStreetMap, as GeoPackage." in description


def test_format_is_part_of_the_name() -> None:
    """Same-named resources differing only by format get collapsed by ResourceMatcher."""
    category = CategoryConfig(name="buildings")
    names = {
        layer_resource_text(category, "osm", fmt)[0] for fmt in ("gpkg", "shp", "kml", "geojson")
    }
    assert len(names) == 4


def test_license_ids_render_as_names() -> None:
    """HDX needs the licence id; the report must not show that id to readers."""
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


class _FakeDataset(dict):
    """Just enough of the hdx Dataset surface for ordering resources and refreshing dates."""

    def __init__(self, resources: list[tuple[str, str]], owner_org: str = "org") -> None:
        super().__init__(owner_org=owner_org)
        self._resources = [
            {"id": str(index), "name": name, "url": url}
            for index, (name, url) in enumerate(resources)
        ]
        self.calls: list[str] = []

    def get_resources(self) -> list[dict[str, str]]:
        return self._resources

    def set_time_period(self, start: datetime, end: datetime | None = None) -> None:
        self.calls.append("set_time_period")
        self.period = {"startdate": start, "enddate": end or start}

    def get_time_period(self) -> dict:
        return self.period

    def update_in_hdx(self, **_options: object) -> None:
        self.calls.append("update_in_hdx")

    def reorder_resources(self, ids: list[str]) -> None:
        self.calls.append("reorder_resources")
        by_id = {resource["id"]: resource for resource in self._resources}
        self._resources = [by_id[resource_id] for resource_id in ids]

    def add_update_resource(self, resource: dict) -> None:
        self._resources.append({"id": str(len(self._resources)), **resource})

    def delete_resource(self, resource: dict) -> bool:
        self.calls.append("delete_resource")
        self._resources = [held for held in self._resources if held is not resource]
        return True

    def names(self) -> list[str]:
        return [resource["name"] for resource in self._resources]


HDX_DOWNLOAD = "https://data.example/dataset/x/resource/1/download/"
S3_LAYERS = "https://s3.example/hot/npl/_layers/"

# Every resource kind a category dataset carries, in the order HDX should list them.
ORDERED_RESOURCES = [
    ("Roads, Data Quality Report", f"{HDX_DOWNLOAD}hot_npl_roads_report.html"),
    ("Roads (OSM), GeoPackage points", f"{HDX_DOWNLOAD}hot_npl_roads_points_osm_gpkg.zip"),
    ("Roads (OSM), GeoPackage lines", f"{HDX_DOWNLOAD}hot_npl_roads_lines_osm_gpkg.zip"),
    ("Roads (OSM), GeoPackage polygons", f"{HDX_DOWNLOAD}hot_npl_roads_polygons_osm_gpkg.zip"),
    ("Roads (Overture), GeoPackage", f"{HDX_DOWNLOAD}hot_npl_roads_lines_overture_gpkg.zip"),
    ("Roads (OSM), Shapefile", f"{HDX_DOWNLOAD}hot_npl_roads_lines_osm_shp.zip"),
    ("Roads (OSM), GeoJSON", f"{HDX_DOWNLOAD}hot_npl_roads_lines_osm.geojson"),
    ("Roads (Overture), KML", f"{HDX_DOWNLOAD}hot_npl_roads_lines_overture_kml.zip"),
    ("Roads (OSM), GeoParquet", f"{S3_LAYERS}osm/roads.parquet"),
    ("Buildings (Overture), GeoParquet", f"{S3_LAYERS}overture/buildings.parquet"),
    ("Roads (OSM), Vector Tiles (PMTiles)", f"{HDX_DOWNLOAD}hot_npl_roads_osm.pmtiles"),
    ("Roads (OSM), STAC Collection", f"{HDX_DOWNLOAD}hot_npl_roads_osm_metadata.json"),
    ("Roads (Overture), STAC Collection", f"{HDX_DOWNLOAD}hot_npl_roads_overture_metadata.json"),
    ("Added by hand", f"{HDX_DOWNLOAD}field_notes.pdf"),
    ("Older notes", f"{HDX_DOWNLOAD}notes.html"),
]


def test_resources_are_reordered_report_first_then_by_format_source_and_geometry() -> None:
    shuffled = [
        ORDERED_RESOURCES[index] for index in (13, 6, 11, 2, 9, 0, 14, 4, 1, 12, 7, 3, 10, 8, 5)
    ]
    dataset = _FakeDataset(shuffled)
    HdxPublisher._order_resources(dataset, "ds")
    assert dataset.names() == [name for name, _ in ORDERED_RESOURCES]


def test_ordering_is_skipped_when_already_in_order() -> None:
    """Reordering is an extra HDX write, so it only happens when it changes something."""
    dataset = _FakeDataset(ORDERED_RESOURCES)
    HdxPublisher._order_resources(dataset, "ds")
    assert dataset.calls == []


def test_refreshing_the_date_reorders_resources_after_the_update(
    bare_publisher: HdxPublisher,
) -> None:
    cfg = RootConfig(iso3="NPL", key="hot", categories=[CategoryConfig(name="Roads")])
    dataset = _FakeDataset(list(reversed(ORDERED_RESOURCES)))
    overture = {
        "extent": {"temporal": {"interval": [["2026-04-15T00:00:00Z", "2026-04-15T00:00:00Z"]]}}
    }
    with (
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=dataset),
        patch("oex.hdx_publisher._download_json", return_value=overture),
    ):
        bare_publisher.refresh_metadata(
            cfg,
            cfg.categories[0],
            PublishContext(snapshot_date=datetime(2026, 5, 1, tzinfo=UTC), source_name="osm"),
        )
    # The period is set from this source, then widened over the Overture Collection.
    assert dataset.calls == [
        "set_time_period",
        "set_time_period",
        "update_in_hdx",
        "reorder_resources",
    ]
    assert dataset.period["startdate"] == datetime(2026, 4, 15, tzinfo=UTC)
    assert dataset.names() == [name for name, _ in ORDERED_RESOURCES]


def test_refreshing_the_date_removes_the_files_of_a_source_turned_off(
    bare_publisher: HdxPublisher,
) -> None:
    """A dataset whose data is unchanged still follows its config."""
    cfg = RootConfig(iso3="NPL", key="hot", categories=[CategoryConfig(name="Roads")])
    cfg.source["overture"].enabled = False
    dataset = _FakeDataset(ORDERED_RESOURCES)
    with (
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=dataset),
        patch("oex.hdx_publisher._download_json") as download,
    ):
        bare_publisher.refresh_metadata(
            cfg,
            cfg.categories[0],
            PublishContext(snapshot_date=datetime(2026, 5, 1, tzinfo=UTC), source_name="osm"),
        )
    assert download.call_count == 0
    # Buildings is not a category of this config, so its GeoParquet is not this dataset's file.
    assert [name for name, _ in ORDERED_RESOURCES if name not in dataset.names()] == [
        "Roads (Overture), GeoPackage",
        "Roads (Overture), KML",
        "Roads (Overture), STAC Collection",
    ]


def test_only_layers_on_a_shared_tileset_filter_it_by_category_and_source(
    layer: Callable[[str, str], LayerAssets],
) -> None:
    """Only the combined tileset carries `category` and `source`; filtering a layer's own
    tileset on them draws nothing."""
    published = {"buildings": [PublishedLayer("b", layer("osm", "buildings").source_metadata, [])]}
    cfg = _cfg()
    cfg.categories = [CategoryConfig(name="buildings")]
    (own,) = _page_layers(cfg, published, None)
    (shared,) = _page_layers(cfg, published, ("all.pmtiles", "all"))
    # A single-source layer names its source, as its HDX resources do.
    assert own.label == "Buildings (OSM)"
    assert own.match_key is None
    assert shared.match_key == "buildings|osm"


def test_a_category_tileset_is_read_with_the_category_layer() -> None:
    url = "https://example.invalid/NPL/buildings/hotosm_npl_buildings_osm.pmtiles"
    resources = [{"name": "Buildings (OSM), Vector Tiles (PMTiles)", "url": url}]
    assert _category_tilesets(resources, "buildings") == {"osm": (url, "buildings")}


COMBINED_ORDER = [
    ("Data Quality Report and Interactive Map", f"{HDX_DOWNLOAD}hot_npl_overview.html"),
    ("Roads (OSM), GeoPackage", f"{HDX_DOWNLOAD}hot_npl_roads_lines_osm_gpkg.zip"),
    ("Roads (OSM), Shapefile", f"{HDX_DOWNLOAD}hot_npl_roads_lines_osm_shp.zip"),
    ("Buildings (OSM), GeoPackage", f"{HDX_DOWNLOAD}hot_npl_buildings_polygons_osm_gpkg.zip"),
    ("Buildings (OSM), Shapefile", f"{HDX_DOWNLOAD}hot_npl_buildings_polygons_osm_shp.zip"),
    ("Roads (OSM), STAC Collection", f"{HDX_DOWNLOAD}hot_npl_roads_osm_metadata.json"),
    ("Buildings (OSM), STAC Collection", f"{HDX_DOWNLOAD}hot_npl_buildings_osm_metadata.json"),
]


def test_a_combined_dataset_keeps_each_category_together_in_config_order() -> None:
    dataset = _FakeDataset([COMBINED_ORDER[index] for index in (4, 6, 1, 0, 3, 5, 2)])
    HdxPublisher._order_resources(dataset, "ds", ["Roads", "Buildings"])
    assert dataset.names() == [name for name, _ in COMBINED_ORDER]


def test_a_report_uploaded_for_the_first_time_is_moved_to_the_top(
    hdx_demo_configuration: None, bare_publisher: HdxPublisher, tmp_path: Path
) -> None:
    """HDX appends a new resource, so the order is set again after the page upload."""
    dataset = _FakeDataset(ORDERED_RESOURCES[1:3])
    report = {"name": ORDERED_RESOURCES[0][0], "url": ORDERED_RESOURCES[0][1]}
    with (
        patch.object(bare_publisher, "_make_resource_for_path", return_value=Resource(report)),
        patch("oex.hdx_publisher._apply_custom_viz"),
    ):
        bare_publisher._publish_html_viz(
            dataset=dataset,
            hdx_dataset_name="ds",
            html_path=tmp_path / "hot_npl_roads_report.html",
            description="report",
            iso3="NPL",
            category_slug="roads",
            s3_config=None,
        )
    assert dataset.names() == [name for name, _ in ORDERED_RESOURCES[:3]]
