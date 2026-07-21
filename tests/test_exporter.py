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


def test_publish_combined_builds_tileset_orders_and_cleans_up(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    import duckdb

    from oex.config.schema import (
        CategoryConfig,
        DuckdbConfig,
        HdxConfig,
        OutputConfig,
        PmtilesConfig,
        RootConfig,
    )
    from oex.exporter import _COMBINED_SLUG, BuiltCategory, CategoryResult, Exporter
    from oex.sources.base import SourceQuery
    from oex.state import StateStore
    from oex.writers import write_geoparquet

    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, combine=True),
        output=OutputConfig(
            dir=str(tmp_path),
            pmtiles=PmtilesConfig(enabled=True, min_zoom=0, max_zoom=8),
            remove_after_upload=True,
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=[CategoryConfig(name="buildings"), CategoryConfig(name="roads")],
    )

    runner = MagicMock()
    runner.name = "osm"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "osm"
    (out_root / "_layers").mkdir(parents=True)
    exporter._state = StateStore(path=out_root / ".state.json", iso3="NPL", source="osm")

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute("CREATE TABLE t AS SELECT 'x' AS name, ST_Point(85.3, 27.7) AS geom")

    query = SourceQuery(
        source_expr="t",
        select_fields=[],
        where_conditions=[],
        bbox_cols="geom",
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
        parquet = write_geoparquet(conn, "t", out_root / "_layers" / f"{name}.parquet")
        return BuiltCategory(
            result=CategoryResult(name=name, status="ok", feature_count=1, zip_paths=[zip_path]),
            category=cat,
            zip_paths=[zip_path],
            metadata_json_path=None,
            metadata_obj=None,
            geoparquet_path=parquet,
            query=query,
        )

    # Feed reversed to prove the combine phase re-sorts to config order.
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
    # Tileset built, then removed after upload; zips removed; GeoParquets kept as deliverables.
    assert pmtiles_path.name == "hotosm_npl.pmtiles"
    assert not pmtiles_path.exists()
    assert all(not b.zip_paths[0].exists() for b in built_list)
    assert all(b.geoparquet_path is not None and b.geoparquet_path.exists() for b in built_list)
    assert exporter._state.is_uploaded(_COMBINED_SLUG, snapshot_label="2026-04-15.0")
    assert all(b.result.hdx_dataset == "hotosm_npl" for b in built_list)


def test_combined_tileset_skips_staged_layers_not_in_this_config(tmp_path: Path) -> None:
    """The S3 staging area is never pruned, so it outlives categories you drop.

    Merging those stale layers would bloat the tileset with features no legend
    entry references, so nothing on the page would even draw them.
    """
    from unittest.mock import MagicMock, patch

    import duckdb

    from oex.config.schema import (
        CategoryConfig,
        DuckdbConfig,
        HdxConfig,
        OutputConfig,
        PmtilesConfig,
        RootConfig,
        S3Config,
    )
    from oex.exporter import BuiltCategory, CategoryResult, Exporter
    from oex.writers import write_geoparquet

    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, combine=True),
        output=OutputConfig(
            dir=str(tmp_path),
            pmtiles=PmtilesConfig(enabled=True, min_zoom=0, max_zoom=8),
            s3=S3Config(enabled=True, bucket="b", prefix="p"),
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=[CategoryConfig(name="buildings")],
    )

    runner = MagicMock()
    runner.name = "osm"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "osm"
    (out_root / "_layers").mkdir(parents=True)

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute("CREATE TABLE t AS SELECT 'x' AS name, ST_Point(85.3, 27.7) AS geom")
    parquet = write_geoparquet(conn, "t", out_root / "_layers" / "buildings.parquet")

    built_ok = [
        BuiltCategory(
            result=CategoryResult(name="buildings", status="ok"),
            category=cfg.categories[0],
            geoparquet_path=parquet,
        )
    ]

    staged = [
        ("overture", "buildings", "https://s3/p/NPL/_layers/overture/buildings.parquet"),
        # Left behind by an earlier config that still had a Rivers category.
        ("overture", "rivers", "https://s3/p/NPL/_layers/overture/rivers.parquet"),
    ]
    captured: dict = {}

    def fake_build(conn, layers, out_path, **kwargs):  # noqa: ANN001, ANN202
        captured["layers"] = layers
        out_path.write_bytes(b"PMTiles")
        return out_path

    with (
        patch("oex.exporter.list_layer_urls", return_value=staged),
        patch("oex.exporter.build_combined_pmtiles", side_effect=fake_build),
        patch("oex.exporter.connect", return_value=conn),
    ):
        exporter._build_combined_tiles(built_ok, out_root, "hotosm_npl")

    merged = {(layer.source, layer.category) for layer in captured["layers"]}
    assert ("overture", "buildings") in merged
    assert ("osm", "buildings") in merged
    assert ("overture", "rivers") not in merged, "stale staged layer must not be merged"
