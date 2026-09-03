"""Postpass engine tests: predicate translation, id mapping, parquet contract.

The one network call is stubbed; the parquet build runs for real against DuckDB
because the schema it produces is the contract the exporter depends on.
"""

import json
from pathlib import Path

import pytest

from oex.config import ConfigError
from oex.config.loader import load_config
from oex.osm.country_parquet import PARQUET_CONTRACT, write_country_parquet
from oex.osm.postpass import (
    _feature_id_expr,
    _rows,
    boundary_area_sq_km,
    build_sql,
    fetch_country_parquet,
    hstore_predicate,
)
from oex.osm.runner import OsmRunner

SQUARE = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [[[85.0, 27.0], [85.1, 27.0], [85.1, 27.1], [85.0, 27.1], [85.0, 27.0]]],
    }
)


def test_true_becomes_a_key_existence_check() -> None:
    assert hstore_predicate({"building": True}) == "(exist(tags, 'building'))"


def test_list_becomes_an_in_clause() -> None:
    assert hstore_predicate({"amenity": ["police", "bank"]}) == (
        "(tags->'amenity' IN ('police', 'bank'))"
    )


def test_string_becomes_an_equality_check() -> None:
    assert hstore_predicate({"place": "town"}) == "(tags->'place' = 'town')"


def test_multiple_keys_are_ored_in_sorted_order() -> None:
    predicate = hstore_predicate({"highway": True, "building": True})
    assert predicate == "(exist(tags, 'building') OR exist(tags, 'highway'))"


def test_single_quotes_are_escaped_on_keys_and_values() -> None:
    predicate = hstore_predicate({"na'me": ["o'brien"]})
    assert "na''me" in predicate
    assert "o''brien" in predicate


def test_false_values_are_skipped_like_the_duckdb_predicate() -> None:
    assert hstore_predicate({"building": True, "highway": False}) == "(exist(tags, 'building'))"


def test_a_filter_with_nothing_usable_fails_loud() -> None:
    with pytest.raises(ValueError, match="non-empty osm.filter"):
        hstore_predicate({"highway": False})


def test_empty_filter_fails_loud() -> None:
    with pytest.raises(ValueError, match="non-empty osm.filter"):
        hstore_predicate({})


def test_points_carry_node_ids() -> None:
    assert _feature_id_expr("planet_osm_point") == "'node/' || osm_id"


def test_negative_ids_are_relations_on_the_other_tables() -> None:
    for table in ("planet_osm_line", "planet_osm_polygon"):
        expr = _feature_id_expr(table)
        assert "'relation/' || (-osm_id)" in expr
        assert "'way/' || osm_id" in expr


def test_sql_clips_to_the_boundary_and_carries_wkt() -> None:
    sql = build_sql("planet_osm_polygon", "(exist(tags, 'building'))", SQUARE)
    assert "ST_Intersects(way, g)" in sql
    assert "ST_AsText(way) AS wkt" in sql
    assert "hstore_to_json(tags)" in sql
    # A GeoJSON geometry never contains a single quote, but the escape must survive.
    assert "ST_GeomFromGeoJSON('" in sql


def test_rows_drop_features_without_geometry() -> None:
    payload = {
        "features": [
            {"properties": {"feature_id": "way/1", "tags_json": "{}", "wkt": "POINT(85 27)"}},
            {"properties": {"feature_id": "way/2", "tags_json": "{}", "wkt": None}},
        ]
    }
    assert _rows(payload) == [("way/1", "{}", "POINT(85 27)")]


def test_written_parquet_matches_the_quackosm_contract(tmp_path: Path) -> None:
    out = tmp_path / "country.parquet"
    write_country_parquet(
        [("way/1", '{"building":"yes"}', "POLYGON((0 0,1 0,1 1,0 0))")], out, "test"
    )

    import duckdb

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    schema = [
        (n, t)
        for n, t, *_ in conn.execute(f"DESCRIBE SELECT * FROM read_parquet('{out}')").fetchall()
    ]
    assert schema == PARQUET_CONTRACT
    tags = conn.execute(f"SELECT tags['building'] FROM read_parquet('{out}')").fetchall()
    assert tags == [("yes",)]


def test_single_part_geometries_are_downcast_to_match_quackosm(tmp_path: Path) -> None:
    """osm2pgsql returns MULTI* for everything; the published files must not change shape."""
    out = tmp_path / "country.parquet"
    write_country_parquet(
        [
            ("way/1", "{}", "MULTIPOLYGON(((0 0,1 0,1 1,0 0)))"),
            ("way/2", "{}", "MULTILINESTRING((0 0,1 1))"),
            ("way/3", "{}", "MULTIPOLYGON(((0 0,1 0,1 1,0 0)),((5 5,6 5,6 6,5 5)))"),
        ],
        out,
        "test",
    )

    import duckdb

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    kinds = dict(
        conn.execute(
            f"SELECT feature_id, ST_GeometryType(geometry) FROM read_parquet('{out}')"
        ).fetchall()
    )
    assert kinds["way/1"] == "POLYGON"
    assert kinds["way/2"] == "LINESTRING"
    # Genuinely multipart geometry stays multipart.
    assert kinds["way/3"] == "MULTIPOLYGON"


def _stub_post(monkeypatch: pytest.MonkeyPatch, features_by_table: dict[str, list[dict]]) -> None:
    """Replace the one network call; keyed on the table named in the SQL."""

    def fake_post(endpoint: str, sql: str, timeout: int) -> dict:
        table = next(t for t in ("point", "line", "polygon") if f"planet_osm_{t}" in sql)
        return {
            "postpass_properties": {"timestamp": "2026-08-27T20:25:16Z"},
            "features": [{"properties": p} for p in features_by_table.get(table, [])],
        }

    monkeypatch.setattr("oex.osm.postpass._post", fake_post)


def test_an_empty_result_is_refused_rather_than_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DuckDB cannot write GeoParquet metadata with no geometries, so the column would
    come back as BLOB and every downstream ST_ call would fail on it."""
    _stub_post(monkeypatch, {})
    with pytest.raises(RuntimeError, match="no features"):
        fetch_country_parquet(
            boundary_geojson=SQUARE,
            tag_filter={"building": True},
            out_path=tmp_path / "country.parquet",
        )


def test_fetch_merges_all_three_tables_and_reports_the_data_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_post(
        monkeypatch,
        {
            "point": [
                {"feature_id": "node/1", "tags_json": '{"amenity":"police"}', "wkt": "POINT(85 27)"}
            ],
            "line": [
                {
                    "feature_id": "way/2",
                    "tags_json": '{"highway":"path"}',
                    "wkt": "LINESTRING(85 27,85.1 27.1)",
                }
            ],
            "polygon": [
                {
                    "feature_id": "way/3",
                    "tags_json": '{"building":"yes"}',
                    "wkt": "POLYGON((85 27,85.1 27,85.1 27.1,85 27))",
                }
            ],
        },
    )
    out = tmp_path / "country.parquet"
    snapshot = fetch_country_parquet(
        boundary_geojson=SQUARE, tag_filter={"building": True}, out_path=out
    )
    assert snapshot.feature_count == 3
    assert snapshot.label == "2026-08-27T20:25:16Z"
    assert snapshot.timestamp.year == 2026 and snapshot.timestamp.tzinfo is not None
    assert snapshot.parquet == out

    import duckdb

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    ids = [
        r[0]
        for r in conn.execute(f"SELECT feature_id FROM read_parquet('{out}') ORDER BY 1").fetchall()
    ]
    assert ids == ["node/1", "way/2", "way/3"]


def test_area_is_measured_geodesically() -> None:
    # Roughly 0.1 degree square near the equator-ish latitude 27; about 110 km2.
    area = boundary_area_sq_km(SQUARE)
    assert 100 < area < 125


def _config(tmp_path: Path, *, max_area: float | None = None) -> Path:
    ceiling = f"    postpass_max_area_sq_km: {max_area}\n" if max_area is not None else ""
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        f"""
iso3: NPL
key: t
boundary:
  geom: '{SQUARE}'
source:
  osm:
    engine: postpass
    cache_dir: {tmp_path / "osm"}
{ceiling}categories:
  - name: buildings
    osm:
      enabled: true
      filter:
        building: true
""",
        encoding="utf-8",
    )
    return yaml


def test_postpass_engine_is_accepted_at_config_load(tmp_path: Path) -> None:
    cfg = load_config(_config(tmp_path))
    assert cfg.source["osm"].engine == "postpass"


def test_a_boundary_over_the_ceiling_is_refused(tmp_path: Path) -> None:
    cfg = load_config(_config(tmp_path, max_area=10.0))
    with pytest.raises(ValueError, match="above the"):
        OsmRunner().prepare(cfg)


def test_a_zero_ceiling_is_rejected_at_config_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="postpass_max_area_sq_km"):
        load_config(_config(tmp_path, max_area=0))
