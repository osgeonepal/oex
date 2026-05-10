"""Unit tests for the planet engine path in OsmRunner.

osmium and quackosm are mocked. Tests exercise: the build pipeline calls,
caching/idempotency, the engine-aware query_for, and geofabrik->planet
fallback wiring.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from oex.config.schema import (
    BoundaryConfig,
    CategoryConfig,
    CategoryOsm,
    OsmSourceConfig,
    RootConfig,
)
from oex.osm.geofabrik import GeofabrikUnavailableError
from oex.osm.runner import OsmRunner

NPL_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[[80, 26], [88, 26], [88, 30], [80, 30], [80, 26]]],
}


def _category(
    name: str,
    osm_filter: dict | None = None,
    *,
    enabled: bool = True,
    select: list[str] | None = None,
) -> CategoryConfig:
    cat = CategoryConfig(name=name)
    cat.osm = CategoryOsm(
        enabled=enabled,
        filter=osm_filter or {},
        select=select or ["feature_id AS id", "tags['name'] AS name"],
        where=[],
    )
    return cat


def _planet_cfg(tmp_path: Path, *, planet_pbf: Path) -> RootConfig:
    cfg = RootConfig(
        iso3="NPL",
        boundary=BoundaryConfig(
            geom=None,
            geoboundaries_release="CGAZ",
            geoboundaries_level="ADM0",
            buffer_meters=5000,
        ),
        categories=[
            _category("buildings", {"building": True}),
            _category("roads", {"highway": True}),
        ],
    )
    cfg.source["osm"] = OsmSourceConfig(
        engine="planet",
        cache_dir=str(tmp_path / "cache"),
        pbf_path=str(planet_pbf),
    )
    return cfg


def _seed_country_parquet(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute(
        f"""
        COPY (
            SELECT
                'way/1' AS feature_id,
                MAP(['building', 'name'], ['yes', 'A']) AS tags,
                ST_GeomFromText('POINT (85 28)') AS geometry
            UNION ALL
            SELECT
                'way/2' AS feature_id,
                MAP(['highway', 'name'], ['primary', 'Road']) AS tags,
                ST_GeomFromText('POINT (85.1 28.1)') AS geometry
        ) TO '{path}' (FORMAT PARQUET)
        """
    )
    conn.close()


def test_planet_prepare_calls_osmium_and_quackosm(tmp_path: Path) -> None:
    planet_pbf = tmp_path / "planet.osm.pbf"
    planet_pbf.write_bytes(b"\x00")
    cfg = _planet_cfg(tmp_path, planet_pbf=planet_pbf)
    osmium_called: dict = {}

    def fake_osmium_extract(pbf_path, polygon, out_pbf, **_):  # noqa: ANN001
        osmium_called["pbf"] = pbf_path
        osmium_called["polygon_keys"] = sorted(polygon.keys())
        out_pbf.write_bytes(b"\x00")

    quackosm_args: dict = {}

    def fake_quackosm(**kw):
        quackosm_args.update(kw)
        _seed_country_parquet(kw["result_file_path"])

    with (
        patch(
            "oex.osm.runner.resolve_boundary",
            return_value=type("B", (), {"geojson": json.dumps(NPL_GEOJSON)})(),
        ),
        patch(
            "oex.osm.runner.osmium_polygon_extract",
            side_effect=fake_osmium_extract,
        ),
        patch(
            "quackosm.functions.convert_pbf_to_parquet",
            side_effect=fake_quackosm,
        ),
    ):
        runner = OsmRunner()
        runner.prepare(cfg)

    assert osmium_called["pbf"] == planet_pbf
    assert quackosm_args["keep_all_tags"] is True
    assert quackosm_args["sort_result"] is True
    assert quackosm_args["tags_filter"] == {"building": True, "highway": True}
    assert runner._engine == "planet"
    assert runner._country_parquet is not None
    assert runner._country_parquet.exists()
    assert runner._snapshot_dir is not None
    manifest = json.loads((runner._snapshot_dir / "manifest.json").read_text())
    assert manifest["engine"] == "planet"
    assert sorted(manifest["filter_keys"]) == ["building", "highway"]


def test_planet_prepare_is_idempotent_when_parquet_exists(tmp_path: Path) -> None:
    planet_pbf = tmp_path / "planet.osm.pbf"
    planet_pbf.write_bytes(b"\x00")
    cfg = _planet_cfg(tmp_path, planet_pbf=planet_pbf)

    snapshot_label = datetime.fromtimestamp(planet_pbf.stat().st_mtime, tz=UTC).date().isoformat()
    snapshot_dir = Path(cfg.source["osm"].cache_dir) / "planet" / "npl" / snapshot_label
    _seed_country_parquet(snapshot_dir / "country.parquet")

    with (
        patch(
            "oex.osm.runner.osmium_polygon_extract",
            side_effect=AssertionError("must not be called"),
        ),
        patch(
            "quackosm.functions.convert_pbf_to_parquet",
            side_effect=AssertionError("must not be called"),
        ),
    ):
        runner = OsmRunner()
        runner.prepare(cfg)

    assert runner._engine == "planet"
    assert runner._country_parquet is not None
    assert runner._country_parquet.exists()


def test_planet_query_for_applies_tag_predicate(tmp_path: Path) -> None:
    planet_pbf = tmp_path / "planet.osm.pbf"
    planet_pbf.write_bytes(b"\x00")
    cfg = _planet_cfg(tmp_path, planet_pbf=planet_pbf)

    snapshot_label = datetime.fromtimestamp(planet_pbf.stat().st_mtime, tz=UTC).date().isoformat()
    snapshot_dir = Path(cfg.source["osm"].cache_dir) / "planet" / "npl" / snapshot_label
    _seed_country_parquet(snapshot_dir / "country.parquet")

    runner = OsmRunner()
    with (
        patch("oex.osm.runner.osmium_polygon_extract"),
        patch("quackosm.functions.convert_pbf_to_parquet"),
    ):
        runner.prepare(cfg)

    buildings_q = runner.query_for(cfg, cfg.categories[0])
    assert "country.parquet" in buildings_q.source_expr
    assert any("tags['building'] IS NOT NULL" in w for w in buildings_q.where_conditions)

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    sql = (
        f"SELECT COUNT(*) FROM {buildings_q.source_expr} "
        f"WHERE {' AND '.join(buildings_q.where_conditions)}"
    )
    row = conn.execute(sql).fetchone()
    assert row is not None
    assert row[0] == 1


def test_planet_engine_requires_pbf_path(tmp_path: Path) -> None:
    cfg = _planet_cfg(tmp_path, planet_pbf=tmp_path / "x.pbf")
    cfg.source["osm"].pbf_path = ""

    with pytest.raises(ValueError, match="pbf_path"):
        OsmRunner().prepare(cfg)


def test_planet_engine_raises_when_pbf_missing(tmp_path: Path) -> None:
    cfg = _planet_cfg(tmp_path, planet_pbf=tmp_path / "missing.pbf")

    with pytest.raises(FileNotFoundError, match="Planet PBF"):
        OsmRunner().prepare(cfg)


def test_planet_engine_auto_downloads_when_flag_set(tmp_path: Path) -> None:
    """When auto_download_planet is true, missing PBF triggers download_pbf."""
    target_pbf = tmp_path / "missing.pbf"
    cfg = _planet_cfg(tmp_path, planet_pbf=target_pbf)
    cfg.source["osm"].auto_download_planet = True

    download_called: dict = {}

    def fake_download(url, dest_dir, *, md5_url=None, filename=None):  # noqa: ANN001
        download_called["url"] = url
        download_called["dest"] = dest_dir
        download_called["filename"] = filename
        out = Path(dest_dir) / (filename or "planet.osm.pbf")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00")
        return type("R", (), {"path": out})()

    def fake_quackosm(**kw):
        _seed_country_parquet(kw["result_file_path"])

    with (
        patch("oex.osm.runner.download_pbf", side_effect=fake_download),
        patch(
            "oex.osm.runner.resolve_boundary",
            return_value=type("B", (), {"geojson": json.dumps(NPL_GEOJSON)})(),
        ),
        patch(
            "oex.osm.runner.osmium_polygon_extract",
            side_effect=lambda pbf, geom, out, **_: out.write_bytes(b"\x00"),
        ),
        patch("quackosm.functions.convert_pbf_to_parquet", side_effect=fake_quackosm),
    ):
        runner = OsmRunner()
        runner.prepare(cfg)

    assert download_called["filename"] == "missing.pbf"
    assert download_called["url"] == cfg.source["osm"].pbf_url
    assert runner._engine == "planet"


def test_planet_engine_does_not_auto_download_by_default(tmp_path: Path) -> None:
    cfg = _planet_cfg(tmp_path, planet_pbf=tmp_path / "missing.pbf")
    assert cfg.source["osm"].auto_download_planet is False

    with patch(
        "oex.osm.runner.download_pbf",
        side_effect=AssertionError("must not be called"),
    ):
        with pytest.raises(FileNotFoundError, match="Planet PBF"):
            OsmRunner().prepare(cfg)


def test_geofabrik_falls_back_to_planet_when_unavailable(tmp_path: Path) -> None:
    planet_pbf = tmp_path / "planet.osm.pbf"
    planet_pbf.write_bytes(b"\x00")
    cfg = _planet_cfg(tmp_path, planet_pbf=planet_pbf)
    cfg.source["osm"].engine = "geofabrik"
    cfg.source["osm"].planet_fallback = True

    def fake_quackosm(**kw):
        _seed_country_parquet(kw["result_file_path"])

    with (
        patch(
            "oex.osm.runner.OsmRunner._prepare_geofabrik",
            side_effect=GeofabrikUnavailableError("no extract"),
        ),
        patch(
            "oex.osm.runner.resolve_boundary",
            return_value=type("B", (), {"geojson": json.dumps(NPL_GEOJSON)})(),
        ),
        patch(
            "oex.osm.runner.osmium_polygon_extract",
            side_effect=lambda pbf, geom, out, **_: out.write_bytes(b"\x00"),
        ),
        patch(
            "quackosm.functions.convert_pbf_to_parquet",
            side_effect=fake_quackosm,
        ),
    ):
        runner = OsmRunner()
        runner.prepare(cfg)

    assert runner._engine == "planet"


def test_geofabrik_propagates_other_errors_even_with_fallback(tmp_path: Path) -> None:
    planet_pbf = tmp_path / "planet.osm.pbf"
    planet_pbf.write_bytes(b"\x00")
    cfg = _planet_cfg(tmp_path, planet_pbf=planet_pbf)
    cfg.source["osm"].engine = "geofabrik"
    cfg.source["osm"].planet_fallback = True

    with patch(
        "oex.osm.runner.OsmRunner._prepare_geofabrik",
        side_effect=ConnectionError("network down"),
    ):
        with pytest.raises(ConnectionError):
            OsmRunner().prepare(cfg)


def test_geofabrik_does_not_fall_back_when_flag_off(tmp_path: Path) -> None:
    planet_pbf = tmp_path / "planet.osm.pbf"
    planet_pbf.write_bytes(b"\x00")
    cfg = _planet_cfg(tmp_path, planet_pbf=planet_pbf)
    cfg.source["osm"].engine = "geofabrik"
    cfg.source["osm"].planet_fallback = False

    with patch(
        "oex.osm.runner.OsmRunner._prepare_geofabrik",
        side_effect=GeofabrikUnavailableError("no extract"),
    ):
        with pytest.raises(GeofabrikUnavailableError):
            OsmRunner().prepare(cfg)
