"""End-to-end OSM export over Nepal across both engines.

Marked `integration`: downloads the Geofabrik Nepal regional extract (~50 MB),
runs quackosm to materialise per-theme parquet, and exports across all
formats.

We exercise both engines:

- `geofabrik`: full happy path including network download + cache build.
- `planet_parquet`: same Nepal PBF treated as a pre-built planet cache, to
  prove the planet branch works without actually downloading 90 GB.
"""

import shutil
import zipfile
from pathlib import Path

import pytest

from oex.config.loader import (
    apply_overrides,
    load_config,
    select_categories,
)
from oex.exporter import Exporter
from oex.osm.build_cache import build_cache
from oex.osm.fetch_planet import download_pbf
from oex.osm.runner import OsmRunner

pytestmark = pytest.mark.integration


_GEOM = (
    '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
    '"geometry":{"type":"Polygon","coordinates":[[[83.95,28.20],'
    "[84.00,28.20],[84.00,28.25],[83.95,28.25],[83.95,28.20]]]}}]}"
)


@pytest.fixture(scope="module")
def nepal_pbf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Download the Geofabrik Nepal PBF once for the whole module."""
    work = tmp_path_factory.mktemp("osm-nepal-pbf")
    result = download_pbf(
        "https://download.geofabrik.de/asia/nepal-latest.osm.pbf",
        work,
        filename="nepal-latest.osm.pbf",
    )
    return result.path


@pytest.fixture(scope="module")
def planet_style_cache(
    tmp_path_factory: pytest.TempPathFactory, nepal_pbf: Path
) -> tuple[Path, str]:
    """Build a small `planet`-style cache from the Nepal PBF (buildings + roads)."""
    work = tmp_path_factory.mktemp("osm-planet-cache")
    cache_root = work / "planet"

    bootstrap = work / "bootstrap.yaml"
    bootstrap.write_text(
        f"""
iso3: NPL
key: planet_bootstrap
source:
  osm:
    cache_dir: {work}
""",
        encoding="utf-8",
    )
    cfg = load_config(bootstrap)
    manifest = build_cache(
        cfg,
        nepal_pbf,
        cache_root=cache_root,
        snapshot="2026-test",
        themes_filter=["buildings", "roads"],
    )
    assert manifest.themes, "build_cache produced no themes"
    return work, "2026-test"


@pytest.mark.slow
def test_geofabrik_engine_all_formats(tmp_path: Path, nepal_pbf: Path) -> None:
    """Geofabrik engine: pre-seed PBF cache so we exercise the build path without re-downloading."""
    work = tmp_path / "osm"
    pbf_dir = work / "geofabrik" / "npl" / "_pbf"
    pbf_dir.mkdir(parents=True)
    shutil.copy(nepal_pbf, pbf_dir / "nepal-latest.osm.pbf")

    yaml = tmp_path / "country.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: it_osm_gf
dataset_name: Pokhara OSM GF
boundary:
  geom: |
    {_GEOM}
output:
  dir: {tmp_path / "output"}
  formats: [gpkg, shp, geojson]
parallel:
  enabled: true
hdx:
  push: false
source:
  osm:
    engine: geofabrik
    cache_dir: {work}
    keep_pbf: true
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    cfg = apply_overrides(cfg, {})
    result = Exporter(cfg, OsmRunner()).run()

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

    # Buildings is guaranteed to have data over the AOI.
    buildings = result.categories["Buildings"]
    assert buildings.status == "ok"
    assert buildings.feature_count > 0
    formats = {p.stem.rsplit("_", 1)[-1] for p in buildings.zip_paths}
    assert formats == {"gpkg", "shp", "geojson"}
    for zp in buildings.zip_paths:
        with zipfile.ZipFile(zp) as zf:
            names = zf.namelist()
            assert "README.txt" in names
            assert "config.yaml" in names


@pytest.mark.slow
def test_planet_parquet_engine(
    tmp_path: Path,
    planet_style_cache: tuple[Path, str],
) -> None:
    """planet_parquet engine: query the prebuilt cache."""
    cache_dir, snapshot = planet_style_cache
    yaml = tmp_path / "country.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: it_osm_planet
dataset_name: Pokhara OSM Planet
boundary:
  geom: |
    {_GEOM}
output:
  dir: {tmp_path / "output"}
  formats: [gpkg, shp]
parallel:
  enabled: false
hdx:
  push: false
source:
  osm:
    engine: planet_parquet
    cache_dir: {cache_dir}
    snapshot: {snapshot}
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml)
    cfg = select_categories(cfg, "buildings")
    result = Exporter(cfg, OsmRunner()).run()

    cat = result.categories["Buildings"]
    assert cat.status == "ok", cat.error
    assert cat.feature_count > 0
    formats = {p.stem.rsplit("_", 1)[-1] for p in cat.zip_paths}
    assert formats == {"gpkg", "shp"}
