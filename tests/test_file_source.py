"""The file source reads a user-supplied spatial file and normalises it to lon/lat."""

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from oex.config.schema import CategoryConfig, CategoryFile, FileSourceConfig, RootConfig
from oex.sources.base import CategorySkippedError
from oex.sources.file import (
    FileRunner,
    FileSourceError,
    category_path,
    declared_crs,
    geometry_expr,
    resolve_crs,
    resolve_layer,
)


@pytest.fixture
def utm_shapefile(tmp_path: Path, conn) -> Path:
    """Two points near Kathmandu, stored in UTM 45N rather than lon/lat.

    Written with DuckDB rather than geopandas, which oex does not depend on, so the
    file source cannot end up silently untested.
    """
    path = tmp_path / "bridges.shp"
    conn.execute("""
        CREATE OR REPLACE TABLE fixture AS
        SELECT * FROM (VALUES
            ('a', 'Damaged',    ST_Point(340000, 3060000)),
            ('b', 'Washed Out', ST_Point(341000, 3061000))
        ) AS t("Bridge_Nm", "Condition", geom)
    """)
    conn.execute(
        f"COPY fixture TO '{path}' WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile', SRS 'EPSG:32645')"
    )
    return path


@pytest.fixture
def conn():
    c = duckdb.connect()
    c.execute("INSTALL spatial; LOAD spatial;")
    return c


def test_a_projected_file_declares_its_crs(conn, utm_shapefile):
    assert declared_crs(conn, str(utm_shapefile)) == "EPSG:32645"


def test_a_file_without_a_prj_declares_nothing(conn, utm_shapefile, tmp_path):
    bare = tmp_path / "bare.shp"
    for suffix in (".shp", ".dbf", ".shx"):
        bare.with_suffix(suffix).write_bytes(utm_shapefile.with_suffix(suffix).read_bytes())
    assert declared_crs(conn, str(bare)) is None


def test_a_file_with_no_crs_and_no_config_is_refused():
    """Assuming lon/lat would place the data in the wrong part of the world."""
    with pytest.raises(FileSourceError, match="declares no CRS"):
        resolve_crs(None, "")


def test_the_config_crs_wins_over_the_declared_one():
    assert resolve_crs("EPSG:4326", "EPSG:32645") == "EPSG:32645"


def test_lonlat_data_is_not_transformed():
    """A pointless ST_Transform would cost time on every row."""
    assert geometry_expr("OGC:CRS84") == "geom"
    assert geometry_expr("EPSG:4326") == "geom"


def test_projected_data_is_transformed_to_crs84():
    expr = geometry_expr("EPSG:32645")
    assert "ST_Transform" in expr and "OGC:CRS84" in expr


@pytest.mark.parametrize(
    ("crs", "wkt", "expected"),
    [
        # EPSG:4258 is latitude-first by authority, so without always_xy it comes back swapped
        ("EPSG:4258", "POINT(10 50)", (10.0, 50.0)),
        # EPSG:3035 is northing-first; without always_xy this lands off the coast of Norway
        ("EPSG:3035", "POINT(4321000 3210000)", (10.0, 52.0)),
        # easting-first, which is why a UTM-only test cannot catch an axis-order bug
        ("EPSG:32645", "POINT(340000 3060000)", (85.378, 27.655)),
    ],
)
def test_an_axis_order_crs_is_not_silently_swapped(conn, crs, wkt, expected):
    """ST_Read hands back x/y, so ST_Transform must be told not to honour axis order."""
    expr = geometry_expr(crs).replace("geom", f"ST_GeomFromText('{wkt}')")
    lon, lat = conn.execute(f"SELECT ST_X({expr}), ST_Y({expr})").fetchone()
    assert (lon, lat) == pytest.approx(expected, abs=0.01)


def test_a_projected_point_lands_in_the_right_place(conn, utm_shapefile):
    """Without the transform oex's own earth filter drops every row silently."""
    expr = geometry_expr("EPSG:32645")
    lon, lat = conn.execute(
        f"SELECT ST_X({expr}), ST_Y({expr}) FROM ST_Read('{utm_shapefile}') LIMIT 1"
    ).fetchone()
    assert lon == pytest.approx(85.378, abs=0.01)
    assert lat == pytest.approx(27.655, abs=0.01)


def test_untransformed_projected_data_would_be_dropped(conn, utm_shapefile):
    """This is the failure the transform exists to prevent."""
    surviving = conn.execute(
        f"""SELECT count(*) FROM ST_Read('{utm_shapefile}')
            WHERE ST_YMin(geom) >= -90 AND ST_YMax(geom) <= 90
              AND ST_XMin(geom) >= -180 AND ST_XMax(geom) <= 180"""
    ).fetchone()[0]
    assert surviving == 0


def _config(path: Path, **file_kwargs) -> RootConfig:
    cfg = RootConfig(iso3="NPL", key="t")
    cfg.source["file"] = FileSourceConfig(enabled=True)
    cfg.categories = [
        CategoryConfig(
            name="bridges",
            file=CategoryFile(
                enabled=True,
                path=str(path),
                select={"name": "Bridge_Nm", "status": "Condition"},
                **file_kwargs,
            ),
        )
    ]
    return cfg


def test_the_query_selects_only_the_mapped_columns(utm_shapefile):
    cfg = _config(utm_shapefile)
    runner = FileRunner()
    runner.prepare(cfg)
    query = runner.query_for(cfg, cfg.categories[0])
    assert query.select_fields == ['"Bridge_Nm" AS "name"', '"Condition" AS "status"']


def test_the_source_expression_exposes_exactly_one_geometry_column(utm_shapefile, conn):
    """The pipeline appends `geometry AS geom`, so a second geometry breaks every writer."""
    cfg = _config(utm_shapefile)
    runner = FileRunner()
    runner.prepare(cfg)
    query = runner.query_for(cfg, cfg.categories[0])
    columns = conn.execute(f"SELECT * FROM {query.source_expr} LIMIT 0").description
    names = [c[0] for c in columns]
    assert names.count("geometry") == 1
    assert "geom" not in names


def test_the_source_expression_returns_lonlat(utm_shapefile, conn):
    cfg = _config(utm_shapefile)
    runner = FileRunner()
    runner.prepare(cfg)
    query = runner.query_for(cfg, cfg.categories[0])
    lon, lat = conn.execute(
        f"SELECT ST_X(geometry), ST_Y(geometry) FROM {query.source_expr} LIMIT 1"
    ).fetchone()
    assert lon == pytest.approx(85.378, abs=0.01)
    assert lat == pytest.approx(27.655, abs=0.01)


def test_the_snapshot_comes_from_the_file_when_the_config_gives_none(utm_shapefile):
    cfg = _config(utm_shapefile)
    runner = FileRunner()
    runner.prepare(cfg)
    query = runner.query_for(cfg, cfg.categories[0])
    mtime = datetime.fromtimestamp(utm_shapefile.stat().st_mtime, tz=UTC)
    assert query.snapshot_date.date() == mtime.date()


def test_an_explicit_snapshot_overrides_the_file_mtime(utm_shapefile):
    cfg = _config(utm_shapefile)
    cfg.source["file"] = FileSourceConfig(enabled=True, snapshot="2026-08-27T00:00:00+00:00")
    runner = FileRunner()
    runner.prepare(cfg)
    assert runner.query_for(cfg, cfg.categories[0]).snapshot_label == "2026-08-27"


def test_a_category_without_a_select_mapping_is_refused(utm_shapefile):
    cfg = _config(utm_shapefile)
    cfg.categories[0].file.select = {}
    runner = FileRunner()
    runner.prepare(cfg)
    with pytest.raises(FileSourceError, match="file.select"):
        runner.query_for(cfg, cfg.categories[0])


def test_a_disabled_category_is_skipped_not_failed(utm_shapefile):
    cfg = _config(utm_shapefile)
    runner = FileRunner()
    runner.prepare(cfg)
    cfg.categories[0].file.enabled = False
    with pytest.raises(CategorySkippedError):
        runner.query_for(cfg, cfg.categories[0])


def test_a_missing_file_fails_loud(tmp_path):
    cfg = _config(tmp_path / "nope.shp")
    with pytest.raises((FileSourceError, duckdb.IOException)):
        FileRunner().prepare(cfg)


def test_a_category_with_no_path_anywhere_is_refused():
    category = CategoryConfig(name="bridges", file=CategoryFile(enabled=True))
    with pytest.raises(FileSourceError, match="no `file.path`"):
        category_path(category, FileSourceConfig(enabled=True))


def test_a_category_falls_back_to_the_shared_path():
    category = CategoryConfig(name="bridges", file=CategoryFile(enabled=True))
    src = FileSourceConfig(enabled=True, path="/data/all.gpkg")
    assert category_path(category, src) == "/data/all.gpkg"


@pytest.fixture
def two_layer_gpkg(tmp_path: Path) -> Path:
    """One file, two layers: ST_Read would silently return only the first.

    DuckDB's GDAL COPY names the layer after the file, so it cannot write two.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    path = tmp_path / "two.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(85.3, 27.6)], crs="EPSG:4326").to_file(
        path, layer="bridges", driver="GPKG"
    )
    gpd.GeoDataFrame(
        {"id": [2, 3]}, geometry=[Point(85.4, 27.7), Point(85.5, 27.8)], crs="EPSG:4326"
    ).to_file(path, layer="roads", driver="GPKG")
    return path


def test_a_multi_layer_file_without_a_layer_is_refused():
    """Taking the first layer would silently drop every other layer in the file."""
    with pytest.raises(FileSourceError, match="set `layer`"):
        resolve_layer(["bridges", "roads"], "")


def test_a_single_layer_file_needs_no_layer():
    assert resolve_layer(["bridges"], "") == ""


def test_a_layer_that_is_not_in_the_file_is_refused():
    with pytest.raises(FileSourceError, match="not in the file"):
        resolve_layer(["bridges", "roads"], "paths")


def test_the_named_layer_is_the_one_read(two_layer_gpkg, conn):
    cfg = _config(two_layer_gpkg, layer="roads")
    runner = FileRunner()
    runner.prepare(cfg)
    query = runner.query_for(cfg, cfg.categories[0])
    assert conn.execute(f"SELECT count(*) FROM {query.source_expr}").fetchone()[0] == 2


def test_a_multi_layer_file_fails_prepare_when_no_layer_is_named(two_layer_gpkg):
    with pytest.raises(FileSourceError, match="2 layers"):
        FileRunner().prepare(_config(two_layer_gpkg))
