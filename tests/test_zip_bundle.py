"""Zip bundling with README and config snapshot."""

import zipfile
from pathlib import Path

from oex.zip_bundle import make_zip


def test_make_zip_includes_files_readme_and_config(tmp_path: Path) -> None:
    src = tmp_path / "stage"
    src.mkdir()
    (src / "data.gpkg").write_bytes(b"\x00\x01\x02")
    (src / "data.shp").write_bytes(b"\x10\x20")

    zip_path = tmp_path / "out.zip"
    make_zip(
        src,
        zip_path,
        readme_lines=["Country: NPL", "Snapshot: 2026-05-01"],
        config_snapshot={"iso3": "NPL", "category": "Buildings"},
    )

    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "data.gpkg" in names
        assert "data.shp" in names
        assert "README.txt" in names
        assert "config.yaml" in names
        readme = zf.read("README.txt").decode("utf-8")
        assert "NPL" in readme
    assert not src.exists()
