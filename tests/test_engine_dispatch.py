"""OSM engine dispatch: planet_parquet vs geofabrik wiring."""

from pathlib import Path

import pytest

from oex.config.loader import apply_overrides, load_config
from oex.osm.runner import OsmRunner


def test_unknown_engine_raises(tmp_path: Path) -> None:
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
    cfg = load_config(yaml)
    runner = OsmRunner()
    with pytest.raises(ValueError, match="Unknown osm.engine"):
        runner.prepare(cfg)


def test_planet_parquet_missing_cache_raises(tmp_path: Path) -> None:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: t
source:
  osm:
    engine: planet_parquet
    cache_dir: {tmp_path / "nonexistent"}
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    runner = OsmRunner()
    with pytest.raises(FileNotFoundError, match="planet cache"):
        runner.prepare(cfg)


def test_planet_parquet_picks_latest_snapshot(tmp_path: Path) -> None:
    cache_dir = tmp_path / "osm"
    planet_root = cache_dir / "planet"
    (planet_root / "2026-01-01").mkdir(parents=True)
    (planet_root / "2026-05-01").mkdir(parents=True)

    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: t
source:
  osm:
    engine: planet_parquet
    cache_dir: {cache_dir}
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    runner = OsmRunner()
    runner.prepare(cfg)
    assert runner._snapshot_label == "2026-05-01"


def test_planet_parquet_explicit_snapshot(tmp_path: Path) -> None:
    cache_dir = tmp_path / "osm"
    planet_root = cache_dir / "planet"
    (planet_root / "2026-01-01").mkdir(parents=True)
    (planet_root / "2026-05-01").mkdir(parents=True)

    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: t
source:
  osm:
    engine: planet_parquet
    cache_dir: {cache_dir}
    snapshot: 2026-01-01
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    runner = OsmRunner()
    runner.prepare(cfg)
    assert runner._snapshot_label == "2026-01-01"


def test_geofabrik_engine_requires_iso3(tmp_path: Path) -> None:
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
key: t
source:
  osm:
    engine: geofabrik
    cache_dir: {tmp_path / "osm"}
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    cfg = apply_overrides(cfg, {})
    runner = OsmRunner()
    with pytest.raises(ValueError, match="iso3"):
        runner.prepare(cfg)


def test_geofabrik_reuses_existing_snapshot(tmp_path: Path) -> None:
    """If a snapshot already exists for the country, no download happens."""
    cache_dir = tmp_path / "osm"
    snapshot_dir = cache_dir / "geofabrik" / "npl" / "2026-04-01"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "buildings.parquet").write_bytes(b"placeholder")

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
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    runner = OsmRunner()
    runner.prepare(cfg)
    assert runner._snapshot_label == "2026-04-01"
    assert runner._snapshot_dir == snapshot_dir
