"""A run with hdx.push off must still produce every local artifact it configures."""

import json
import re
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
from oex.naming import layer_dataset_name
from oex.report import SourceMetadata
from oex.stac import LayerAssets


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


def _artifacts(out_root: Path, name: str) -> list[Path]:
    """Several formats, listed so that insertion order is not the intended order."""
    paths = []
    for suffix in ("geojson", "shp", "gpkg"):
        path = out_root / f"hotosm_npl_{name}_overture_{suffix}.zip"
        path.write_bytes(b"z" * 8)
        paths.append(path)
    return paths


def _layer_assets(cfg: RootConfig, out_root: Path, name: str) -> LayerAssets:
    built = _built(cfg, out_root, name)
    assert built.source_metadata is not None
    return LayerAssets(
        category=built.category,
        source_metadata=built.source_metadata,
        dataset_name=f"hotosm_npl_{name}",
        artifacts=built.zip_paths,
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
    # One shared tileset, so each layer draws only its own features from it.
    assert "'buildings|overture'" in html and "'roads|overture'" in html
    # Local outputs survive: nothing was uploaded, so nothing may be cleaned up.
    assert all(b.zip_paths[0].exists() for b in built_list)


def test_combined_metadata_is_one_file_per_source_covering_its_layers(tmp_path: Path) -> None:
    import json

    cfg = _combined_cfg(tmp_path)
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    (out_root / "_layers").mkdir(parents=True)
    built = [_built(cfg, out_root, "buildings"), _built(cfg, out_root, "roads")]

    path = exporter._write_combined_metadata(built, out_root, "hotosm_npl")

    # The source token keeps a combined dataset's sources from overwriting each other.
    assert path is not None and path.name == "hotosm_npl_overture_metadata.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["type"] == "Collection"
    assert doc["id"] == "hotosm_npl"
    # Titled with the place, since an Item turns up in searches away from its Collection.
    assert [link["title"] for link in doc["links"] if link["rel"] == "item"] == [
        "Buildings of Nepal",
        "Roads of Nepal",
    ]
    assert [link["href"] for link in doc["links"] if link["rel"] == "item"] == [
        "hotosm_npl_buildings_overture_item.json",
        "hotosm_npl_roads_overture_item.json",
    ]


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
        zip_paths=_artifacts(out_root, "buildings"),
        pmtiles_path=pmtiles_path,
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
        cat_tag="[buildings/overture]",
    )

    html = report_path.read_text(encoding="utf-8")
    assert "pmtiles://hotosm_npl_buildings_overture.pmtiles" in html
    assert "'source-layer': 'buildings'" in html
    # The layer's own tileset has no `category` property to filter on.
    assert "['get', 'category']" not in html
    assert 'id="map"' in html
    # One ordering rule serves the local page and the published one.
    formats = re.findall(r'<option value="[^"]*">([^<]+)</option>', html)
    assert formats[0].startswith("GeoPackage ("), formats
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
        zip_paths=[_built(cfg, out_root, "buildings").zip_paths[0]],
        pmtiles_path=None,
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
        cat_tag="[buildings/overture]",
    )

    html = report_path.read_text(encoding="utf-8")
    assert 'id="map"' not in html
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


def test_the_combined_collection_names_the_merged_tileset(tmp_path: Path) -> None:
    """The dataset's one viewable asset is the merged tileset, so it belongs on the Collection."""
    import json

    cfg = _combined_cfg(tmp_path)
    cfg.output.s3.enabled = True
    cfg.output.s3.bucket = "example-bucket"
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    (out_root / "_layers").mkdir(parents=True)
    built = [_built(cfg, out_root, "buildings")]
    tiles = out_root / "hotosm_npl.pmtiles"
    tiles.write_bytes(b"\x00")

    with patch("oex.exporter.s3_upload"):
        path = exporter._write_combined_metadata(built, out_root, "hotosm_npl", tiles)

    assert path is not None
    assets = json.loads(path.read_text(encoding="utf-8"))["assets"]
    assert assets["pmtiles"]["href"].endswith("/combined/hotosm_npl.pmtiles")


def test_an_artifact_url_matches_the_key_the_publisher_uploads_to(tmp_path: Path) -> None:
    """A derived href that disagrees with the upload key is a 404 on every asset."""
    from oex.s3 import artifact_key

    cfg = _combined_cfg(tmp_path)
    cfg.output.s3.enabled = True
    cfg.output.s3.bucket = "example-bucket"
    exporter = Exporter(cfg, MagicMock())
    url = exporter._artifact_url(tmp_path / "hotosm_npl.pmtiles", "combined")
    key = artifact_key(
        cfg.output.s3.prefix,
        cfg.iso3,
        "combined",
        "hotosm_npl.pmtiles",
        folder=cfg.output.s3.folder,
        nest_by_category=cfg.output.s3.nest_by_category,
    )
    assert url is not None and url.endswith(key)


def test_pushing_to_hdx_without_s3_is_refused(tmp_path: Path) -> None:
    """Items are published to the bucket and linked by URL, so without one the Collection
    would name a layer file that was uploaded nowhere."""
    cfg = _combined_cfg(tmp_path)
    cfg.hdx.push = True
    cfg.output.s3 = S3Config(enabled=False)
    runner = MagicMock()
    runner.name = "osm"

    with pytest.raises(ValueError, match="output.s3.enabled is false"):
        Exporter(cfg, runner).run()


def test_an_items_file_is_staged_at_the_key_the_collection_links_to(tmp_path: Path) -> None:
    """The Collection links each Item by URL; if the upload key and that URL disagree,
    every layer of the dataset is a 404 and nothing in the run notices."""
    import json

    cfg = _combined_cfg(tmp_path)
    cfg.output.s3 = S3Config(
        enabled=True, bucket="example-bucket", prefix="ISO3", region="us-east-1"
    )
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    (out_root / "_layers").mkdir(parents=True)
    layer = _layer_assets(cfg, out_root, "buildings")

    with patch("oex.exporter.s3_upload") as upload:
        exporter._write_layer_metadata(layer, out_root, None)

    staged_key = upload.call_args.kwargs["key"]
    with patch("oex.exporter.s3_upload"):
        path = exporter._write_combined_metadata(
            [_built(cfg, out_root, "buildings")], out_root, "hotosm_npl"
        )
    assert path is not None
    linked = [
        link["href"]
        for link in json.loads(path.read_text(encoding="utf-8"))["links"]
        if link["rel"] == "item"
    ]
    assert linked == [f"https://example-bucket.s3.amazonaws.com/{staged_key}"]


def test_a_local_run_writes_the_collection_that_ties_its_layers_together(tmp_path: Path) -> None:
    """Without it the output directory is a pile of Items with no entry point, and the
    catalog only exists once someone pushes to HDX."""
    import json

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

    collection = out_root / "hotosm_npl_overture_metadata.json"
    assert collection.exists()
    payload = json.loads(collection.read_text(encoding="utf-8"))
    assert payload["type"] == "Collection"
    assert [link["href"] for link in payload["links"] if link["rel"] == "item"] == [
        "hotosm_npl_buildings_overture_item.json",
        "hotosm_npl_roads_overture_item.json",
    ]


def test_a_run_without_hdx_stages_the_collection_the_items_point_at(tmp_path: Path) -> None:
    """S3 on and push off publishes Items whose root and collection links name the
    Collection, so a Collection that never reaches the bucket makes all of them 404."""
    cfg = _combined_cfg(tmp_path)
    cfg.hdx.push = False
    cfg.output.s3 = S3Config(
        enabled=True, bucket="example-bucket", prefix="ISO3", region="us-east-1"
    )
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    (out_root / "_layers").mkdir(parents=True)
    built = [_built(cfg, out_root, "buildings")]

    with patch("oex.exporter.s3_upload") as upload:
        exporter._write_layer_metadata(_layer_assets(cfg, out_root, "buildings"), out_root, None)
        exporter._write_combined_metadata(built, out_root, "hotosm_npl")

    staged = [call.kwargs["key"] for call in upload.call_args_list]
    assert staged == [
        "ISO3/NPL/buildings/hotosm_npl_buildings_overture_item.json",
        "ISO3/NPL/combined/hotosm_npl_overture_metadata.json",
    ]


def test_a_combined_runs_category_page_links_the_combined_hdx_dataset(tmp_path: Path) -> None:
    """Combining publishes to one dataset, so a per-category link would name one never made."""
    cfg = _combined_cfg(tmp_path)
    cfg.hdx.push = True
    cfg.hdx.site = "prod"
    runner = MagicMock()
    runner.name = "overture"
    out_root = tmp_path / "npl" / "overture"
    out_root.mkdir(parents=True)
    report_path = out_root / "report.html"
    Exporter(cfg, runner)._write_local_report(
        category=cfg.categories[0],
        source_metadata=_source_metadata(),
        report_path=report_path,
        zip_paths=[_built(cfg, out_root, "buildings").zip_paths[0]],
        pmtiles_path=None,
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
        cat_tag="[buildings/overture]",
    )
    html = report_path.read_text(encoding="utf-8")
    assert "https://data.humdata.org/dataset/hotosm_npl<" in html
    assert "dataset/hotosm_npl_buildings" not in html


def test_an_s3_folder_does_not_change_the_hdx_dataset_an_item_links(tmp_path: Path) -> None:
    """The folder renames the files, not the dataset the publisher writes to."""
    from oex.stac import LayerAssets

    cfg = _combined_cfg(tmp_path)
    cfg.hdx.combine = False
    cfg.hdx.push = True
    cfg.hdx.site = "prod"
    cfg.output.s3.folder = "kathmandu"
    runner = MagicMock()
    runner.name = "overture"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "overture"
    out_root.mkdir(parents=True)
    layer = LayerAssets(
        category=cfg.categories[0],
        source_metadata=_source_metadata(),
        dataset_name=layer_dataset_name(cfg, cfg.categories[0]),
    )
    item_path = exporter._write_layer_metadata(layer, out_root, None)
    links = json.loads(item_path.read_text(encoding="utf-8"))["links"]
    assert [link["href"] for link in links if link["rel"] == "via"] == [
        "https://data.humdata.org/dataset/hotosm_npl_buildings"
    ]


def test_each_category_collection_links_the_other_categories_of_its_area(tmp_path: Path) -> None:
    """A STAC client moves between an area's datasets the way a reader does on the page."""
    from oex.stac import LayerAssets

    cfg = _combined_cfg(tmp_path)
    cfg.hdx.combine = False
    cfg.output.report.related_datasets = True
    cfg.hdx.push = True
    cfg.output.s3.enabled = True
    cfg.output.s3.bucket = "bucket"
    cfg.output.s3.prefix = "ISO3"
    cfg.output.s3.region = "us-east-1"
    runner = MagicMock()
    runner.name = "osm"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "osm"
    out_root.mkdir(parents=True)
    layer = LayerAssets(
        category=cfg.categories[0],
        source_metadata=_source_metadata("osm"),
        dataset_name=layer_dataset_name(cfg, cfg.categories[0]),
    )
    collection_path = out_root / "collection.json"
    with patch.object(Exporter, "_stage_metadata"):
        exporter._write_layer_metadata(layer, out_root, collection_path)
    links = json.loads(collection_path.read_text(encoding="utf-8"))["links"]
    assert [(link["title"], link["href"]) for link in links if link["rel"] == "related"] == [
        (
            "Roads",
            "https://bucket.s3.amazonaws.com/ISO3/NPL/roads/hotosm_npl_roads_osm_metadata.json",
        )
    ]
    cfg.output.report.related_datasets = False
    with patch.object(Exporter, "_stage_metadata"):
        exporter._write_layer_metadata(layer, out_root, collection_path)
    links = json.loads(collection_path.read_text(encoding="utf-8"))["links"]
    assert not [link for link in links if link["rel"] == "related"]


def _page_link(cfg: RootConfig, tmp_path: Path) -> list[str]:
    from oex.stac import LayerAssets

    cfg.hdx.push = True
    cfg.output.s3.enabled = True
    cfg.output.s3.bucket = "bucket"
    cfg.output.s3.prefix = "ISO3"
    cfg.output.s3.region = "us-east-1"
    runner = MagicMock()
    runner.name = "osm"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "osm"
    out_root.mkdir(parents=True, exist_ok=True)
    layer = LayerAssets(
        category=cfg.categories[0],
        source_metadata=_source_metadata("osm"),
        dataset_name=layer_dataset_name(cfg, cfg.categories[0]),
    )
    collection_path = out_root / "collection.json"
    with patch.object(Exporter, "_stage_metadata"):
        item_path = exporter._write_layer_metadata(layer, out_root, collection_path)
    pages = []
    for path in (item_path, collection_path):
        links = json.loads(path.read_text(encoding="utf-8"))["links"]
        pages += [
            link["href"]
            for link in links
            if link["rel"] == "alternate"
            and link["type"] == "text/html"
            and link["title"] == "Dataset page"
        ]
    return pages


def test_stac_links_the_page_the_publisher_uploads(tmp_path: Path) -> None:
    """STAC clients offer an `alternate` text/html link as the human view of the data."""
    cfg = _combined_cfg(tmp_path)
    cfg.hdx.combine = False
    page = "https://bucket.s3.amazonaws.com/ISO3/NPL/buildings/hotosm_npl_buildings_report.html"
    # The Item and its Collection both name it.
    assert _page_link(cfg, tmp_path) == [page, page]
    combined = _combined_cfg(tmp_path)
    overview = "https://bucket.s3.amazonaws.com/ISO3/NPL/overview/hotosm_npl_overview.html"
    assert _page_link(combined, tmp_path) == [overview, overview]
    no_page = _combined_cfg(tmp_path)
    no_page.output.report.enabled = False
    assert _page_link(no_page, tmp_path) == []
