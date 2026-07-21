"""A run with hdx.push off must still produce every local artifact it configures."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oex.config.schema import (
    BoundaryConfig,
    CategoryConfig,
    CombinedHdx,
    DuckdbConfig,
    HdxConfig,
    OutputConfig,
    PmtilesConfig,
    ReportConfig,
    RootConfig,
    S3Config,
)
from oex.exporter import BuiltCategory, CategoryResult, Exporter
from oex.metadata import ColumnReport, MetadataReport
from oex.report import SourceMetadata


def _source_metadata(name: str = "overture") -> SourceMetadata:
    return SourceMetadata(
        source_name=name,
        snapshot_label="2026-06-17.0",
        dataset_source="Overture",
        generated_utc="2026-07-01T14:00:00Z",
        oex_version="0.4.2",
        license_label="ODbL 1.0",
        license_url="https://opendatacommons.org/licenses/odbl/1-0/",
        pcode_source_date=None,
        boundary="user geom",
        metadata=MetadataReport(
            feature_count=500,
            geometry_types={"POLYGON": 500},
            bbox=(85.30, 27.68, 85.36, 27.73),
            columns=[ColumnReport("name", "VARCHAR", 250, 50.0, 40, [])],
            summary="500 features.",
            temporal=None,
        ),
    )


def _combined_cfg(tmp_path: Path) -> RootConfig:
    return RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=False, combine=True, combined=CombinedHdx(name="hotosm_npl")),
        output=OutputConfig(
            dir=str(tmp_path),
            report=ReportConfig(enabled=True),
            pmtiles=PmtilesConfig(enabled=True, min_zoom=0, max_zoom=8),
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=[CategoryConfig(name="buildings"), CategoryConfig(name="roads")],
    )


def _built(cfg: RootConfig, out_root: Path, name: str) -> BuiltCategory:
    zip_path = out_root / f"hotosm_npl_{name}_overture_gpkg.zip"
    zip_path.write_bytes(b"z" * 128)
    return BuiltCategory(
        result=CategoryResult(name=name, status="ok", feature_count=500, zip_paths=[zip_path]),
        category=next(c for c in cfg.categories if c.name == name),
        zip_paths=[zip_path],
        source_metadata=_source_metadata(),
        geoparquet_path=out_root / "_layers" / f"{name}.parquet",
    )


def test_combined_run_without_push_writes_tileset_and_overview(tmp_path: Path) -> None:
    cfg = _combined_cfg(tmp_path)
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    (out_root / "_layers").mkdir(parents=True)
    built_list = [_built(cfg, out_root, "buildings"), _built(cfg, out_root, "roads")]

    def fake_build(conn, layers, out_path, **kwargs):  # noqa: ANN001, ANN202
        out_path.write_bytes(b"PMTiles")
        return out_path

    with (
        patch("oex.exporter.build_combined_pmtiles", side_effect=fake_build),
        patch("oex.exporter.connect", return_value=MagicMock()),
    ):
        exporter._finish_combined(
            built_list,
            out_root,
            None,
            peeked_label=None,
            boundary_bbox=(85.30, 27.68, 85.36, 27.73),
        )

    assert (out_root / "hotosm_npl.pmtiles").exists()
    overview = out_root / "hotosm_npl_overview.html"
    assert overview.exists()
    html = overview.read_text(encoding="utf-8")
    # The map points at the tileset sitting beside it, not at an HDX resource URL.
    assert "pmtiles://hotosm_npl.pmtiles" in html
    assert "Buildings" in html and "Roads" in html
    # Local outputs survive: nothing was uploaded, so nothing may be cleaned up.
    assert all(b.zip_paths[0].exists() for b in built_list)


def test_combined_run_without_push_skips_overview_when_report_disabled(tmp_path: Path) -> None:
    cfg = _combined_cfg(tmp_path)
    cfg.output.report.enabled = False
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    (out_root / "_layers").mkdir(parents=True)

    def fake_build(conn, layers, out_path, **kwargs):  # noqa: ANN001, ANN202
        out_path.write_bytes(b"PMTiles")
        return out_path

    with (
        patch("oex.exporter.build_combined_pmtiles", side_effect=fake_build),
        patch("oex.exporter.connect", return_value=MagicMock()),
    ):
        exporter._finish_combined(
            [_built(cfg, out_root, "buildings")],
            out_root,
            None,
            peeked_label=None,
            boundary_bbox=(85.30, 27.68, 85.36, 27.73),
        )

    assert (out_root / "hotosm_npl.pmtiles").exists()
    assert not (out_root / "hotosm_npl_overview.html").exists()


def test_local_report_maps_the_category_tileset(tmp_path: Path) -> None:
    cfg = _combined_cfg(tmp_path)
    cfg.hdx.combine = False
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    out_root.mkdir(parents=True)
    pmtiles_path = out_root / "hotosm_npl_buildings_overture.pmtiles"
    pmtiles_path.write_bytes(b"PMTiles")
    report_path = out_root / "hotosm_npl_buildings_overture_report.html"

    exporter._write_local_report(
        category=cfg.categories[0],
        source_metadata=_source_metadata(),
        report_path=report_path,
        pmtiles_path=pmtiles_path,
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
        cat_tag="[buildings/overture]",
    )

    html = report_path.read_text(encoding="utf-8")
    assert "pmtiles://hotosm_npl_buildings_overture.pmtiles" in html
    assert "Map preview" in html
    # The legend names the layer, not just the source it came from.
    assert "Buildings" in html


def test_local_report_without_tiles_has_no_map(tmp_path: Path) -> None:
    cfg = _combined_cfg(tmp_path)
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    out_root.mkdir(parents=True)
    report_path = out_root / "report.html"

    exporter._write_local_report(
        category=cfg.categories[0],
        source_metadata=_source_metadata(),
        report_path=report_path,
        pmtiles_path=None,
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
        cat_tag="[buildings/overture]",
    )

    html = report_path.read_text(encoding="utf-8")
    assert "Map preview" not in html
    assert "pmtiles" not in html


def test_s3_preflight_runs_without_hdx_push(tmp_path: Path) -> None:
    """Layer staging uploads on output.s3.enabled, so the bucket check cannot sit under push."""
    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        boundary=BoundaryConfig(
            geom='{"type":"Polygon","coordinates":'
            "[[[85.3,27.6],[85.4,27.6],[85.4,27.7],[85.3,27.7],[85.3,27.6]]]}"
        ),
        hdx=HdxConfig(push=False),
        output=OutputConfig(
            dir=str(tmp_path),
            s3=S3Config(enabled=True, bucket="unreachable-bucket"),
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=[CategoryConfig(name="buildings")],
    )
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)

    with (
        patch("oex.s3.preflight", side_effect=RuntimeError("S3 preflight: cannot reach bucket")),
        pytest.raises(RuntimeError, match="S3 preflight"),
    ):
        exporter.run()
