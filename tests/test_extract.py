"""Unit tests for osmium subprocess wrapper. No real osmium binary needed."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from oex.osm.extract import (
    OsmiumExtractError,
    OsmiumNotInstalledError,
    osmium_polygon_extract,
)

_NEPAL_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[80, 26], [88, 26], [88, 30], [80, 30], [80, 26]]],
}


def _fake_pbf(tmp_path: Path, name: str = "in.pbf") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00" * 16)
    return p


def test_raises_when_osmium_not_on_path(tmp_path: Path) -> None:
    pbf = _fake_pbf(tmp_path)
    with patch("oex.osm.extract.shutil.which", return_value=None):
        with pytest.raises(OsmiumNotInstalledError, match="osmium-tool binary not found"):
            osmium_polygon_extract(pbf, _NEPAL_POLYGON, tmp_path / "out.pbf")


def test_raises_when_input_pbf_missing(tmp_path: Path) -> None:
    with patch("oex.osm.extract.shutil.which", return_value="/usr/bin/osmium"):
        with pytest.raises(FileNotFoundError):
            osmium_polygon_extract(tmp_path / "missing.pbf", _NEPAL_POLYGON, tmp_path / "out.pbf")


def test_constructs_correct_command(tmp_path: Path) -> None:
    pbf = _fake_pbf(tmp_path)
    out_pbf = tmp_path / "out.pbf"
    captured: dict = {}

    def fake_run(cmd, capture_output, text):  # noqa: ANN001 - subprocess signature
        captured["cmd"] = cmd
        # Verify the geojson file passed to -p actually contains a FeatureCollection.
        p_index = cmd.index("-p") + 1
        with open(cmd[p_index]) as fh:
            captured["feature_collection"] = json.load(fh)
        out_pbf.write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("oex.osm.extract.shutil.which", return_value="/usr/bin/osmium"),
        patch("oex.osm.extract.subprocess.run", side_effect=fake_run),
    ):
        osmium_polygon_extract(pbf, _NEPAL_POLYGON, out_pbf)

    assert captured["cmd"][0] == "/usr/bin/osmium"
    assert captured["cmd"][1] == "extract"
    assert "--strategy" in captured["cmd"]
    assert "complete_ways" in captured["cmd"]
    assert str(pbf) in captured["cmd"]
    assert str(out_pbf) in captured["cmd"]
    assert captured["feature_collection"]["type"] == "FeatureCollection"
    assert captured["feature_collection"]["features"][0]["geometry"] == _NEPAL_POLYGON


def test_strategy_passes_through(tmp_path: Path) -> None:
    pbf = _fake_pbf(tmp_path)
    out_pbf = tmp_path / "out.pbf"

    def fake_run(cmd, capture_output, text):  # noqa: ANN001
        out_pbf.write_bytes(b"x")
        assert cmd[cmd.index("--strategy") + 1] == "smart"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("oex.osm.extract.shutil.which", return_value="/usr/bin/osmium"),
        patch("oex.osm.extract.subprocess.run", side_effect=fake_run),
    ):
        osmium_polygon_extract(pbf, _NEPAL_POLYGON, out_pbf, strategy="smart")


def test_raises_extract_error_on_nonzero_exit(tmp_path: Path) -> None:
    pbf = _fake_pbf(tmp_path)

    def fake_run(cmd, capture_output, text):  # noqa: ANN001
        return subprocess.CompletedProcess(cmd, 2, "", "ERROR: malformed polygon\n")

    with (
        patch("oex.osm.extract.shutil.which", return_value="/usr/bin/osmium"),
        patch("oex.osm.extract.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(OsmiumExtractError, match="malformed polygon"):
            osmium_polygon_extract(pbf, _NEPAL_POLYGON, tmp_path / "out.pbf")


def test_raises_when_output_is_empty(tmp_path: Path) -> None:
    pbf = _fake_pbf(tmp_path)
    out_pbf = tmp_path / "out.pbf"

    def fake_run(cmd, capture_output, text):  # noqa: ANN001
        out_pbf.write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("oex.osm.extract.shutil.which", return_value="/usr/bin/osmium"),
        patch("oex.osm.extract.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(OsmiumExtractError, match="no output"):
            osmium_polygon_extract(pbf, _NEPAL_POLYGON, out_pbf)


def test_geojson_tempfile_is_cleaned_up_on_failure(tmp_path: Path) -> None:
    """Even when osmium fails, the temp geojson must be removed."""
    pbf = _fake_pbf(tmp_path)
    captured_geojson_paths: list[str] = []

    def fake_run(cmd, capture_output, text):  # noqa: ANN001
        p_index = cmd.index("-p") + 1
        captured_geojson_paths.append(cmd[p_index])
        return subprocess.CompletedProcess(cmd, 99, "", "boom")

    with (
        patch("oex.osm.extract.shutil.which", return_value="/usr/bin/osmium"),
        patch("oex.osm.extract.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(OsmiumExtractError):
            osmium_polygon_extract(pbf, _NEPAL_POLYGON, tmp_path / "out.pbf")

    assert captured_geojson_paths
    for path in captured_geojson_paths:
        assert not Path(path).exists(), f"temp geojson left behind: {path}"
