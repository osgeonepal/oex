"""StateStore: atomic per-(iso3, source) resume state."""

import json
from pathlib import Path

from oex.state import StateStore


def _store(tmp_path: Path, *, iso3: str = "NPL", source: str = "osm") -> StateStore:
    return StateStore(path=tmp_path / ".state.json", iso3=iso3, source=source)


def test_get_returns_none_on_first_load(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get("buildings") is None


def test_mark_built_then_uploaded_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    zip1 = tmp_path / "buildings.gpkg.zip"
    zip1.write_bytes(b"x")
    zip2 = tmp_path / "buildings.shp.zip"
    zip2.write_bytes(b"y")

    store.mark_built(
        "buildings",
        snapshot_label="2026-05-09",
        zip_paths=[zip1, zip2],
        metadata_json_path=None,
    )
    assert store.is_built("buildings", snapshot_label="2026-05-09")
    assert not store.is_uploaded("buildings", snapshot_label="2026-05-09")

    store.mark_uploaded("buildings", hdx_dataset="hotosm_npl_buildings")
    assert store.is_uploaded("buildings", snapshot_label="2026-05-09")

    reopened = _store(tmp_path)
    assert reopened.is_uploaded("buildings", snapshot_label="2026-05-09")
    entry = reopened.get("buildings")
    assert entry is not None
    assert entry.hdx_dataset == "hotosm_npl_buildings"


def test_snapshot_label_mismatch_invalidates_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    zip1 = tmp_path / "buildings.zip"
    zip1.write_bytes(b"x")
    store.mark_built(
        "buildings", snapshot_label="2026-05-09", zip_paths=[zip1], metadata_json_path=None
    )
    assert not store.is_built("buildings", snapshot_label="2026-06-01")


def test_missing_zip_invalidates_built_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    zip1 = tmp_path / "buildings.zip"
    zip1.write_bytes(b"x")
    store.mark_built(
        "buildings", snapshot_label="2026-05-09", zip_paths=[zip1], metadata_json_path=None
    )
    zip1.unlink()
    assert not store.is_built("buildings", snapshot_label="2026-05-09")


def test_reset_clears_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    zip1 = tmp_path / "buildings.zip"
    zip1.write_bytes(b"x")
    store.mark_built(
        "buildings", snapshot_label="2026-05-09", zip_paths=[zip1], metadata_json_path=None
    )
    store.reset("buildings")
    assert store.get("buildings") is None
    reopened = _store(tmp_path)
    assert reopened.get("buildings") is None


def test_atomic_write_no_tmp_left_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    zip1 = tmp_path / "buildings.zip"
    zip1.write_bytes(b"x")
    store.mark_built(
        "buildings", snapshot_label="2026-05-09", zip_paths=[zip1], metadata_json_path=None
    )
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_corrupt_state_starts_fresh(tmp_path: Path) -> None:
    state_path = tmp_path / ".state.json"
    state_path.write_text("{not json", encoding="utf-8")
    store = _store(tmp_path)
    assert store.get("buildings") is None


def test_other_iso3_or_source_starts_fresh(tmp_path: Path) -> None:
    state_path = tmp_path / ".state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iso3": "AFG",
                "source": "osm",
                "categories": {
                    "buildings": {
                        "snapshot_label": "2026-05-09",
                        "built_utc": "x",
                        "zip_paths": [],
                        "metadata_json_path": None,
                        "uploaded_utc": "y",
                        "hdx_dataset": "hotosm_afg_buildings",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = _store(tmp_path, iso3="NPL", source="osm")
    assert store.get("buildings") is None
