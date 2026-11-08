"""Exporter helpers."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from oex.config.schema import (
    BoundaryConfig,
    CategoryConfig,
    CategoryOverture,
    CombinedHdx,
    DuckdbConfig,
    HdxConfig,
    OutputConfig,
    PmtilesConfig,
    RootConfig,
    S3Config,
)
from oex.exporter import (
    COMBINED_STATE_KEY,
    BuiltCategory,
    CategoryResult,
    Exporter,
    _remove_uploaded_outputs,
)
from oex.naming import resource_prefix
from oex.sources.base import SourceQuery
from oex.state import StateStore
from oex.writers import TileLayer, write_geoparquet

MakeExporter = Callable[..., Exporter]


def test_remove_uploaded_outputs_deletes_and_tolerates_missing(tmp_path: Path) -> None:
    zip1 = tmp_path / "buildings_osm_gpkg.zip"
    zip1.write_bytes(b"x")
    meta = tmp_path / "buildings_osm_metadata.json"
    meta.write_text("{}", encoding="utf-8")
    already_gone = tmp_path / "buildings_osm_shp.zip"

    _remove_uploaded_outputs([zip1, already_gone], meta)

    assert not zip1.exists()
    assert not meta.exists()


def test_remove_uploaded_outputs_deletes_zips_when_there_is_no_metadata_file(
    tmp_path: Path,
) -> None:
    zip1 = tmp_path / "roads_osm_gpkg.zip"
    zip1.write_bytes(b"x")

    _remove_uploaded_outputs([zip1], None)

    assert not zip1.exists()


@pytest.fixture
def point_table(spatial_conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    spatial_conn.execute("CREATE TABLE t AS SELECT 'x' AS name, ST_Point(85.3, 27.7) AS geom")
    return spatial_conn


def _combined_cfg(
    tmp_path: Path, categories: list[CategoryConfig], s3: S3Config | None = None
) -> RootConfig:
    return RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, combine=True),
        output=OutputConfig(
            dir=str(tmp_path),
            pmtiles=PmtilesConfig(enabled=True, min_zoom=0, max_zoom=8),
            s3=s3 or S3Config(),
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=categories,
    )


def test_publish_combined_builds_tileset_orders_and_cleans_up(
    tmp_path: Path,
    point_table: duckdb.DuckDBPyConnection,
    make_exporter: MakeExporter,
) -> None:
    cfg = _combined_cfg(tmp_path, [CategoryConfig(name="buildings"), CategoryConfig(name="roads")])
    cfg.output.remove_after_upload = True
    exporter = make_exporter(cfg, "osm")
    out_root = tmp_path / "npl" / "osm"
    (out_root / "_layers").mkdir(parents=True)
    exporter._state = StateStore(path=out_root / ".state.json", iso3="NPL", source="osm")

    query = SourceQuery(
        source_expr="t",
        select_fields=[],
        where_conditions=[],
        bbox_mode="geom",
        dataset_source="OpenStreetMap",
        source_url="",
        source_description="",
        snapshot_date=datetime.now(UTC),
        snapshot_label="2026-04-15.0",
        extra_readme_lines=[],
    )

    def _built(name: str) -> BuiltCategory:
        cat = next(c for c in cfg.categories if c.name == name)
        zip_path = out_root / f"hotosm_npl_{name}_osm_gpkg.zip"
        zip_path.write_bytes(b"z" * 128)
        parquet = write_geoparquet(point_table, "t", out_root / "_layers" / f"{name}.parquet")
        return BuiltCategory(
            result=CategoryResult(name=name, status="ok", feature_count=1, zip_paths=[zip_path]),
            category=cat,
            zip_paths=[zip_path],
            metadata_json_path=None,
            metadata_report=None,
            geoparquet_path=parquet,
            query=query,
        )

    # Fed reversed to prove the combine phase re-sorts to config order.
    built_list = [_built("roads"), _built("buildings")]

    publisher = MagicMock()
    publisher.publish_combined.return_value = "hotosm_npl"

    exporter._finish_combined(
        built_list,
        out_root,
        publisher,
        peeked_label="2026-04-15.0",
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
    )

    publisher.publish_combined.assert_called_once()
    call = publisher.publish_combined.call_args
    entries = call.args[1]
    assert [e.category.name for e in entries] == ["buildings", "roads"]
    pmtiles_path = call.kwargs["pmtiles_path"]
    # GeoParquets are deliverables, so they stay when the uploaded tileset and zips go.
    assert pmtiles_path.name == "hotosm_npl.pmtiles"
    assert not pmtiles_path.exists()
    assert all(not b.zip_paths[0].exists() for b in built_list)
    assert all(b.geoparquet_path is not None and b.geoparquet_path.exists() for b in built_list)
    assert exporter._state.is_uploaded(COMBINED_STATE_KEY, snapshot_label="2026-04-15.0")
    assert all(b.result.hdx_dataset == "hotosm_npl" for b in built_list)


@pytest.mark.parametrize(
    ("category", "staged", "expected"),
    [
        (
            CategoryConfig(name="buildings"),
            [
                ("overture", "buildings", "https://s3/p/NPL/_layers/overture/buildings.parquet"),
                ("overture", "rivers", "https://s3/p/NPL/_layers/overture/rivers.parquet"),
            ],
            {("osm", "buildings"), ("overture", "buildings")},
        ),
        (
            CategoryConfig(name="buildings", overture=CategoryOverture(tiles=False)),
            [("overture", "buildings", "https://s3/p/NPL/_layers/overture/buildings.parquet")],
            {("osm", "buildings")},
        ),
    ],
    ids=["staged layer of a dropped category", "source with tiles false"],
)
def test_the_combined_tileset_merges_only_configured_tiled_layers(
    tmp_path: Path,
    point_table: duckdb.DuckDBPyConnection,
    make_exporter: MakeExporter,
    fake_pmtiles_build: list[TileLayer],
    category: CategoryConfig,
    staged: list[tuple[str, str, str]],
    expected: set[tuple[str, str]],
) -> None:
    """Staging outlives dropped categories, and merging them would add features no legend
    entry draws; `tiles: false` keeps a source a download but off the map."""
    cfg = _combined_cfg(tmp_path, [category], s3=S3Config(enabled=True, bucket="b", prefix="p"))
    exporter = make_exporter(cfg, "osm")
    out_root = tmp_path / "npl" / "osm"
    (out_root / "_layers").mkdir(parents=True)
    parquet = write_geoparquet(point_table, "t", out_root / "_layers" / "buildings.parquet")
    built_ok = [
        BuiltCategory(
            result=CategoryResult(name="buildings", status="ok"),
            category=cfg.categories[0],
            geoparquet_path=parquet,
        )
    ]

    with patch("oex.exporter.list_layer_urls", return_value=staged):
        exporter._build_combined_tiles(built_ok, out_root, "hotosm_npl")

    assert {(layer.source, layer.category) for layer in fake_pmtiles_build} == expected


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        (
            RootConfig(
                iso3="COD",
                key="hot_cod",
                hdx=HdxConfig(combine=True, combined=CombinedHdx(name="hot_cod_humanitarian_data")),
            ),
            "hot_cod_humanitarian_data",
        ),
        (RootConfig(iso3="COD", key="hot_cod", hdx=HdxConfig(combine=True)), "hot_cod_cod"),
        (RootConfig(iso3="COD", key="hot_cod"), "hot_cod_cod"),
        (
            RootConfig(
                iso3="MCO", key="hotosm_project", output=OutputConfig(s3=S3Config(folder="11731"))
            ),
            "hotosm_project_11731",
        ),
    ],
    ids=[
        "combined with a dataset name",
        "combined without a name",
        "per category",
        "per category with an s3 folder",
    ],
)
def test_resource_prefix_prefers_the_combined_dataset_name_then_key_and_area(
    cfg: RootConfig, expected: str
) -> None:
    assert resource_prefix(cfg) == expected


def _misconfigured_run(tmp_path: Path, configure, make_exporter: MakeExporter) -> None:  # noqa: ANN001
    """Runs the exporter with the boundary fetch stubbed to fail, so a config error raised
    later than run start shows up as the wrong exception."""
    cfg = RootConfig(iso3="NPL", categories=[CategoryConfig(name="buildings")])
    cfg.output.dir = str(tmp_path / "output")
    cfg.duckdb.temp_dir = str(tmp_path / "duckdb")
    configure(cfg)
    with patch("oex.exporter.resolve_boundary", side_effect=AssertionError("passed run start")):
        make_exporter(cfg, "osm").run()


def test_nest_by_geometry_without_the_split_fails_the_run_at_start(
    tmp_path: Path, make_exporter: MakeExporter
) -> None:
    """One artifact holding every geometry type cannot sit under a single segment."""

    def configure(cfg: RootConfig) -> None:
        cfg.output.s3.enabled = True
        cfg.output.s3.bucket = "exports"
        cfg.output.s3.nest_by_geometry = True

    with pytest.raises(ValueError, match="split_by_geometry"):
        _misconfigured_run(tmp_path, configure, make_exporter)


def test_geoparquet_pushed_to_hdx_without_s3_fails_the_run_at_start(
    tmp_path: Path, make_exporter: MakeExporter
) -> None:
    """The HDX resource links the staged object, so a category would otherwise fail late."""

    def configure(cfg: RootConfig) -> None:
        cfg.hdx.push = True
        cfg.output.metadata = False
        cfg.output.report.enabled = False
        cfg.categories[0].formats = ["gpkg", "geoparquet"]

    with pytest.raises(ValueError, match="nothing to publish"):
        _misconfigured_run(tmp_path, configure, make_exporter)


def test_the_geometry_label_is_read_back_off_the_artifact_name(
    make_exporter: MakeExporter,
) -> None:
    cfg = RootConfig(iso3="NPL")
    cfg.output.split_by_geometry = True
    exporter = make_exporter(cfg, "osm")
    name = Path("hotosm_project_1_buildings_polygons_shp.zip")
    assert exporter._artifact_geometry(name, "buildings") == "polygons"
    unzipped = Path("hotosm_project_1_buildings_lines.geojson")
    assert exporter._artifact_geometry(unzipped, "buildings") == "lines"
    with pytest.raises(ValueError, match="no geometry label"):
        exporter._artifact_geometry(Path("hotosm_project_1_buildings_shp.zip"), "buildings")


@pytest.mark.parametrize(("simplify_meters", "embedded"), [(0.0, False), (500.0, True)])
def test_only_a_simplified_outline_becomes_the_item_footprint(
    make_exporter: MakeExporter, simplify_meters: float, embedded: bool
) -> None:
    """A raw national outline can be over 100 MB, and every Item would carry a copy."""
    cfg = RootConfig(
        iso3="NPL",
        boundary=BoundaryConfig(buffer_meters=1000.0, simplify_meters=simplify_meters),
    )
    outline = '{"type":"Polygon","coordinates":[[[85,27],[86,27],[86,28],[85,27]]]}'
    footprint = make_exporter(cfg, "osm")._item_footprint(outline)
    assert (footprint == outline) is embedded


def _s3_only_exporter(
    tmp_path: Path, make_exporter: MakeExporter, s3: S3Config, *, remove_after_upload: bool
) -> Exporter:
    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        output=OutputConfig(dir=str(tmp_path), s3=s3, remove_after_upload=remove_after_upload),
    )
    exporter = make_exporter(cfg, "osm")
    exporter._state = StateStore(path=tmp_path / ".state.json", iso3="NPL", source="osm")
    exporter._state.mark_built(
        "buildings", snapshot_label="2026-04-15", zip_paths=[], metadata_json_path=None
    )
    return exporter


@pytest.mark.parametrize(
    ("s3", "filename", "expected_key"),
    [
        (
            S3Config(enabled=True, bucket="b", prefix="ISO3"),
            "hotosm_npl_buildings_osm_gpkg.zip",
            "ISO3/NPL/buildings/hotosm_npl_buildings_osm_gpkg.zip",
        ),
        (
            S3Config(
                enabled=True, bucket="b", prefix="ISO3", folder="4242", nest_by_category=False
            ),
            "hotosm_npl_buildings_osm_gpkg.zip",
            "ISO3/4242/hotosm_npl_buildings_osm_gpkg.zip",
        ),
        (
            S3Config(enabled=True, bucket="b", prefix="ISO3", nest_by_geometry=True),
            "hotosm_npl_buildings_polygons_shp.zip",
            "ISO3/NPL/buildings/polygons/hotosm_npl_buildings_polygons_shp.zip",
        ),
    ],
    ids=["nested by category", "s3 folder without category", "nested by geometry"],
)
def test_an_s3_only_upload_puts_each_artifact_under_its_key_and_marks_it_uploaded(
    tmp_path: Path, make_exporter: MakeExporter, s3: S3Config, filename: str, expected_key: str
) -> None:
    exporter = _s3_only_exporter(tmp_path, make_exporter, s3, remove_after_upload=False)
    artifact = tmp_path / filename
    artifact.write_bytes(b"z")

    with patch("oex.exporter.s3_upload") as upload:
        exporter._upload_category_to_s3([artifact], "buildings", "[buildings]")

    assert [call.kwargs["key"] for call in upload.call_args_list] == [expected_key]
    assert upload.call_args.kwargs["bucket"] == "b"
    assert exporter._state is not None
    assert exporter._state.is_uploaded("buildings", snapshot_label="2026-04-15")


@pytest.mark.parametrize("remove_after_upload", [True, False])
def test_an_s3_only_upload_deletes_local_outputs_only_when_remove_after_upload_is_set(
    tmp_path: Path, make_exporter: MakeExporter, remove_after_upload: bool
) -> None:
    exporter = _s3_only_exporter(
        tmp_path,
        make_exporter,
        S3Config(enabled=True, bucket="b", prefix="ISO3"),
        remove_after_upload=remove_after_upload,
    )
    artifacts = [tmp_path / "hotosm_npl_buildings_osm_gpkg.zip", tmp_path / "x_shp.zip"]
    for artifact in artifacts:
        artifact.write_bytes(b"z")

    with patch("oex.exporter.s3_upload"):
        exporter._upload_category_to_s3(artifacts, "buildings", "[buildings]")

    assert [artifact.exists() for artifact in artifacts] == [not remove_after_upload] * 2
