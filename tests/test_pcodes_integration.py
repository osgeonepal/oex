"""End-to-end p-code tagging against live fieldmaps.io.

Marked `integration`: hits `data.fieldmaps.io` and downloads real admin
parquets. Skipped by `just test`; run with `just test-integration`.

Coverage:
- Manifest fetch + freshness check.
- Real ADM1/ADM2 parquet download for Nepal (smaller than ADM3/ADM4).
- Tagger end-to-end: feature centroids in known Nepal provinces resolve
  to the expected COD-AB pcodes (NP01..NP07).
"""

from pathlib import Path

import duckdb
import pytest

from oex.pcodes import ensure_admin_parquets, tag_table

pytestmark = pytest.mark.integration


_NEPAL_KATHMANDU = (85.3240, 27.7172)  # Kathmandu, Bagmati province (NP03)
_NEPAL_POKHARA = (83.9956, 28.2096)  # Pokhara, Gandaki province (NP04)
_NEPAL_BIRATNAGAR = (87.2718, 26.4525)  # Biratnagar, Koshi province (NP01)


def test_fieldmaps_cache_and_tag_against_nepal_adm1_adm2(tmp_path: Path) -> None:
    cache_dir = tmp_path / "pcodes_cache"

    cache_entries = ensure_admin_parquets(
        cache_dir=cache_dir,
        levels=[1, 2],
        manifest_url="https://data.fieldmaps.io/edge-matched.json",
        parquet_url_template=(
            "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
        ),
        manifest_group="humanitarian",
    )
    assert set(cache_entries) == {1, 2}
    for level, entry in cache_entries.items():
        assert entry.path.exists(), f"adm{level} parquet missing on disk"
        assert entry.path.stat().st_size > 1024, f"adm{level} parquet implausibly small"
        assert entry.upstream_date, f"adm{level} cache entry has no upstream_date"

    second = ensure_admin_parquets(
        cache_dir=cache_dir,
        levels=[1, 2],
        manifest_url="https://data.fieldmaps.io/edge-matched.json",
        parquet_url_template=(
            "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
        ),
        manifest_group="humanitarian",
    )
    for level in (1, 2):
        assert second[level].upstream_date == cache_entries[level].upstream_date
        assert second[level].path == cache_entries[level].path

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial; LOAD spatial")
    try:
        kt_x, kt_y = _NEPAL_KATHMANDU
        pk_x, pk_y = _NEPAL_POKHARA
        br_x, br_y = _NEPAL_BIRATNAGAR
        conn.execute(
            f"""
            CREATE TABLE features AS SELECT * FROM (VALUES
                (1, 'Kathmandu',  ST_Point({kt_x}, {kt_y})),
                (2, 'Pokhara',    ST_Point({pk_x}, {pk_y})),
                (3, 'Biratnagar', ST_Point({br_x}, {br_y})),
                (4, 'Mid-ocean',  ST_Point(0.0, 0.0))
            ) AS t(id, label, geom)
            """
        )

        report = tag_table(
            conn,
            table="features",
            iso3="NPL",
            cache_entries=cache_entries,
            levels=[1, 2],
        )
        assert report.iso3 == "NPL"
        assert report.adm0_pcode == "NPL"
        assert sorted(report.levels_tagged) == [1, 2]
        assert report.levels_empty == []

        rows = conn.execute(
            "SELECT id, adm0_pcode, adm1_pcode, adm2_pcode FROM features ORDER BY id"
        ).fetchall()
        by_id = {r[0]: r for r in rows}

        assert by_id[1][1] == "NPL"
        assert by_id[1][2] == "NP03", "Kathmandu must resolve to Bagmati (NP03)"
        assert by_id[2][2] == "NP04", "Pokhara must resolve to Gandaki (NP04)"
        assert by_id[3][2] == "NP01", "Biratnagar must resolve to Koshi (NP01)"

        assert by_id[4] == (4, "NPL", None, None)
    finally:
        conn.close()
