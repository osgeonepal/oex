import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from hdx.api.configuration import Configuration

from oex import boundary
from oex.config.loader import load_config
from oex.config.schema import RootConfig
from oex.exporter import Exporter
from oex.hdx_publisher import HdxPublisher
from oex.metadata import ColumnReport, MetadataReport
from oex.report import SourceMetadata
from oex.writers import TileLayer


@pytest.fixture
def set_timezone() -> Iterator[Callable[[str], None]]:
    """Switch the process timezone; time.tzset runs again after restoring so no zone leaks."""
    original = os.environ.get("TZ")

    def apply(zone: str) -> None:
        os.environ["TZ"] = zone
        time.tzset()

    yield apply
    if original is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = original
    time.tzset()


@pytest.fixture(autouse=True)
def clear_boundary_cache() -> Iterator[None]:
    """Boundaries are cached per process, so one test's boundary must not answer another's."""
    boundary._boundary_cache.clear()
    yield
    boundary._boundary_cache.clear()


@pytest.fixture
def spatial_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(":memory:")
    # The axis order oex.duckdb_session sets; spatial refuses a GDAL COPY with an SRS without it.
    conn.execute("INSTALL spatial; LOAD spatial; SET geometry_always_xy = true")
    yield conn
    conn.close()


@pytest.fixture
def load_yaml_config(tmp_path: Path) -> Callable[[str], RootConfig]:
    """Load a YAML config from text, through the same loader the CLI uses."""

    def load(text: str) -> RootConfig:
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    return load


@pytest.fixture
def hdx_demo_configuration() -> Iterator[None]:
    """hdx-python-api keeps one global configuration, so each test sets and clears its own."""
    Configuration.delete()
    Configuration.create(hdx_site="demo", user_agent="oex-test", hdx_key="dummy")
    yield
    Configuration.delete()


@pytest.fixture
def bare_publisher() -> HdxPublisher:
    """A publisher for organisation `org` that skips the HDX session and its preflight."""
    publisher = HdxPublisher.__new__(HdxPublisher)
    publisher._owner_org = "org"
    publisher._maintainer = "me"
    return publisher


@pytest.fixture
def make_source_metadata() -> Callable[..., SourceMetadata]:
    """A layer's SourceMetadata with a small polygon report; keyword arguments override fields."""

    def make(source_name: str = "osm", **overrides: object) -> SourceMetadata:
        report = MetadataReport(
            feature_count=3,
            geometry_types={"POLYGON": 3},
            bbox=(85.0, 27.8, 85.4, 28.3),
            columns=[ColumnReport("id", "VARCHAR", 0, 0.0, 3, [])],
            summary="3 features.",
            temporal=None,
        )
        fields: dict[str, object] = {
            "snapshot_label": "2026-08-24",
            "dataset_source": "OpenStreetMap",
            "generated_utc": "2026-08-24T00:00:00Z",
            "oex_version": "0.4.16",
            "license_label": "Open Database License (ODC-ODbL)",
            "license_url": None,
            "pcode_source_date": None,
            "boundary": "user geom",
            "metadata": report,
            **overrides,
        }
        return SourceMetadata(source_name=source_name, **fields)

    return make


@pytest.fixture
def make_exporter() -> Callable[..., Exporter]:
    """An Exporter over a stub runner whose `name` is the source token the exporter reads."""

    def make(cfg: RootConfig, source: str = "osm") -> Exporter:
        runner = MagicMock()
        runner.name = source
        return Exporter(cfg, runner)

    return make


@pytest.fixture
def fake_pmtiles_build() -> Iterator[list[TileLayer]]:
    """Stubs the combined tileset build with a placeholder file; yields the layers it was given."""
    merged: list[TileLayer] = []

    def build(conn: object, layers: list[TileLayer], out_path: Path, **_: object) -> Path:
        merged.extend(layers)
        out_path.write_bytes(b"PMTiles")
        return out_path

    with (
        patch("oex.exporter.build_combined_pmtiles", side_effect=build),
        patch("oex.exporter.connect", return_value=MagicMock()),
    ):
        yield merged
