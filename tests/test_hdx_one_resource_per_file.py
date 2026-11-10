"""A dataset holds one resource per file, and a page shows each category and source once."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hdx.data.resource import Resource

from oex.config.schema import BoundaryConfig, CategoryConfig, RootConfig
from oex.hdx_publisher import HdxPublisher, PublishedLayer, TurnedOffSources, _page_layers
from oex.report import SourceMetadata

SNAPSHOT = datetime(2026, 11, 9, tzinfo=UTC)
BUCKET = "https://production-raw-data-api.s3.amazonaws.com"
CURRENT = f"{BUCKET}/hdx-dev-test/BTN"
OLDER = f"{BUCKET}/ISO3/BTN"
HDX_DOWNLOADS = "https://stage.data-humdata-org.ahconu.org/dataset/d/resource"
FILES = [
    "buildings/hotosm_btn_buildings_osm_gpkg.zip",
    "buildings/hotosm_btn_buildings_osm_shp.zip",
    "buildings/hotosm_btn_buildings_osm.pmtiles",
    "buildings/hotosm_btn_buildings_osm_metadata.json",
]


class _Dataset(dict):
    """Stands in for an HDX dataset after its update: every resource HDX now holds."""

    def __init__(self, written: Sequence[dict | Resource], held_after_update: list[dict]) -> None:
        super().__init__()
        self._resources: list[dict | Resource] = list(written)
        self._held_after_update = held_after_update
        self.deleted: list[str] = []
        self.period = {"startdate": SNAPSHOT, "enddate": SNAPSHOT}

    def get_resources(self) -> list[dict | Resource]:
        return self._resources

    def update_in_hdx(self, **_: object) -> None:
        self._resources = list(self._held_after_update)

    def delete_resource(self, resource: dict) -> bool:
        self.deleted.append(resource["url"])
        self._resources = [held for held in self._resources if held is not resource]
        return True

    def reorder_resources(self, _: list[str]) -> None: ...

    def get_time_period(self) -> dict[str, datetime]:
        return self.period

    def set_time_period(self, start: datetime, end: datetime) -> None:
        self.period = {"startdate": start, "enddate": end}


def _publish(
    bare_publisher: HdxPublisher,
    written: list[dict],
    others: list[dict],
    turned_off: frozenset[str] = frozenset(),
) -> _Dataset:
    """Run the update step with `written` as this publish's resources and `others` already held."""
    published = [dict(resource, id=f"id-{index}") for index, resource in enumerate(written)]
    return _update(bare_publisher, _Dataset(written, published + others), others, turned_off)


def _update(
    bare_publisher: HdxPublisher,
    dataset: _Dataset,
    others: list[dict],
    turned_off: frozenset[str] = frozenset(),
) -> _Dataset:
    existing = MagicMock()
    existing.get.return_value = "org"
    existing.__getitem__.return_value = "dataset-id"
    existing.get_resources.return_value = others
    with patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=existing):
        bare_publisher._create_or_update(
            dataset,
            "hotosm_btn_buildings",
            remove_additional=False,
            source_name="osm",
            turned_off=TurnedOffSources(turned_off, "hotosm_btn", CATEGORY_SLUGS),
        )
    return dataset


def test_a_copy_of_a_published_file_at_another_location_is_removed(
    bare_publisher: HdxPublisher,
) -> None:
    written = [{"name": f"new {path}", "url": f"{CURRENT}/{path}"} for path in FILES]
    older = [{"id": f"old-{path}", "name": path, "url": f"{OLDER}/{path}"} for path in FILES]
    dataset = _publish(bare_publisher, written, older)
    assert sorted(dataset.deleted) == sorted(f"{OLDER}/{path}" for path in FILES)


def test_another_sources_files_and_unrelated_files_stay(bare_publisher: HdxPublisher) -> None:
    written = [
        {"name": "Buildings (OSM), GeoPackage", "url": f"{CURRENT}/{FILES[0]}"},
        {"name": "Buildings (OSM), GeoParquet", "url": f"{CURRENT}/_layers/osm/buildings.parquet"},
    ]
    others = [
        {
            "id": "overture-gpkg",
            "name": "o",
            "url": f"{CURRENT}/buildings/hotosm_btn_buildings_overture_gpkg.zip",
        },
        {
            "id": "overture-parquet",
            "name": "o",
            "url": f"{CURRENT}/_layers/overture/buildings.parquet",
        },
        {"id": "notes", "name": "Field notes", "url": "https://example.org/field-notes.pdf"},
    ]
    dataset = _publish(bare_publisher, written, others)
    assert dataset.deleted == []


def test_a_staged_geoparquet_at_another_location_is_removed(bare_publisher: HdxPublisher) -> None:
    written = [
        {"name": "Buildings (OSM), GeoParquet", "url": f"{CURRENT}/_layers/osm/buildings.parquet"}
    ]
    older = [{"id": "old", "name": "b", "url": f"{OLDER}/_layers/osm/buildings.parquet"}]
    dataset = _publish(bare_publisher, written, older)
    assert dataset.deleted == [f"{OLDER}/_layers/osm/buildings.parquet"]


@pytest.mark.usefixtures("hdx_demo_configuration")
def test_a_file_uploaded_to_hdx_replaces_its_copy_at_another_location(
    bare_publisher: HdxPublisher, tmp_path: Path
) -> None:
    upload = tmp_path / "hotosm_btn_buildings_osm_gpkg.zip"
    upload.write_bytes(b"zip")
    written = Resource({"name": "Buildings (OSM), GeoPackage", "format": "GeoPackage"})
    written.set_file_to_upload(str(upload))
    held = {
        "id": "uploaded",
        "name": "Buildings (OSM), GeoPackage",
        "url": f"{HDX_DOWNLOADS}/uploaded/download/hotosm_btn_buildings_osm_gpkg.zip",
    }
    older = [{"id": "old", "name": "g", "url": f"{OLDER}/{FILES[0]}"}]
    dataset = _update(bare_publisher, _Dataset([written], [held, *older]), older)
    assert dataset.deleted == [f"{OLDER}/{FILES[0]}"]


CATEGORY_SLUGS = frozenset({"buildings", "roads", "roads_osm"})
OVERTURE_FILES = [
    f"{CURRENT}/buildings/hotosm_btn_buildings_overture_gpkg.zip",
    f"{CURRENT}/buildings/hotosm_btn_buildings_polygons_overture_shp.zip",
    f"{CURRENT}/buildings/hotosm_btn_buildings_overture.pmtiles",
    f"{CURRENT}/buildings/hotosm_btn_buildings_overture_metadata.json",
    f"{CURRENT}/_layers/overture/buildings.parquet",
]
SHARED_FILES = [
    f"{CURRENT}/buildings/hotosm_btn_buildings_report.html",
    "https://example.org/field-notes.pdf",
    "https://example.org/field_survey_overture.csv",
    f"{CURRENT}/buildings/hotosm_btn_buildings_overture.csv",
    f"{CURRENT}/combined/hotosm_btn_overture.pmtiles",
    f"{CURRENT}/roads_osm/hotosm_btn_roads_osm_gpkg.zip",
    f"{CURRENT}/schools/hotosm_btn_schools_overture_gpkg.zip",
]


def _held(urls: list[str]) -> list[dict]:
    return [{"id": url, "name": url.rsplit("/", 1)[-1], "url": url} for url in urls]


def test_a_source_turned_off_in_the_config_has_its_files_removed(
    bare_publisher: HdxPublisher,
) -> None:
    written = [{"name": "Buildings (OSM), GeoPackage", "url": f"{CURRENT}/{FILES[0]}"}]
    dataset = _publish(
        bare_publisher,
        written,
        _held(OVERTURE_FILES + SHARED_FILES),
        turned_off=frozenset({"overture"}),
    )
    assert sorted(dataset.deleted) == sorted(OVERTURE_FILES)


def test_a_category_whose_name_ends_in_a_source_is_not_read_as_that_source(
    bare_publisher: HdxPublisher,
) -> None:
    """`roads_osm` is a category; its files name no source when the source is left out."""
    osm_buildings = f"{CURRENT}/buildings/hotosm_btn_buildings_osm_gpkg.zip"
    roads_osm = f"{CURRENT}/roads_osm/hotosm_btn_roads_osm_gpkg.zip"
    written = [{"name": "Buildings (Overture), GeoPackage", "url": OVERTURE_FILES[0]}]
    dataset = _publish(
        bare_publisher, written, _held([osm_buildings, roads_osm]), frozenset({"osm"})
    )
    assert dataset.deleted == [osm_buildings]


def test_a_delete_hdx_does_not_carry_out_fails_the_publish(bare_publisher: HdxPublisher) -> None:
    class _KeepsEverything(_Dataset):
        def delete_resource(self, resource: dict) -> bool:
            return False

    written = [{"name": "Buildings (OSM), GeoPackage", "url": f"{CURRENT}/{FILES[0]}"}]
    published = [dict(written[0], id="id-0")]
    dataset = _KeepsEverything(written, published + _held(OVERTURE_FILES))
    with pytest.raises(RuntimeError, match="HDX did not delete"):
        _update(bare_publisher, dataset, _held(OVERTURE_FILES), frozenset({"overture"}))


def test_a_source_left_on_keeps_its_files_when_another_source_publishes(
    bare_publisher: HdxPublisher,
) -> None:
    """Each source publishes on its own run, so an OSM run leaves Overture's files alone."""
    written = [{"name": "Buildings (OSM), GeoPackage", "url": f"{CURRENT}/{FILES[0]}"}]
    overture_period = {"interval": [["2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"]]}
    with patch(
        "oex.hdx_publisher._download_json",
        return_value={"extent": {"temporal": overture_period}},
    ):
        dataset = _publish(bare_publisher, written, _held(OVERTURE_FILES + SHARED_FILES))
    assert dataset.deleted == []


def test_a_page_with_one_category_and_source_listed_twice_is_refused(
    make_source_metadata: Callable[..., SourceMetadata],
) -> None:
    cfg = RootConfig(iso3="BTN", key="hotosm", boundary=BoundaryConfig())
    cfg.categories = [CategoryConfig(name="buildings")]
    twice = [PublishedLayer(name, make_source_metadata("osm"), []) for name in ("now", "before")]
    with pytest.raises(RuntimeError, match="buildings from osm is published more than once"):
        _page_layers(cfg, {"buildings": twice}, None)
