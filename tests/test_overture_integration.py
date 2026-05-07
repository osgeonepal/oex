"""End-to-end Overture export over a small Nepal AOI.

Marked `integration`: needs network and reads from the public Overture S3
bucket. Skipped by `just test`; run with `just test-integration`.

Coverage matrix:
- All 8 default categories (Buildings, Roads, Hospitals, Schools, Rivers,
  Land Use, Transportation Hubs, Settlements).
- All three output formats (gpkg, shp, geojson).
- Tiny Pokhara AOI to keep S3 read time bounded.
"""

import zipfile
from pathlib import Path

import pytest

from oex.config.loader import (
    apply_overrides,
    load_config,
    select_categories,
)
from oex.exporter import Exporter
from oex.overture.runner import OvertureRunner

pytestmark = pytest.mark.integration


_GEOM = (
    '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
    '"geometry":{"type":"Polygon","coordinates":[[[83.95,28.20],'
    "[84.00,28.20],[84.00,28.25],[83.95,28.25],[83.95,28.20]]]}}]}"
)


def _yaml(tmp: Path, formats: list[str]) -> Path:
    p = tmp / "nepal.yaml"
    p.write_text(
        f"""
iso3: NPL
key: it_overture
dataset_name: Pokhara IT
boundary:
  geom: |
    {_GEOM}
output:
  dir: {tmp / "output"}
  formats: {formats}
parallel:
  enabled: true
hdx:
  push: false
""",
        encoding="utf-8",
    )
    return p


@pytest.mark.slow
def test_overture_buildings_gpkg(tmp_path: Path) -> None:
    cfg = load_config(_yaml(tmp_path, ["gpkg"]))
    cfg = apply_overrides(cfg, {})
    cfg = select_categories(cfg, "buildings")
    result = Exporter(cfg, OvertureRunner()).run()
    cat = result.categories["Buildings"]
    assert cat.status == "ok", cat.error
    assert cat.feature_count > 0
    assert len(cat.zip_paths) == 1
    with zipfile.ZipFile(cat.zip_paths[0]) as zf:
        names = zf.namelist()
        assert any(n.endswith(".gpkg") for n in names)
        assert "README.txt" in names
        assert "config.yaml" in names


@pytest.mark.slow
def test_overture_all_categories_all_formats(tmp_path: Path) -> None:
    """Run every default theme across all three formats."""
    cfg = load_config(_yaml(tmp_path, ["gpkg", "shp", "geojson"]))
    result = Exporter(cfg, OvertureRunner()).run()

    expected = {
        "Buildings",
        "Roads",
        "Hospitals",
        "Schools",
        "Rivers",
        "Land Use",
        "Transportation Hubs",
        "Settlements",
    }
    assert set(result.categories) == expected
    failures = {k: v.error for k, v in result.categories.items() if v.status == "failed"}
    assert not failures, failures

    # Categories with data should have produced one zip per format.
    for name, cat in result.categories.items():
        if cat.status != "ok":
            continue
        formats_in_zips = {p.stem.rsplit("_", 1)[-1] for p in cat.zip_paths}
        assert formats_in_zips <= {"gpkg", "shp", "geojson"}
        for zp in cat.zip_paths:
            assert zp.exists(), f"{name}: missing {zp}"
            assert zp.stat().st_size > 0
            with zipfile.ZipFile(zp) as zf:
                names = zf.namelist()
                assert "README.txt" in names
                assert "config.yaml" in names
