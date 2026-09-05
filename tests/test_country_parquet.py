"""The country.parquet contract shared by every OSM engine.

The exporter, the category selects and the published schema all assume these three
columns, so a change here changes every dataset oex publishes.
"""

from pathlib import Path

import duckdb
import pytest

from oex.osm.country_parquet import write_country_parquet


def _schema(path: Path) -> list[tuple[str, str]]:
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    return [
        (name, dtype)
        for name, dtype, *_ in conn.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path}')"
        ).fetchall()
    ]


def test_tags_round_trip_as_a_map(tmp_path: Path) -> None:
    out = tmp_path / "country.parquet"
    write_country_parquet(
        [("way/1", '{"building":"house","name:ne":"घर"}', "POINT(85 27)")], out, "test"
    )
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    row = conn.execute(
        f"SELECT tags['building'], tags['name:ne'] FROM read_parquet('{out}')"
    ).fetchall()
    assert row == [("house", "घर")]


def test_single_part_geometries_are_downcast(tmp_path: Path) -> None:
    """Upstreams differ on MULTI wrapping; published files must not change shape with the engine."""
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
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    kinds = dict(
        conn.execute(
            f"SELECT feature_id, ST_GeometryType(geometry) FROM read_parquet('{out}')"
        ).fetchall()
    )
    assert kinds["way/1"] == "POLYGON"
    assert kinds["way/2"] == "LINESTRING"
    assert kinds["way/3"] == "MULTIPOLYGON"


def test_contract_drift_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An engine that silently changed the schema would break every downstream layer."""
    from oex.osm import country_parquet

    monkeypatch.setattr(country_parquet, "PARQUET_CONTRACT", [("feature_id", "VARCHAR")])
    with pytest.raises(RuntimeError, match="does not match the contract"):
        write_country_parquet([("way/1", "{}", "POINT(85 27)")], tmp_path / "c.parquet", "test")


def test_the_engine_name_appears_in_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oex.osm import country_parquet

    monkeypatch.setattr(country_parquet, "PARQUET_CONTRACT", [("nope", "VARCHAR")])
    with pytest.raises(RuntimeError, match="Raw Data API parquet schema"):
        write_country_parquet(
            [("way/1", "{}", "POINT(85 27)")], tmp_path / "c.parquet", "Raw Data API"
        )
