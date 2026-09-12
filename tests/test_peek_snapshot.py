"""`peek_snapshot_label` drives the resume short-circuit, so a stale answer skips a country."""

from pathlib import Path

import pytest
import requests

from oex.config.schema import BoundaryConfig, OsmSourceConfig, RootConfig
from oex.osm.geofabrik import GeofabrikUnavailableError
from oex.osm.runner import OsmRunner


class _Extract:
    pbf_url = "https://download.geofabrik.de/asia/afghanistan-latest.osm.pbf"


class _Head:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers

    def raise_for_status(self) -> None:
        return None


def _cfg(cache_dir: Path) -> RootConfig:
    cfg = RootConfig(iso3="AFG", key="t", boundary=BoundaryConfig())
    cfg.source["osm"] = OsmSourceConfig(engine="geofabrik", cache_dir=str(cache_dir))
    return cfg


@pytest.fixture
def cached_august(tmp_path: Path) -> Path:
    (tmp_path / "geofabrik" / "afg" / "2026-08-06").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def index_reachable(monkeypatch):
    monkeypatch.setattr("oex.osm.runner.lookup_country", lambda *a, **k: _Extract())


def test_peek_reports_the_upstream_snapshot_not_the_cached_one(
    cached_august, index_reachable, monkeypatch
):
    """The cache holds August; Geofabrik has rebuilt since, so the export must not skip."""
    monkeypatch.setattr(
        "oex.osm.runner.requests.head",
        lambda *a, **k: _Head({"Last-Modified": "Sun, 06 Sep 2026 23:09:31 GMT"}),
    )
    assert OsmRunner().peek_snapshot_label(_cfg(cached_august)) == "2026-09-06"


def test_peek_gives_up_rather_than_guessing_when_the_index_is_unreachable(
    cached_august, monkeypatch
):
    """Returning the cached label would silently skip; None just means run and find out."""

    def unavailable(*_args, **_kwargs):
        raise GeofabrikUnavailableError("index unreachable")

    monkeypatch.setattr("oex.osm.runner.lookup_country", unavailable)
    assert OsmRunner().peek_snapshot_label(_cfg(cached_august)) is None


def test_peek_gives_up_when_the_extract_itself_is_unreachable(
    cached_august, index_reachable, monkeypatch
):
    """The index answering does not mean the PBF does, and only the PBF carries the date."""

    def unreachable(*_args, **_kwargs):
        raise requests.ConnectionError("HEAD failed")

    monkeypatch.setattr("oex.osm.runner.requests.head", unreachable)
    assert OsmRunner().peek_snapshot_label(_cfg(cached_august)) is None


def test_peek_gives_up_when_geofabrik_reports_no_last_modified(
    cached_august, index_reachable, monkeypatch
):
    """A PBF that answers without a date cannot be dated, and guessing is the bug."""
    monkeypatch.setattr("oex.osm.runner.requests.head", lambda *a, **k: _Head({}))
    assert OsmRunner().peek_snapshot_label(_cfg(cached_august)) is None


def test_peek_returns_none_before_the_country_has_ever_been_exported(tmp_path):
    assert OsmRunner().peek_snapshot_label(_cfg(tmp_path)) is None
