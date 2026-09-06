"""What the writer names, the publisher must be able to read back.

`oex-cli metadata --prune` decides which published HDX resources to delete by parsing
artifact filenames, so a writer that names a file the parser cannot read would either
orphan a resource or delete the wrong one. This runs a real export and parses its own
output, rather than asserting the two halves separately.
"""

import json
from pathlib import Path

import duckdb
import pytest

from oex.config.loader import load_config
from oex.exporter import Exporter
from oex.hdx_publisher import _split_artifact_filename
from oex.sources.file import FileRunner

BOUNDARY = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [[[85.0, 27.4], [85.7, 27.4], [85.7, 27.9], [85.0, 27.9], [85.0, 27.4]]],
    }
)

CONFIG = """
iso3: NPL
key: test
frequency: as_needed
boundary:
  geom: '{boundary}'
source:
  osm:
    enabled: false
  overture:
    enabled: false
  pcodes:
    enabled: false
  file:
    enabled: true
output:
  formats: [{formats}]
  dir: {out}
  {zip_line}
  s3:
    enabled: false
    name_include_source: {include_source}
  pmtiles:
    enabled: false
  report:
    enabled: false
hdx:
  push: false
categories:
  - name: {category}
    file:
      enabled: true
      path: {path}
      select:
        name: Bridge_Nm
"""


@pytest.fixture
def shapefile(tmp_path: Path) -> Path:
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute(
        "CREATE TABLE f AS SELECT * FROM (VALUES "
        "('a', ST_Point(85.3, 27.6)), ('b', ST_Point(85.4, 27.7))) AS t(\"Bridge_Nm\", geom)"
    )
    path = tmp_path / "bridges.shp"
    conn.execute(f"COPY f TO '{path}' WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile', SRS 'EPSG:4326')")
    return path


def run_export(
    tmp_path: Path,
    shapefile: Path,
    *,
    formats: str,
    zip_line: str,
    category: str,
    include_source: bool = True,
):
    out = tmp_path / "out"
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        CONFIG.format(
            boundary=BOUNDARY,
            formats=formats,
            out=out,
            zip_line=zip_line,
            category=category,
            path=shapefile,
            include_source="true" if include_source else "false",
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    Exporter(cfg, runner=FileRunner()).run()
    return sorted(p.name for p in (out / "npl" / "file").iterdir() if p.is_file())


@pytest.mark.parametrize(
    ("formats", "zip_line", "category", "include_source"),
    [
        ("gpkg, geojson", "", "bridge_damage", True),
        ("gpkg, geojson", "zip_formats: [gpkg]", "bridge_damage", True),
        # a category whose name ends in a source token, which naive parsing mis-splits
        ("gpkg", "", "roads_osm", True),
        # without the source token the format is the only thing after the slug
        ("gpkg, geojson", "", "bridge_damage", False),
        ("gpkg, geojson", "zip_formats: [gpkg]", "roads_osm", False),
    ],
)
def test_every_artifact_parses_back_to_its_category_and_source(
    tmp_path, shapefile, formats, zip_line, category, include_source
):
    names = run_export(
        tmp_path,
        shapefile,
        formats=formats,
        zip_line=zip_line,
        category=category,
        include_source=include_source,
    )
    layers = [n for n in names if not n.endswith((".json", ".html"))]
    assert layers, f"export produced no layer artifacts: {names}"
    prefix = "test_npl_"
    for name in layers:
        assert name.startswith(prefix), name
        slug, source, fmt = _split_artifact_filename(name[len(prefix) :], {category})
        assert slug == category, f"{name} parsed to slug {slug!r}"
        assert source == ("file" if include_source else ""), f"{name} parsed to source {source!r}"
        assert fmt in {"gpkg", "geojson"}, f"{name} parsed to format {fmt!r}"
