"""OSM engine dispatch: engine validation, fallback and Geofabrik's fast-fail paths, with the
network and quackosm stubbed.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from oex.config import ConfigError
from oex.config.schema import RootConfig
from oex.osm.errors import OsmEngineUnavailableError
from oex.osm.runner import OsmRunner, _parquet_fingerprint

LoadConfig = Callable[[str], RootConfig]

SQUARE = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [[[85.0, 27.0], [85.1, 27.0], [85.1, 27.1], [85.0, 27.1], [85.0, 27.0]]],
    }
)

BUILDINGS = """
categories:
  - name: buildings
    osm:
      enabled: true
      filter:
        building: true
"""


@pytest.mark.parametrize(
    ("osm_block", "message"),
    [
        ("engine: invalid_engine", "invalid_engine"),
        ("engine: planet", "pbf_path"),
        ("engine: postpass\n    fallback_engine: nonsense", "fallback_engine"),
        ("engine: postpass\n    fallback_engine: postpass", "must differ"),
    ],
    ids=["unknown engine", "planet without pbf_path", "unknown fallback", "fallback is primary"],
)
def test_an_invalid_engine_setting_is_rejected_at_config_load(
    load_yaml_config: LoadConfig, osm_block: str, message: str
) -> None:
    """A fallback equal to the primary would retry the same outage."""
    with pytest.raises(ConfigError, match=message):
        load_yaml_config(f"iso3: NPL\nkey: t\nsource:\n  osm:\n    {osm_block}\n")


def test_rawdata_engine_is_accepted_at_config_load(load_yaml_config: LoadConfig) -> None:
    cfg = load_yaml_config(
        "iso3: NPL\nkey: t\nsource:\n  osm:\n    engine: rawdata\ncategories: []\n"
    )
    assert cfg.source["osm"].engine == "rawdata"


def test_geofabrik_engine_requires_iso3(load_yaml_config: LoadConfig, tmp_path: Path) -> None:
    cfg = load_yaml_config(
        f"""
key: t
source:
  osm:
    engine: geofabrik
    cache_dir: {tmp_path / "osm"}
categories: []
"""
    )
    with pytest.raises(ValueError, match="iso3"):
        OsmRunner().prepare(cfg)


def test_geofabrik_reuses_an_existing_snapshot_parquet_without_rebuilding(
    load_yaml_config: LoadConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is inline and the index lookup stubbed, so nothing leaves the machine."""
    monkeypatch.setattr(
        "oex.osm.runner.lookup_country",
        lambda *a, **k: type("E", (), {"pbf_url": "https://example.com/npl.osm.pbf"})(),
    )
    cache_dir = tmp_path / "osm"
    snapshot_dir = cache_dir / "geofabrik" / "npl" / "2026-04-01"
    snapshot_dir.mkdir(parents=True)
    cfg = load_yaml_config(
        f"""
iso3: NPL
key: t
boundary:
  geom: '{SQUARE}'
source:
  osm:
    engine: geofabrik
    cache_dir: {cache_dir}
    snapshot: 2026-04-01
{BUILDINGS}"""
    )
    cached = snapshot_dir / f"country-{_parquet_fingerprint(cfg, clip=True)}.parquet"
    cached.write_bytes(b"placeholder")

    runner = OsmRunner()
    runner.prepare(cfg)
    assert runner._snapshot_label == "2026-04-01"
    assert runner._snapshot_dir == snapshot_dir
    assert runner._country_parquet == cached


@pytest.fixture
def fallback_config(load_yaml_config: LoadConfig, tmp_path: Path) -> RootConfig:
    return load_yaml_config(
        f"""
iso3: NPL
key: t
source:
  osm:
    engine: postpass
    fallback_engine: rawdata
    cache_dir: {tmp_path / "osm"}
{BUILDINGS}"""
    )


def test_the_fallback_engine_runs_when_the_primary_fails(
    fallback_config: RootConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One upstream outage must not stop a run when a fallback is configured."""
    called: list[str] = []

    def fake(self, cfg_, src, engine):  # noqa: ANN001
        called.append(engine)
        if engine == "postpass":
            raise OsmEngineUnavailableError("Postpass returned HTTP 503")

    monkeypatch.setattr(OsmRunner, "_prepare_engine", fake)
    OsmRunner().prepare(fallback_config)
    assert called == ["postpass", "rawdata"]


def test_the_fallback_does_not_cover_a_defect_in_the_data(
    fallback_config: RootConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an outage may fall back; bad data has to surface where it happened."""
    called: list[str] = []

    def fake(self, cfg_, src, engine):  # noqa: ANN001
        called.append(engine)
        raise RuntimeError("Postpass returned a geometry oex cannot read")

    monkeypatch.setattr(OsmRunner, "_prepare_engine", fake)
    with pytest.raises(RuntimeError, match="cannot read"):
        OsmRunner().prepare(fallback_config)
    assert called == ["postpass"]


class _DatelessHead:
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None


def test_an_extract_geofabrik_will_not_date_falls_back_like_any_other_outage(
    load_yaml_config: LoadConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undatable extract is an outage: recovering from it must not depend on the shape."""
    cfg = load_yaml_config(
        f"""
iso3: NPL
key: t
source:
  osm:
    engine: geofabrik
    fallback_engine: rawdata
    planet_fallback: false
    cache_dir: {tmp_path / "osm"}
{BUILDINGS}"""
    )
    called: list[str] = []

    monkeypatch.setattr(
        "oex.osm.runner.lookup_country",
        lambda *a, **k: type("E", (), {"pbf_url": "https://example.com/npl.osm.pbf"})(),
    )
    monkeypatch.setattr("oex.http.session.head", lambda *a, **k: _DatelessHead())
    monkeypatch.setattr(
        OsmRunner, "_prepare_rawdata", lambda self, cfg_, src: called.append("rawdata")
    )

    OsmRunner().prepare(cfg)
    assert called == ["rawdata"]
