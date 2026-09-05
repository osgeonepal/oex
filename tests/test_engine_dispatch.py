"""OSM engine dispatch tests covering the new planet + geofabrik wiring.

Planet-engine end-to-end behavior is covered by tests/test_runner_planet.py;
this file just verifies the dispatch layer (engine validation, geofabrik
fast-fail paths). No network and no quackosm runs.
"""

from pathlib import Path

import pytest

from oex.config import ConfigError
from oex.config.loader import load_config
from oex.osm.errors import OsmEngineUnavailableError
from oex.osm.runner import OsmRunner


def test_unknown_engine_rejected_at_config_load(tmp_path: Path) -> None:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        """
iso3: NPL
key: t
source:
  osm:
    engine: invalid_engine
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="invalid_engine"):
        load_config(yaml)


def test_planet_engine_requires_pbf_path_at_config_load(tmp_path: Path) -> None:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        """
iso3: NPL
key: t
source:
  osm:
    engine: planet
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="pbf_path"):
        load_config(yaml)


def test_geofabrik_engine_requires_iso3(tmp_path: Path) -> None:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
key: t
source:
  osm:
    engine: geofabrik
    cache_dir: {tmp_path / "osm"}
categories: []
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    runner = OsmRunner()
    with pytest.raises(ValueError, match="iso3"):
        runner.prepare(cfg)


def test_geofabrik_reuses_existing_snapshot(tmp_path: Path) -> None:
    """When the parquet for this boundary + filters exists, nothing is rebuilt."""
    from oex.osm.runner import _parquet_fingerprint

    cache_dir = tmp_path / "osm"
    snapshot_dir = cache_dir / "geofabrik" / "npl" / "2026-04-01"
    snapshot_dir.mkdir(parents=True)

    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: t
source:
  osm:
    engine: geofabrik
    cache_dir: {cache_dir}
    snapshot: 2026-04-01
categories:
  - name: buildings
    osm:
      enabled: true
      filter:
        building: true
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    cached = snapshot_dir / f"country-{_parquet_fingerprint(cfg, clip=True)}.parquet"
    cached.write_bytes(b"placeholder")

    runner = OsmRunner()
    runner.prepare(cfg)
    assert runner._snapshot_label == "2026-04-01"
    assert runner._snapshot_dir == snapshot_dir
    assert runner._country_parquet == cached


def test_rawdata_engine_is_accepted_at_config_load(tmp_path: Path) -> None:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        """
iso3: NPL
key: t
source:
  osm:
    engine: rawdata
categories: []
""",
        encoding="utf-8",
    )
    assert load_config(yaml).source["osm"].engine == "rawdata"


def test_an_unknown_fallback_engine_is_rejected(tmp_path: Path) -> None:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        """
iso3: NPL
key: t
source:
  osm:
    engine: postpass
    fallback_engine: nonsense
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="fallback_engine"):
        load_config(yaml)


def test_a_fallback_matching_the_primary_is_rejected(tmp_path: Path) -> None:
    """Falling back to the engine that just failed would retry the same outage."""
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        """
iso3: NPL
key: t
source:
  osm:
    engine: postpass
    fallback_engine: postpass
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must differ"):
        load_config(yaml)


def _fallback_config(tmp_path: Path) -> Path:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: t
source:
  osm:
    engine: postpass
    fallback_engine: rawdata
    cache_dir: {tmp_path / "osm"}
categories:
  - name: buildings
    osm:
      enabled: true
      filter:
        building: true
""",
        encoding="utf-8",
    )
    return yaml


def test_the_fallback_engine_runs_when_the_primary_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One upstream outage must not stop a run when a fallback is configured."""
    cfg = load_config(_fallback_config(tmp_path))
    called: list[str] = []

    def fake(self, cfg_, src, engine):  # noqa: ANN001
        called.append(engine)
        if engine == "postpass":
            raise OsmEngineUnavailableError("Postpass returned HTTP 503")

    monkeypatch.setattr(OsmRunner, "_prepare_engine", fake)
    OsmRunner().prepare(cfg)
    assert called == ["postpass", "rawdata"]


def test_the_fallback_does_not_cover_a_defect_in_the_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an outage may fall back; bad data has to surface where it happened."""
    cfg = load_config(_fallback_config(tmp_path))
    called: list[str] = []

    def fake(self, cfg_, src, engine):  # noqa: ANN001
        called.append(engine)
        raise RuntimeError("Postpass returned a geometry oex cannot read")

    monkeypatch.setattr(OsmRunner, "_prepare_engine", fake)
    with pytest.raises(RuntimeError, match="cannot read"):
        OsmRunner().prepare(cfg)
    assert called == ["postpass"]
