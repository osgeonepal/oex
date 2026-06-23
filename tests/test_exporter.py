"""Exporter helpers."""

from pathlib import Path

from oex.exporter import _remove_uploaded_outputs


def test_remove_uploaded_outputs_deletes_and_tolerates_missing(tmp_path: Path) -> None:
    zip1 = tmp_path / "buildings_osm_gpkg.zip"
    zip1.write_bytes(b"x")
    meta = tmp_path / "buildings_osm_metadata.json"
    meta.write_text("{}", encoding="utf-8")
    already_gone = tmp_path / "buildings_osm_shp.zip"

    _remove_uploaded_outputs([zip1, already_gone], meta)

    assert not zip1.exists()
    assert not meta.exists()


def test_remove_uploaded_outputs_without_metadata(tmp_path: Path) -> None:
    zip1 = tmp_path / "roads_osm_gpkg.zip"
    zip1.write_bytes(b"x")

    _remove_uploaded_outputs([zip1], None)

    assert not zip1.exists()
