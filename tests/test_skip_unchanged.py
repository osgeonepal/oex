"""skip_unchanged: a layer whose features and settings match its published STAC Item."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from hdx.data.hdxobject import HDXError

from oex.boundary import Boundary
from oex.catalog import CatalogConflictError
from oex.config.schema import CategoryConfig, RootConfig
from oex.exporter import COMBINED_STATE_KEY, BuiltCategory, CategoryResult, Exporter
from oex.sources.base import SourceQuery
from oex.state import StateStore

FEATURES = """
    SELECT * FROM (VALUES
        ('node/1', MAP {'amenity': 'school'}, '2026-01-05', ST_Point(85.30, 27.70)),
        ('way/2', MAP {'highway': 'primary'}, '2026-03-09', ST_Point(85.31, 27.71))
    ) AS t(feature_id, tags, update_time, geom)
"""


def _cfg(tmp_path: Path) -> RootConfig:
    cfg = RootConfig(iso3="NPL", categories=[CategoryConfig(name="schools")])
    cfg.output.dir = str(tmp_path / "output")
    cfg.output.metadata = True
    cfg.output.skip_unchanged = True
    cfg.output.s3.enabled = True
    cfg.output.s3.bucket = "exports"
    cfg.hdx.push = True
    cfg.duckdb.temp_dir = str(tmp_path / "duckdb")
    return cfg


def _fingerprint(exporter: Exporter, conn: duckdb.DuckDBPyConnection, rows_sql: str) -> str:
    conn.execute(f"CREATE OR REPLACE TABLE features AS {rows_sql}")
    return exporter._content_hash(conn, "features", exporter._cfg.categories[0])


def test_the_fingerprint_ignores_row_order(make_exporter, tmp_path, spatial_conn):
    """Parallel queries return rows in any order; the data is the same."""
    exporter = make_exporter(_cfg(tmp_path), "osm")
    forward = _fingerprint(exporter, spatial_conn, FEATURES)
    reversed_order = _fingerprint(
        exporter, spatial_conn, f"SELECT * FROM ({FEATURES}) ORDER BY feature_id DESC"
    )
    assert forward == reversed_order


def test_the_fingerprint_changes_when_a_tag_value_changes(make_exporter, tmp_path, spatial_conn):
    exporter = make_exporter(_cfg(tmp_path), "osm")
    before = _fingerprint(exporter, spatial_conn, FEATURES)
    after = _fingerprint(exporter, spatial_conn, FEATURES.replace("'primary'", "'secondary'"))
    assert before != after


def test_the_fingerprint_counts_repeated_rows(make_exporter, tmp_path, spatial_conn):
    """Two extra copies of a row would cancel out under XOR."""
    exporter = make_exporter(_cfg(tmp_path), "osm")
    once = _fingerprint(exporter, spatial_conn, FEATURES)
    first_row = f"SELECT * FROM ({FEATURES}) WHERE feature_id = 'node/1'"
    tripled = _fingerprint(
        exporter, spatial_conn, f"{FEATURES} UNION ALL {first_row} UNION ALL {first_row}"
    )
    assert once != tripled


def test_the_recipe_ignores_paths_and_memory_but_not_the_layer(make_exporter, tmp_path):
    """The laptop and the server write to different disks with different memory."""
    laptop, server = _cfg(tmp_path), _cfg(tmp_path)
    server.output.dir = "/mnt/disk/output"
    server.parallel.memory_gb = 12
    category = laptop.categories[0]
    assert make_exporter(laptop)._layer_recipe(category) == make_exporter(server)._layer_recipe(
        category
    )

    without_pcodes = CategoryConfig(name="schools", skip_pcodes=True)
    assert make_exporter(laptop)._layer_recipe(without_pcodes) != make_exporter(
        laptop
    )._layer_recipe(category)


@pytest.mark.parametrize(
    ("item", "expected"),
    [(None, None), ({"properties": {"oex:content_hash": "abc"}}, "abc")],
)
def test_the_published_fingerprint_is_read_from_the_item(make_exporter, tmp_path, item, expected):
    exporter = make_exporter(_cfg(tmp_path), "osm")
    with patch("oex.exporter.S3CatalogStore") as store:
        store.return_value.read.return_value = (item, None)
        assert exporter._published_content_hash(exporter._cfg.categories[0]) == expected
    assert store.return_value.read.call_args.args[0].endswith(
        "NPL/schools/npl_schools_osm_item.json"
    )


@pytest.mark.parametrize(
    ("setting", "value"),
    [("metadata", False), ("resume", False), ("skip_unchanged", False)],
)
def test_skipping_needs_metadata_resume_and_the_setting(make_exporter, tmp_path, setting, value):
    cfg = _cfg(tmp_path)
    assert make_exporter(cfg)._skips_unchanged(combine=False)
    setattr(cfg.output, setting, value)
    assert not make_exporter(cfg)._skips_unchanged(combine=False)


def test_a_combined_dataset_is_never_skipped(make_exporter, tmp_path):
    """One combined dataset holds every layer, so a single unchanged layer decides nothing."""
    assert not make_exporter(_cfg(tmp_path))._skips_unchanged(combine=True)


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.name = "osm"
    runner.query_for.return_value = SourceQuery(
        source_expr=f"({FEATURES.replace('geom)', 'geometry)')})",
        select_fields=["feature_id", "tags", "update_time"],
        where_conditions=[],
        bbox_mode="geom",
        dataset_source="OpenStreetMap",
        source_url="",
        source_description="",
        snapshot_date=datetime(2026, 12, 1, tzinfo=UTC),
        snapshot_label="2026-12-01",
        extra_readme_lines=[],
    )
    return runner


BOUNDARY = Boundary(
    iso3="NPL",
    bbox=(85.0, 27.0, 86.0, 28.0),
    geojson=json.dumps(
        {"type": "Polygon", "coordinates": [[[85, 27], [86, 27], [86, 28], [85, 28], [85, 27]]]}
    ),
    source="test",
)


PUBLISHED_ITEM = {
    "properties": {"oex:content_hash": "new", "created": "2026-11-04T06:45:47Z"},
    "assets": {"geojson": {"href": "s3://exports/NPL/schools/npl_schools_osm_geojson.zip"}},
}

FAR_AWAY = Boundary(
    iso3="NPL",
    bbox=(0.0, 0.0, 1.0, 1.0),
    geojson=json.dumps(
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    ),
    source="test",
)


def _process(
    cfg: RootConfig,
    tmp_path: Path,
    publisher: MagicMock | None,
    published: str,
    state: StateStore | None = None,
):
    """Returns the built category, the last Item uploaded to S3, and the catalog store that
    holds the published Item."""
    exporter = Exporter(cfg, _runner())
    exporter._state = state
    staged_items: list[dict] = []

    def stage(path: Path, **_):
        if path.name.endswith("_item.json"):
            staged_items.append(json.loads(path.read_text()))

    with (
        patch.object(Exporter, "_published_content_hash", return_value=published),
        patch.object(Exporter, "_content_hash", return_value="new"),
        patch("oex.exporter.s3_upload", side_effect=stage),
        patch("oex.exporter.S3CatalogStore") as store,
    ):
        store.return_value.read.return_value = (copy.deepcopy(PUBLISHED_ITEM), "etag-1")
        built = exporter._process_category(cfg.categories[0], BOUNDARY, tmp_path / "out", publisher)
    return built, staged_items[-1] if staged_items else None, store.return_value


def test_an_unchanged_layer_only_moves_its_hdx_dates(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.categories[0].temporal.column = "update_time"
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = "hotosm_npl_schools"

    built, _, _ = _process(cfg, tmp_path, publisher, published="new")

    assert built.result.status == "unchanged"
    assert built.result.feature_count == 2
    publisher.publish.assert_not_called()
    context = publisher.refresh_metadata.call_args.args[2]
    assert (context.temporal_min.date(), context.temporal_max.date()) == (
        datetime(2026, 1, 5).date(),
        datetime(2026, 3, 9).date(),
    )


def test_an_unchanged_layer_stamps_its_published_item_as_updated(tmp_path):
    """A scheduler reading the catalog would otherwise see the layer as never refreshed."""
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = "hotosm_npl_schools"

    _, _, store = _process(_cfg(tmp_path), tmp_path, publisher, published="new")

    key, item, version = store.write.call_args.args
    assert key.endswith("NPL/schools/npl_schools_osm_item.json")
    assert version == "etag-1"
    assert item["properties"]["created"] == "2026-11-04T06:45:47Z"
    assert datetime.fromisoformat(item["properties"]["updated"]).date() == datetime.now(UTC).date()


def test_a_failed_refresh_leaves_the_item_unstamped(tmp_path):
    publisher = MagicMock()
    publisher.refresh_metadata.side_effect = HDXError("down")

    _, _, store = _process(_cfg(tmp_path), tmp_path, publisher, published="new")

    store.write.assert_not_called()


@pytest.mark.parametrize(("published", "on_hdx"), [("old", "hotosm_npl_schools"), ("new", None)])
def test_a_rebuilt_layer_is_stamped_once_hdx_accepts_it(tmp_path, published, on_hdx):
    """The Item is staged before the publish, which reads it, so `updated` marks success."""
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = on_hdx
    publisher.publish.return_value = "hotosm_npl_schools"

    built, _, store = _process(_cfg(tmp_path), tmp_path, publisher, published=published)

    assert built.result.status == "ok"
    assert "updated" in store.write.call_args.args[1]["properties"]


def test_a_failed_publish_leaves_the_item_unstamped(tmp_path):
    """Otherwise the layer reads as published and the scheduler never retries it."""
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = None
    publisher.publish.side_effect = HDXError("upload rejected")

    built, _, store = _process(_cfg(tmp_path), tmp_path, publisher, published="old")

    assert built.result.status == "failed"
    store.write.assert_not_called()


def test_an_unchanged_layer_missing_from_hdx_is_published(tmp_path):
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = None

    built, item, _ = _process(_cfg(tmp_path), tmp_path, publisher, published="new")

    assert built.result.status == "ok"
    publisher.publish.assert_called_once()
    assert item["properties"]["oex:content_hash"] == "new"


def test_a_changed_layer_is_published_with_its_new_fingerprint(tmp_path):
    publisher = MagicMock()

    built, item, _ = _process(_cfg(tmp_path), tmp_path, publisher, published="old")

    assert built.result.status == "ok"
    publisher.refresh_metadata.assert_not_called()
    publisher.publish.assert_called_once()
    assert item["properties"]["oex:content_hash"] == "new"


def test_a_failed_publish_leaves_no_fingerprint_to_skip_on(tmp_path):
    """Otherwise the next run would skip a layer HDX never received."""
    publisher = MagicMock()
    publisher.publish.side_effect = RuntimeError("HDX timeout")

    built, item, _ = _process(_cfg(tmp_path), tmp_path, publisher, published="old")

    assert built.result.status == "failed"
    assert item["properties"].get("oex:content_hash") is None


@pytest.mark.parametrize(("on_hdx", "marked_done"), [("hotosm_npl_schools", True), (None, False)])
def test_only_a_refreshed_unchanged_layer_counts_as_done(tmp_path, on_hdx, marked_done):
    """A rerun of the same snapshot then skips it without touching HDX again."""
    state = StateStore(path=tmp_path / ".state.json", iso3="NPL", source="osm")
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = on_hdx
    publisher.publish.side_effect = RuntimeError("HDX timeout")

    _process(_cfg(tmp_path), tmp_path, publisher, published="new", state=state)

    assert state.is_uploaded("schools", snapshot_label="2026-12-01") is marked_done


def test_an_hdx_error_while_refreshing_an_unchanged_layer_fails_it(tmp_path):
    """A transient HDX error is no reason to re-export and republish an unchanged layer."""
    publisher = MagicMock()
    publisher.refresh_metadata.side_effect = HDXError("HDX unavailable")

    built, _, _ = _process(_cfg(tmp_path), tmp_path, publisher, published="new")

    assert built.result.status == "failed"
    assert built.result.error == "HDX unavailable"
    publisher.publish.assert_not_called()


def test_an_hdx_error_while_refreshing_an_empty_layer_keeps_it_empty(tmp_path):
    """An empty layer has nothing to re-export, so the older HDX date simply stays."""
    publisher = MagicMock()
    publisher.refresh_metadata.side_effect = HDXError("HDX unavailable")
    exporter = Exporter(_cfg(tmp_path), _runner())

    with patch("oex.exporter.S3CatalogStore") as store:
        store.return_value.read.return_value = (copy.deepcopy(PUBLISHED_ITEM), "etag-1")
        built = exporter._process_category(
            exporter._cfg.categories[0], FAR_AWAY, tmp_path / "out", publisher
        )

    assert built.result.status == "empty"
    publisher.refresh_metadata.assert_called_once()
    store.return_value.write.assert_not_called()


def test_a_layer_that_turned_empty_stamps_its_item_once_hdx_is_refreshed(tmp_path):
    """Its old Item would otherwise read as overdue on every scheduled run."""
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = "hotosm_npl_schools"

    built, staged, store = _process_empty(tmp_path, copy.deepcopy(PUBLISHED_ITEM), publisher)

    assert built.result.status == "empty"
    assert staged == []
    assert store.write.call_args.args[1]["assets"] == PUBLISHED_ITEM["assets"]


def test_an_empty_layer_whose_hdx_dataset_was_deleted_loses_its_files_in_stac(tmp_path):
    """HDX no longer carries the files, so the Item must not list them."""
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = None

    built, staged, store = _process_empty(tmp_path, copy.deepcopy(PUBLISHED_ITEM), publisher)

    assert built.result.status == "empty"
    [(_, item)] = staged
    assert item["assets"] == {}
    store.write.assert_not_called()


def _process_empty(
    tmp_path: Path,
    published_item: dict | None,
    publisher: MagicMock | None,
    cfg: RootConfig | None = None,
):
    """Returns the built category, the Items staged to S3 for a layer with no features, and
    the catalog store that holds the published Item."""
    exporter = Exporter(cfg or _cfg(tmp_path), _runner())
    staged: list[tuple[str, dict]] = []

    def stage(path: Path, *, key: str, **_):
        staged.append((key, json.loads(path.read_text())))

    with (
        patch("oex.exporter.s3_upload", side_effect=stage),
        patch("oex.exporter.S3CatalogStore") as store,
    ):
        store.return_value.read.return_value = (
            published_item,
            "etag-1" if published_item else None,
        )
        built = exporter._process_category(
            exporter._cfg.categories[0], FAR_AWAY, tmp_path / "out", publisher
        )
    return built, staged, store.return_value


@pytest.mark.parametrize(
    "published_item",
    [None, {"properties": {"created": "2026-11-04T06:45:47Z"}, "assets": {}}],
    ids=["never-published", "empty-before"],
)
def test_an_empty_layer_stages_an_item_without_files(tmp_path, published_item):
    """Without it the catalog cannot tell a layer found empty from a category never run, and
    an HDX dataset an empty layer never had cannot date it either."""
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = None

    built, staged, _ = _process_empty(tmp_path, published_item, publisher)

    assert built.result.status == "empty"
    [(key, item)] = staged
    assert key.endswith("NPL/schools/npl_schools_osm_item.json")
    assert item["assets"] == {}
    assert item["properties"]["table:row_count"] == 0
    assert item["properties"]["created"] > "2026-11-04T06:45:47Z"
    assert not [link for link in item["links"] if link["rel"] in ("collection", "via")]
    assert "oex:geometry_types" not in item["properties"]
    publisher.refresh_metadata.assert_called_once()


def test_an_empty_layer_keeps_the_item_that_links_its_published_files(tmp_path):
    """Its files are still on S3 and HDX, so only its date moves."""
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = "hotosm_npl_schools"

    built, staged, _ = _process_empty(tmp_path, copy.deepcopy(PUBLISHED_ITEM), publisher)

    assert built.result.status == "empty"
    assert staged == []


def test_a_run_without_hdx_leaves_items_undated(tmp_path):
    """An Item's `updated` says HDX holds the layer, which a run that skips HDX cannot vouch for."""
    cfg = _cfg(tmp_path)
    cfg.hdx.push = False

    built, staged, store = _process_empty(tmp_path, copy.deepcopy(PUBLISHED_ITEM), None, cfg)

    assert built.result.status == "empty"
    assert staged == []
    store.write.assert_not_called()


def test_a_resume_after_an_upload_that_skipped_hdx_rebuilds_the_layer(tmp_path):
    """Otherwise a run that pushes would date the Item while HDX never got the layer."""
    state = StateStore(path=tmp_path / ".state.json", iso3="NPL", source="osm")
    state.mark_built("schools", snapshot_label="2026-12-01", zip_paths=[], metadata_json_path=None)
    state.mark_uploaded("schools", hdx_dataset=None)
    exporter = Exporter(_cfg(tmp_path), _runner())
    exporter._state = state
    query = exporter._runner.query_for(exporter._cfg, exporter._cfg.categories[0])

    with patch("oex.exporter.S3CatalogStore") as store:
        assert exporter._resumed_result(exporter._cfg.categories[0], query, 0.0, "") is None
    store.return_value.write.assert_not_called()


def _combined_cfg(tmp_path: Path) -> RootConfig:
    cfg = _cfg(tmp_path)
    cfg.hdx.push = cfg.hdx.combine = True
    return cfg


def _empty_in_combined(tmp_path: Path):
    """An empty layer of a combined dataset, which defers its Item to the combined publish."""
    built, staged, store = _process_empty(
        tmp_path, copy.deepcopy(PUBLISHED_ITEM), MagicMock(), _combined_cfg(tmp_path)
    )
    assert (built.result.status, staged) == ("empty", [])
    store.write.assert_not_called()
    return built


def _record(tmp_path: Path, built_list: list, published: bool):
    exporter = Exporter(_combined_cfg(tmp_path), _runner())
    staged: list[dict] = []
    with (
        patch(
            "oex.exporter.s3_upload",
            side_effect=lambda path, **_: staged.append(json.loads(path.read_text())),
        ),
        patch("oex.exporter.S3CatalogStore") as store,
    ):
        store.return_value.read.return_value = (copy.deepcopy(PUBLISHED_ITEM), "etag-1")
        exporter._record_combined_layers(built_list, tmp_path / "out", None, published=published)
    return staged, store.return_value


def test_a_layer_dropped_from_a_combined_dataset_loses_its_files_in_stac(tmp_path):
    """The combined dataset no longer carries the layer, so its Item must not list the files."""
    staged, store = _record(tmp_path, [_empty_in_combined(tmp_path)], published=True)

    [item] = staged
    assert item["assets"] == {}
    assert item["properties"]["table:row_count"] == 0
    assert "updated" in item["properties"]
    store.write.assert_not_called()


def test_an_all_empty_combined_run_keeps_the_files_hdx_still_carries(tmp_path):
    """Nothing was published, so the dataset still holds the layer's files from before."""
    staged, store = _record(tmp_path, [_empty_in_combined(tmp_path)], published=False)

    assert staged == []
    assert store.write.call_args.args[1]["assets"] == PUBLISHED_ITEM["assets"]


@pytest.mark.parametrize(("zips", "stamped"), [(["schools.zip"], True), ([], False)])
def test_a_layer_of_a_combined_dataset_is_stamped_only_when_published(tmp_path, zips, stamped):
    """A layer without files stays out of the combined publish, so nothing vouches for it."""
    built = BuiltCategory(
        result=CategoryResult(name="schools", status="ok", duration_s=0.0),
        category=CategoryConfig(name="schools"),
        zip_paths=[tmp_path / name for name in zips],
    )
    _, store = _record(tmp_path, [built], published=True)

    assert store.write.called is stamped


def test_a_missing_combined_item_raises_after_the_others_are_dated(tmp_path):
    """Every layer that has its Item is dated first, so only the missing one reruns."""
    exporter = Exporter(_combined_cfg(tmp_path), _runner())
    built = [
        BuiltCategory(
            result=CategoryResult(name=name, status="ok", duration_s=0.0),
            category=CategoryConfig(name=name),
            zip_paths=[tmp_path / f"{name}.zip"],
        )
        for name in ("roads", "schools")
    ]
    with patch("oex.exporter.S3CatalogStore") as store:
        store.return_value.read.side_effect = [(None, None), (copy.deepcopy(PUBLISHED_ITEM), "e")]
        with pytest.raises(CatalogConflictError, match="roads"):
            exporter._record_combined_layers(built, tmp_path / "out", None, published=True)
    assert store.return_value.write.call_count == 1


def test_a_resumed_layer_whose_item_is_missing_is_rebuilt(tmp_path):
    """Its Item moved or was deleted, so the layer is exported again rather than failing."""
    state = StateStore(path=tmp_path / ".state.json", iso3="NPL", source="osm")
    state.mark_built("schools", snapshot_label="2026-12-01", zip_paths=[], metadata_json_path=None)
    state.mark_uploaded("schools", hdx_dataset="hotosm_npl_schools")
    exporter = Exporter(_cfg(tmp_path), _runner())
    exporter._state = state
    query = exporter._runner.query_for(exporter._cfg, exporter._cfg.categories[0])

    with patch("oex.exporter.S3CatalogStore") as store:
        store.return_value.read.return_value = (None, None)
        assert exporter._resumed_result(exporter._cfg.categories[0], query, 0.0, "") is None


def test_a_failed_combined_publish_records_nothing(tmp_path):
    """No layer is dated, so the scheduler retries the dataset."""
    exporter = Exporter(_combined_cfg(tmp_path), _runner())
    built = BuiltCategory(
        result=CategoryResult(name="schools", status="ok", duration_s=0.0),
        category=CategoryConfig(name="schools"),
        zip_paths=[tmp_path / "schools.zip"],
    )
    with (
        patch.object(Exporter, "_build_combined_tiles", return_value=None),
        patch.object(Exporter, "_publish_combined", side_effect=HDXError("upload rejected")),
        patch.object(Exporter, "_record_combined_layers") as record,
        pytest.raises(HDXError),
    ):
        exporter._finish_combined(
            [built], tmp_path / "out", MagicMock(), peeked_label=None, boundary_bbox=(0, 0, 1, 1)
        )
    record.assert_not_called()


@pytest.mark.parametrize("published_item", [None, PUBLISHED_ITEM], ids=["none", "with-files"])
def test_a_run_without_metadata_leaves_items_alone(tmp_path, published_item):
    """Items are built from the layer metadata, so dating one would vouch for stale content."""
    cfg = _cfg(tmp_path)
    cfg.output.metadata = False
    cfg.output.report.enabled = False

    _, staged, store = _process_empty(tmp_path, copy.deepcopy(published_item), None, cfg)

    assert staged == []
    store.write.assert_not_called()


@pytest.mark.parametrize(("status", "published"), [("ok", True), ("empty", False)])
def test_the_combined_run_records_its_layers_after_the_publish(tmp_path, status, published):
    """With no layer built nothing is published, and the dataset keeps what it carried."""
    exporter = Exporter(_combined_cfg(tmp_path), _runner())
    built = BuiltCategory(
        result=CategoryResult(name="schools", status=status, duration_s=0.0),
        category=CategoryConfig(name="schools"),
        zip_paths=[tmp_path / "schools.zip"] if status == "ok" else [],
    )
    with (
        patch.object(Exporter, "_build_combined_tiles", return_value=None),
        patch.object(Exporter, "_publish_combined") as publish,
        patch.object(Exporter, "_record_combined_layers") as record,
    ):
        exporter._finish_combined(
            [built], tmp_path / "out", MagicMock(), peeked_label=None, boundary_bbox=(0, 0, 1, 1)
        )
    assert publish.called is published
    assert record.call_args.kwargs["published"] is published


def test_a_resumed_layer_dates_its_item(tmp_path):
    """Its upload this snapshot succeeded, so a run stopped before dating the Item ends here."""
    state = StateStore(path=tmp_path / ".state.json", iso3="NPL", source="osm")
    state.mark_built("schools", snapshot_label="2026-12-01", zip_paths=[], metadata_json_path=None)
    state.mark_uploaded("schools", hdx_dataset="hotosm_npl_schools")

    built, _, store = _process(_cfg(tmp_path), tmp_path, MagicMock(), "new", state=state)

    assert built.result.status == "ok"
    assert "updated" in store.write.call_args.args[1]["properties"]


@pytest.mark.parametrize(
    ("combine", "updated", "skips"),
    [
        (True, "2099-01-01T00:00:00Z", True),
        (True, "2000-01-01T00:00:00Z", False),
        (True, None, False),
        (False, "2099-01-01T00:00:00Z", False),
    ],
)
def test_a_whole_run_is_skipped_only_when_every_item_is_dated(tmp_path, combine, updated, skips):
    """A layer's own resume dates its Item; a combined dataset has none, so it reruns instead."""
    cfg = _combined_cfg(tmp_path) if combine else _cfg(tmp_path)
    exporter = Exporter(cfg, _runner())
    exporter._state = StateStore(path=tmp_path / ".state.json", iso3="NPL", source="osm")
    for key in (COMBINED_STATE_KEY, "schools"):
        exporter._state.mark_built(
            key, snapshot_label="2026-12-01", zip_paths=[], metadata_json_path=None
        )
        exporter._state.mark_uploaded(key, hdx_dataset="hotosm_npl")
    item = copy.deepcopy(PUBLISHED_ITEM)
    if updated:
        item["properties"]["updated"] = updated
    with patch("oex.exporter.S3CatalogStore") as store:
        store.return_value.read.return_value = (item, "etag-1")
        assert exporter._resume_skips_run("2026-12-01") is skips


def test_a_run_without_hdx_stages_an_item_a_later_push_cannot_skip(tmp_path):
    """No `updated` and no fingerprint, so the next push exports the layer in full."""
    cfg = _cfg(tmp_path)
    cfg.hdx.push = False

    built, item, store = _process(cfg, tmp_path, None, published="old")

    assert built.result.status == "ok"
    assert "updated" not in item["properties"]
    assert "oex:content_hash" not in item["properties"]
    store.write.assert_not_called()


def test_an_empty_item_links_up_to_its_area_catalog(tmp_path):
    """It has no Collection, so without this a STAC browser cannot reach it."""
    cfg = _cfg(tmp_path)
    cfg.output.stac.catalog = True

    _, staged, _ = _process_empty(tmp_path, None, MagicMock(), cfg)

    [(_, item)] = staged
    parents = [link["href"] for link in item["links"] if link["rel"] == "parent"]
    assert parents == ["https://exports.s3.amazonaws.com/NPL/catalog.json"]
    assert "oex:geometry_types" not in item["properties"]
