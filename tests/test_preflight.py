"""Writable-paths preflight."""

import errno
import os
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


def test_preflight_creates_every_missing_configured_directory(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    check_writable_paths(cfg)
    for sub in ("out", "duck", "osm-cache", "pcodes-cache"):
        assert (tmp_path / sub).is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only mode")
def test_a_read_only_output_directory_fails_preflight(tmp_path: Path) -> None:
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


def test_a_directory_that_refuses_renames_fails_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_renames_that_move_the_file(source: Path, destination: Path) -> None:
        # POSIX defines renaming a file onto itself as a successful no-op.
        if Path(source) != Path(destination):
            raise OSError(errno.EPERM, "rename refused")

    monkeypatch.setattr("oex.preflight.os.replace", refuse_renames_that_move_the_file)
    with pytest.raises(PreflightError, match="cannot rename"):
        check_writable_paths(_cfg(tmp_path))
    assert not any(p.name.startswith(".oex_preflight") for p in (tmp_path / "out").iterdir())
