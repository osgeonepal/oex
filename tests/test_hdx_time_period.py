from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from oex.config.loader import ConfigError, load_config
from oex.hdx_publisher import PublishContext, apply_time_period, span_other_sources

SNAPSHOT = datetime(2026, 10, 4)


class _Dataset:
    def set_time_period(self, start, end=None) -> None:
        self.period = (start, end)


def _period(start: str | None, **ctx_fields) -> tuple:
    dataset = _Dataset()
    apply_time_period(dataset, PublishContext(SNAPSHOT, **ctx_fields), start)
    return dataset.period


def test_a_configured_start_runs_the_period_to_the_snapshot() -> None:
    assert _period("2026-08-26") == (datetime(2026, 8, 26), SNAPSHOT)


def test_without_a_start_the_period_is_the_snapshot_day() -> None:
    assert _period(None) == (SNAPSHOT, None)


def test_feature_dates_take_precedence_over_a_configured_start() -> None:
    first, last = datetime(2026, 9, 1), datetime(2026, 9, 30)
    assert _period("2026-08-26", temporal_min=first, temporal_max=last) == (first, last)


def test_a_start_after_the_snapshot_is_refused() -> None:
    """HDX accepts a reversed range and shows it as is."""
    with pytest.raises(ValueError, match="after the snapshot"):
        _period("2026-10-10")


def test_a_start_that_is_not_a_date_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text("iso3: NPL\nhdx:\n  time_period_start: 26-08-2026\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="time_period_start"):
        load_config(config)


BASE = "https://example.org/hdx-dev-test/BTN_TEST_EVENT/combined"
NAME = "hdx_dev_test_btn_event"


@pytest.mark.usefixtures("hdx_demo_configuration")
def test_the_source_published_last_keeps_the_newer_snapshot_of_another_source() -> None:
    from hdx.data.dataset import Dataset

    dataset = Dataset({"name": NAME})
    dataset.set_time_period(datetime(2026, 10, 1), datetime(2026, 10, 21))
    held = [
        {"id": "osm", "name": "STAC Collection (OSM)", "url": f"{BASE}/{NAME}_osm_metadata.json"},
        {"id": "own", "name": "Overture", "url": f"{BASE}/{NAME}_overture_metadata.json"},
        {"id": "aoi", "name": "Area of Interest", "url": f"{BASE}/{NAME}_aoi.geojson"},
    ]
    osm_period = {"interval": [["2026-11-09T16:41:50Z", "2026-11-09T16:41:50Z"]]}
    collections = {f"{BASE}/{NAME}_osm_metadata.json": {"extent": {"temporal": osm_period}}}
    with patch("oex.hdx_publisher._download_json", side_effect=lambda url: collections[url]):
        span_other_sources(dataset, held, NAME, "overture", turned_off=frozenset())
    period = dataset.get_time_period()
    assert (period["startdate"].date(), period["enddate"].date()) == (
        datetime(2026, 10, 1).date(),
        datetime(2026, 11, 9).date(),
    )


@pytest.mark.usefixtures("hdx_demo_configuration")
def test_a_source_turned_off_in_the_config_does_not_widen_the_period() -> None:
    """Its files are removed in the same publish, so its dates no longer describe the data."""
    from hdx.data.dataset import Dataset

    dataset = Dataset({"name": NAME})
    dataset.set_time_period(datetime(2026, 10, 1), datetime(2026, 10, 21))
    held = [
        {"id": "osm", "name": "STAC Collection (OSM)", "url": f"{BASE}/{NAME}_osm_metadata.json"}
    ]
    with patch("oex.hdx_publisher._download_json") as download:
        span_other_sources(dataset, held, NAME, "overture", turned_off=frozenset({"osm"}))
    period = dataset.get_time_period()
    assert download.call_count == 0
    assert period["enddate"].date() == datetime(2026, 10, 21).date()


@pytest.mark.usefixtures("hdx_demo_configuration")
def test_a_per_category_dataset_with_its_own_folder_still_spans_every_source() -> None:
    """Its files are named from the S3 folder, not the dataset name."""
    from hdx.data.dataset import Dataset

    dataset = Dataset({"name": "hot_flood_npl_buildings"})
    dataset.set_time_period(datetime(2026, 10, 21), datetime(2026, 10, 21))
    url = "https://example.org/ISO3/NPL_FLOOD/buildings/hot_flood_npl_flood_buildings_osm_metadata.json"
    held = [{"id": "osm", "name": "STAC Collection (OSM)", "url": url}]
    osm_period = {"interval": [["2026-11-09T16:41:50Z", "2026-11-09T16:41:50Z"]]}
    with patch(
        "oex.hdx_publisher._download_json", return_value={"extent": {"temporal": osm_period}}
    ):
        span_other_sources(
            dataset, held, "hot_flood_npl_buildings", "overture", turned_off=frozenset()
        )
    assert dataset.get_time_period()["enddate"].date() == datetime(2026, 11, 9).date()


@pytest.mark.usefixtures("hdx_demo_configuration")
def test_an_unreadable_collection_of_another_source_names_the_file() -> None:
    from hdx.data.dataset import Dataset

    dataset = Dataset({"name": NAME})
    dataset.set_time_period(datetime(2026, 10, 1), datetime(2026, 10, 21))
    held = [{"id": "osm", "name": "OSM", "url": f"{BASE}/{NAME}_osm_metadata.json"}]
    with (
        patch("oex.hdx_publisher._download_json", return_value={"extent": {}}),
        pytest.raises(RuntimeError, match=f"{NAME}_osm_metadata.json"),
    ):
        span_other_sources(dataset, held, NAME, "overture", turned_off=frozenset())
