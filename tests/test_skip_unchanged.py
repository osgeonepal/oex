"""skip_unchanged: a layer whose features and settings match its published STAC Item."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import duckdb
import pytest

from oex.boundary import Boundary
from oex.config.schema import CategoryConfig, RootConfig
from oex.exporter import Exporter
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
    cfg.duckdb.temp_dir = str(tmp_path / "duckdb")
    return cfg


@pytest.fixture
def conn():
    connection = duckdb.connect()
    connection.execute("INSTALL spatial; LOAD spatial")
    yield connection
    connection.close()


def _fingerprint(exporter: Exporter, conn, rows_sql: str) -> str:
    conn.execute(f"CREATE OR REPLACE TABLE features AS {rows_sql}")
    return exporter._content_hash(conn, "features", exporter._cfg.categories[0])


def test_the_fingerprint_ignores_row_order(tmp_path, conn):
    """Parallel queries return rows in any order; the data is the same."""
    exporter = Exporter(_cfg(tmp_path), Mock(name="osm"))
    forward = _fingerprint(exporter, conn, FEATURES)
    reversed_order = _fingerprint(
        exporter, conn, f"SELECT * FROM ({FEATURES}) ORDER BY feature_id DESC"
    )
    assert forward == reversed_order


def test_the_fingerprint_changes_with_a_tag(tmp_path, conn):
    exporter = Exporter(_cfg(tmp_path), Mock(name="osm"))
    before = _fingerprint(exporter, conn, FEATURES)
    after = _fingerprint(exporter, conn, FEATURES.replace("'primary'", "'secondary'"))
    assert before != after


def test_the_fingerprint_counts_repeated_rows(tmp_path, conn):
    """Two extra copies of a row would cancel out under XOR."""
    exporter = Exporter(_cfg(tmp_path), Mock(name="osm"))
    once = _fingerprint(exporter, conn, FEATURES)
    first_row = f"SELECT * FROM ({FEATURES}) WHERE feature_id = 'node/1'"
    tripled = _fingerprint(
        exporter, conn, f"{FEATURES} UNION ALL {first_row} UNION ALL {first_row}"
    )
    assert once != tripled


def test_the_recipe_ignores_paths_and_memory_but_not_the_layer(tmp_path):
    """The laptop and the server write to different disks with different memory."""
    laptop, server = _cfg(tmp_path), _cfg(tmp_path)
    server.output.dir = "/mnt/disk/output"
    server.parallel.memory_gb = 12
    category = laptop.categories[0]
    assert Exporter(laptop, Mock())._layer_recipe(category) == Exporter(
        server, Mock()
    )._layer_recipe(category)

    without_pcodes = CategoryConfig(name="schools", skip_pcodes=True)
    assert Exporter(laptop, Mock())._layer_recipe(without_pcodes) != Exporter(
        laptop, Mock()
    )._layer_recipe(category)


@pytest.mark.parametrize(
    ("item", "expected"),
    [(None, None), ({"properties": {"oex:content_hash": "abc"}}, "abc")],
)
def test_the_published_fingerprint_is_read_from_the_item(tmp_path, item, expected):
    exporter = Exporter(_cfg(tmp_path), Mock(name="osm"))
    exporter._runner.name = "osm"
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
def test_skipping_needs_metadata_resume_and_the_setting(tmp_path, setting, value):
    cfg = _cfg(tmp_path)
    assert Exporter(cfg, Mock())._skips_unchanged(combine=False)
    setattr(cfg.output, setting, value)
    assert not Exporter(cfg, Mock())._skips_unchanged(combine=False)


def test_a_combined_dataset_is_never_skipped(tmp_path):
    """One combined dataset holds every layer, so a single unchanged layer decides nothing."""
    assert not Exporter(_cfg(tmp_path), Mock())._skips_unchanged(combine=True)


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.name = "osm"
    runner.query_for.return_value = SourceQuery(
        source_expr=f"({FEATURES.replace('geom)', 'geometry)')})",
        select_fields=["feature_id", "tags", "update_time"],
        where_conditions=[],
        bbox_cols="geom",
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


def _process(
    cfg: RootConfig,
    tmp_path: Path,
    publisher: MagicMock,
    published: str,
    state: StateStore | None = None,
):
    """Returns the built category and the last Item uploaded to S3."""
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
    ):
        built = exporter._process_category(cfg.categories[0], BOUNDARY, tmp_path / "out", publisher)
    return built, staged_items[-1] if staged_items else None


def test_an_unchanged_layer_only_moves_its_hdx_dates(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.categories[0].temporal.column = "update_time"
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = "hotosm_npl_schools"

    built, _ = _process(cfg, tmp_path, publisher, published="new")

    assert built.result.status == "unchanged"
    assert built.result.feature_count == 2
    publisher.publish.assert_not_called()
    context = publisher.refresh_metadata.call_args.args[2]
    assert (context.temporal_min.date(), context.temporal_max.date()) == (
        datetime(2026, 1, 5).date(),
        datetime(2026, 3, 9).date(),
    )


def test_an_unchanged_layer_missing_from_hdx_is_published(tmp_path):
    publisher = MagicMock()
    publisher.refresh_metadata.return_value = None

    built, item = _process(_cfg(tmp_path), tmp_path, publisher, published="new")

    assert built.result.status == "ok"
    publisher.publish.assert_called_once()
    assert item["properties"]["oex:content_hash"] == "new"


def test_a_changed_layer_is_published_with_its_new_fingerprint(tmp_path):
    publisher = MagicMock()

    built, item = _process(_cfg(tmp_path), tmp_path, publisher, published="old")

    assert built.result.status == "ok"
    publisher.refresh_metadata.assert_not_called()
    publisher.publish.assert_called_once()
    assert item["properties"]["oex:content_hash"] == "new"


def test_a_failed_publish_leaves_no_fingerprint_to_skip_on(tmp_path):
    """Otherwise the next run would skip a layer HDX never received."""
    publisher = MagicMock()
    publisher.publish.side_effect = RuntimeError("HDX timeout")

    built, item = _process(_cfg(tmp_path), tmp_path, publisher, published="old")

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
