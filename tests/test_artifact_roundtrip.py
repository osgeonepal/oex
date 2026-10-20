"""Real exports checked end to end: what the writer names, the publisher must read back
(`metadata --prune` deletes HDX resources by parsing filenames), and the memory it writes with.
"""

import json
import time
from pathlib import Path

import duckdb
import pytest

import oex.exporter
from oex.config.loader import load_config
from oex.exporter import Exporter
from oex.naming import split_artifact_filename
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
        slug, source, fmt = split_artifact_filename(name[len(prefix) :], {category})
        assert slug == category, f"{name} parsed to slug {slug!r}"
        assert source == ("file" if include_source else ""), f"{name} parsed to source {source!r}"
        assert fmt in {"gpkg", "geojson"}, f"{name} parsed to format {fmt!r}"


def test_metadata_without_reports_writes_the_catalog_and_no_report_page(
    tmp_path: Path, shapefile: Path
) -> None:
    """`output.metadata` describes the layer; it must not turn report pages back on."""
    out = tmp_path / "out"
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        CONFIG.format(
            boundary=BOUNDARY,
            formats="gpkg",
            out=out,
            zip_line="",
            category="bridges",
            path=shapefile,
            include_source="true",
        ).replace("output:\n", "output:\n  metadata: true\n"),
        encoding="utf-8",
    )
    Exporter(load_config(cfg_path), runner=FileRunner()).run()
    written = sorted(p.name for p in (out / "npl" / "file").iterdir() if p.is_file())
    assert not [name for name in written if name.endswith("_report.html")], written
    assert "test_npl_bridges_file_item.json" in written
    collection = out / "npl" / "file" / "test_npl_bridges_file_metadata.json"
    assert json.loads(collection.read_text(encoding="utf-8"))["title"] == "Bridges of Nepal"


def _memory_limit(conn: duckdb.DuckDBPyConnection) -> str:
    row = conn.execute("SELECT current_setting('memory_limit')").fetchone()
    assert row is not None
    return row[0]


def test_geoparquet_is_written_within_the_run_memory_budget(tmp_path, shapefile, monkeypatch):
    """GeoParquet's Hilbert sort holds the whole layer, so a cap below the run's fails large ones."""

    limits = []
    write = oex.exporter.write_geoparquet

    def recording_write(conn, table_name, out_path):
        limits.append(_memory_limit(conn))
        return write(conn, table_name, out_path)

    monkeypatch.setattr(oex.exporter, "write_geoparquet", recording_write)
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        CONFIG.format(
            boundary=BOUNDARY,
            formats="geoparquet",
            out=tmp_path / "out",
            zip_line="",
            category="bridges",
            path=shapefile,
            include_source="true",
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    cfg.parallel.memory_gb = 4
    Exporter(cfg, runner=FileRunner()).run()

    expected = duckdb.connect()
    expected.execute("SET memory_limit='4GB'")
    assert limits == [_memory_limit(expected)]


def test_a_built_category_that_never_uploaded_is_rebuilt_on_resume(
    tmp_path, shapefile, monkeypatch
):
    """Publishing only the cached zips would drop its tiles from the HDX dataset."""
    from unittest.mock import MagicMock

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        CONFIG.format(
            boundary=BOUNDARY,
            formats="gpkg",
            out=tmp_path / "out",
            zip_line="",
            category="bridges",
            path=shapefile,
            include_source="true",
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    cfg.output.pmtiles.enabled = True
    Exporter(cfg, runner=FileRunner()).run()

    cfg.hdx.push = True
    publisher_class = MagicMock()
    publisher_class.return_value.publish.return_value = "test_npl_bridges"
    monkeypatch.setattr(oex.exporter, "HdxPublisher", publisher_class)
    Exporter(cfg, runner=FileRunner()).run()

    publisher_class.return_value.publish.assert_called_once()
    assert publisher_class.return_value.publish.call_args.kwargs["extra_resources"]


def test_the_build_time_on_an_item_leaves_out_the_upload(
    tmp_path: Path, shapefile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow connection must not read as a slow export."""

    class Clock:
        offset = 0.0

        def time(self) -> float:
            return time.time() + self.offset

    clock = Clock()
    staged: list[Path] = []

    def slow_upload(self, path: Path, slug: str, cat_tag: str) -> str:
        staged.append(path)
        clock.offset += 1000
        return "https://example.com/bridges.parquet"

    monkeypatch.setattr(oex.exporter, "time", clock)
    monkeypatch.setattr(Exporter, "_stage_layer_parquet", slow_upload)
    out = tmp_path / "out"
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        CONFIG.format(
            boundary=BOUNDARY,
            formats="gpkg, geoparquet",
            out=out,
            zip_line="",
            category="bridges",
            path=shapefile,
            include_source="true",
        ).replace("output:\n", "output:\n  metadata: true\n"),
        encoding="utf-8",
    )
    Exporter(load_config(cfg_path), runner=FileRunner()).run()

    assert staged, "the GeoParquet upload must run for this test to mean anything"
    item = json.loads((out / "npl" / "file" / "test_npl_bridges_file_item.json").read_text())
    assert 0 <= item["properties"]["oex:export_seconds"] < 1000
