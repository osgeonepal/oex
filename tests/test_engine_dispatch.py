"""OSM engine dispatch tests covering the new planet + geofabrik wiring.

Planet-engine end-to-end behavior is covered by tests/test_runner_planet.py;
this file just verifies the dispatch layer (engine validation, geofabrik
fast-fail paths). No network and no quackosm runs.
"""

from pathlib import Path

import pytest

from oex.config import ConfigError
from oex.config.loader import load_config
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
    """When country.parquet already exists, no download/quackosm rebuild runs."""
    cache_dir = tmp_path / "osm"
    snapshot_dir = cache_dir / "geofabrik" / "npl" / "2026-04-01"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "country.parquet").write_bytes(b"placeholder")

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
    runner = OsmRunner()
    runner.prepare(cfg)
    assert runner._snapshot_label == "2026-04-01"
    assert runner._snapshot_dir == snapshot_dir
    assert runner._country_parquet == snapshot_dir / "country.parquet"
