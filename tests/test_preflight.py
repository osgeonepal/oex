"""Writable-paths preflight."""

from pathlib import Path

import pytest

from oex.config.schema import (
    DuckdbConfig,
    OsmSourceConfig,
    OutputConfig,
    PcodesSourceConfig,
    RootConfig,
)
from oex.preflight import PreflightError, check_writable_paths


def _cfg(tmp_path: Path) -> RootConfig:
    cfg = RootConfig(iso3="NPL")
    cfg.output = OutputConfig(dir=str(tmp_path / "out"))
    cfg.duckdb = DuckdbConfig(temp_dir=str(tmp_path / "duck"))
    cfg.source = {
        "osm": OsmSourceConfig(
            enabled=True,
            cache_dir=str(tmp_path / "osm-cache"),
        ),
        "pcodes": PcodesSourceConfig(
            enabled=True,
            cache_dir=str(tmp_path / "pcodes-cache"),
        ),
    }
    return cfg


def test_creates_missing_dirs_and_passes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    check_writable_paths(cfg)
    for sub in ("out", "duck", "osm-cache", "pcodes-cache"):
        assert (tmp_path / sub).is_dir()


def test_fails_on_readonly_path(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ro = tmp_path / "out"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(PreflightError, match="cannot write"):
            check_writable_paths(cfg)
    finally:
        ro.chmod(0o700)


def test_skips_pcodes_check_when_disabled(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.source["pcodes"].enabled = False
    check_writable_paths(cfg)
    assert not (tmp_path / "pcodes-cache").exists()


def test_no_temp_files_left_behind(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    check_writable_paths(cfg)
    for sub in ("out", "duck", "osm-cache", "pcodes-cache"):
        leftovers = [p for p in (tmp_path / sub).iterdir() if p.name.startswith(".oex_preflight")]
        assert leftovers == []
