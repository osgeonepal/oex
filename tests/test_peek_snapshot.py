"""`peek_snapshot_label` drives the resume short-circuit, so a stale answer skips a country.

Reading the cache directory answers with the snapshot already exported, which makes resume
decide the work is done and pins the country to that snapshot forever.
"""

from pathlib import Path

import pytest

from oex.config.schema import BoundaryConfig, OsmSourceConfig, RootConfig
from oex.osm.geofabrik import GeofabrikUnavailableError
from oex.osm.runner import OsmRunner


class _Extract:
    pbf_url = "https://download.geofabrik.de/asia/afghanistan-latest.osm.pbf"


def _cfg(cache_dir: Path) -> RootConfig:
    cfg = RootConfig(iso3="AFG", key="t", boundary=BoundaryConfig())
    cfg.source["osm"] = OsmSourceConfig(engine="geofabrik", cache_dir=str(cache_dir))
    return cfg


@pytest.fixture
def cached_august(tmp_path: Path) -> Path:
    (tmp_path / "geofabrik" / "afg" / "2026-08-06").mkdir(parents=True)
    return tmp_path


def test_peek_reports_the_upstream_snapshot_not_the_cached_one(cached_august, monkeypatch):
    """The cache holds August; Geofabrik has rebuilt since, so the export must not skip."""
    monkeypatch.setattr("oex.osm.runner.lookup_country", lambda *a, **k: _Extract())
    monkeypatch.setattr(
        OsmRunner, "_resolve_geofabrik_snapshot", staticmethod(lambda *a: "2026-09-06")
    )
    assert OsmRunner().peek_snapshot_label(_cfg(cached_august)) == "2026-09-06"


def test_peek_gives_up_rather_than_guessing_when_geofabrik_is_unreachable(
    cached_august, monkeypatch
):
    """Returning the cached label would silently skip; None just means run and find out."""

    def unavailable(*_args, **_kwargs):
        raise GeofabrikUnavailableError("index unreachable")

    monkeypatch.setattr("oex.osm.runner.lookup_country", unavailable)
    assert OsmRunner().peek_snapshot_label(_cfg(cached_august)) is None


def test_peek_returns_none_before_the_country_has_ever_been_exported(tmp_path):
    assert OsmRunner().peek_snapshot_label(_cfg(tmp_path)) is None
