"""SPATIAL_JOIN's optimizer needs a single spatial predicate per ON clause;
any second condition demotes the plan to BLOCKWISE_NL_JOIN at ~25-40x cost.
Admin polygons are pre-simplified at ~10m tolerance because SPATIAL_JOIN's
ray-cast cost scales linearly with vertex count.

https://duckdb.org/2025/08/08/spatial-joins
https://duckdb.org/2025/05/21/announcing-duckdb-130
"""

from dataclasses import dataclass
from pathlib import Path

import duckdb

from oex.logging_setup import get_logger
from oex.pcodes.cache import PcodeCacheEntry

logger = get_logger(__name__)


@dataclass(frozen=True)
class PcodeTagReport:
    iso3: str
    levels_tagged: list[int]
    levels_empty: list[int]
    adm0_pcode: str | None
    adm0_name: str | None


# Smaller than a typical building footprint. fieldmaps.io adm polygons carry
# tens of thousands of vertices each; simplifying at this tolerance drops the
# join cost by 25-40x with no observable change in tagging accuracy.
# Verified in scripts/bench_tagger.py against BGD's 12M buildings.
_ADMIN_SIMPLIFY_TOLERANCE_DEG = 0.0001


def _country_admin_table_name(table: str, level: int) -> str:
    return f"_pcodes_adm{level}_{table}"


def _tagged_table_name(table: str) -> str:
    return f"_pcodes_tagged_{table}"


def _load_admin_table(
    conn: duckdb.DuckDBPyConnection,
    *,
    parquet_path: Path,
    iso3: str,
    level: int,
    target_table: str,
) -> int:
    conn.execute(
        f"""
        CREATE OR REPLACE TABLE {target_table} AS
        SELECT
            adm{level}_src AS pcode,
            adm{level}_name AS name,
            adm0_src AS adm0_pcode,
            adm0_name AS adm0_name,
            ST_SimplifyPreserveTopology(
                CAST(geometry AS GEOMETRY),
                {_ADMIN_SIMPLIFY_TOLERANCE_DEG}
            ) AS admin_geom
        FROM read_parquet(?)
        WHERE iso_3 = ?
        """,
        [str(parquet_path), iso3],
    )
    row = conn.execute(f"SELECT COUNT(*) FROM {target_table}").fetchone()
    return int(row[0]) if row else 0


def _read_adm0(conn: duckdb.DuckDBPyConnection, source_table: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        f"SELECT adm0_pcode, adm0_name FROM {source_table} WHERE adm0_pcode IS NOT NULL LIMIT 1"
    ).fetchone()
    if row is None:
        return None, None
    return (row[0], row[1])


def _prepare_admin_tables(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    iso3: str,
    cache_entries: dict[int, PcodeCacheEntry],
    levels: list[int],
) -> tuple[dict[int, str], list[int], list[int]]:
    admin_tables: dict[int, str] = {}
    levels_with_data: list[int] = []
    levels_empty: list[int] = []
    for level in levels:
        entry = cache_entries.get(level)
        if entry is None:
            logger.warning("[pcodes] level %d missing from cache; emitting NULLs", level)
            levels_empty.append(level)
            continue
        target = _country_admin_table_name(table, level)
        admin_tables[level] = target
        count = _load_admin_table(
            conn,
            parquet_path=entry.path,
            iso3=iso3,
            level=level,
            target_table=target,
        )
        if count == 0:
            logger.warning(
                "[pcodes] adm%d has no polygons for ISO3=%s; emitting NULLs",
                level,
                iso3,
            )
            levels_empty.append(level)
        else:
            logger.info("[pcodes] adm%d: %d polygons loaded for %s", level, count, iso3)
            levels_with_data.append(level)
    return admin_tables, levels_with_data, levels_empty


def _drop_tables(conn: duckdb.DuckDBPyConnection, names: list[str]) -> None:
    for name in names:
        conn.execute(f"DROP TABLE IF EXISTS {name}")


def _add_null_pcode_columns(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    requested_levels: list[int],
    adm0_pcode: str,
) -> None:
    conn.execute(f"ALTER TABLE {table} ADD COLUMN adm0_pcode VARCHAR")
    conn.execute(f"UPDATE {table} SET adm0_pcode = ?", [adm0_pcode])
    conn.execute(f"ALTER TABLE {table} ADD COLUMN adm0_name VARCHAR")
    for level in requested_levels:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN adm{level}_pcode VARCHAR")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN adm{level}_name VARCHAR")


def _handle_no_admin_data(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    iso3: str,
    requested_levels: list[int],
    levels_empty: list[int],
    admin_tables: dict[int, str],
) -> PcodeTagReport:
    _add_null_pcode_columns(
        conn,
        table=table,
        requested_levels=requested_levels,
        adm0_pcode=iso3,
    )
    _drop_tables(conn, list(admin_tables.values()))
    logger.warning(
        "[pcodes] no admin polygons for ISO3=%s at any requested level; "
        "added schema-stable null columns only",
        iso3,
    )
    return PcodeTagReport(
        iso3=iso3,
        levels_tagged=[],
        levels_empty=levels_empty,
        adm0_pcode=None,
        adm0_name=None,
    )


def _build_tagged_select(
    *,
    feature_table: str,
    geom_column: str,
    requested_levels: list[int],
    levels_with_data: list[int],
    admin_tables: dict[int, str],
) -> str:
    select_cols: list[str] = [
        f"f.* EXCLUDE ({geom_column})",
        f"f.{geom_column} AS {geom_column}",
        "?::VARCHAR AS adm0_pcode",
        "?::VARCHAR AS adm0_name",
    ]
    join_clauses: list[str] = []
    for level in requested_levels:
        if level in levels_with_data:
            alias = f"a{level}"
            select_cols.append(f"{alias}.pcode AS adm{level}_pcode")
            select_cols.append(f"{alias}.name AS adm{level}_name")
            join_clauses.append(
                f"LEFT JOIN {admin_tables[level]} AS {alias} "
                f"ON ST_Contains({alias}.admin_geom, ST_Centroid(f.{geom_column}))"
            )
        else:
            select_cols.append(f"NULL AS adm{level}_pcode")
            select_cols.append(f"NULL AS adm{level}_name")
    return f"SELECT {', '.join(select_cols)}\nFROM {feature_table} AS f\n" + "\n".join(join_clauses)


def _replace_table_with_tagged(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    tagged_table: str,
) -> None:
    # Both statements live in the same DuckDB transaction (autocommit off by
    # default for explicit BEGIN); a failure between them rolls back.
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tagged_table} RENAME TO {table}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tag_table(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    iso3: str,
    cache_entries: dict[int, PcodeCacheEntry],
    levels: list[int],
    geom_column: str = "geom",
) -> PcodeTagReport:
    iso3_upper = iso3.upper()
    requested = sorted(set(levels))
    if not requested:
        raise ValueError("levels must be non-empty")

    admin_tables, levels_with_data, levels_empty = _prepare_admin_tables(
        conn,
        table=table,
        iso3=iso3_upper,
        cache_entries=cache_entries,
        levels=requested,
    )

    if not levels_with_data:
        return _handle_no_admin_data(
            conn,
            table=table,
            iso3=iso3_upper,
            requested_levels=requested,
            levels_empty=levels_empty,
            admin_tables=admin_tables,
        )

    seed_table = admin_tables[levels_with_data[0]]
    adm0_pcode, adm0_name = _read_adm0(conn, seed_table)
    if adm0_pcode is None:
        adm0_pcode = iso3_upper

    select_sql = _build_tagged_select(
        feature_table=table,
        geom_column=geom_column,
        requested_levels=requested,
        levels_with_data=levels_with_data,
        admin_tables=admin_tables,
    )

    tagged_table = _tagged_table_name(table)
    conn.execute(f"DROP TABLE IF EXISTS {tagged_table}")
    conn.execute(
        f"CREATE TABLE {tagged_table} AS\n{select_sql}",
        [adm0_pcode, adm0_name],
    )
    _replace_table_with_tagged(conn, table=table, tagged_table=tagged_table)
    _drop_tables(conn, list(admin_tables.values()))

    logger.info(
        "[pcodes] tagged %s: ISO3=%s adm0=(%s, %s) levels_with_data=%s levels_empty=%s",
        table,
        iso3_upper,
        adm0_pcode,
        adm0_name,
        levels_with_data,
        levels_empty,
    )
    return PcodeTagReport(
        iso3=iso3_upper,
        levels_tagged=levels_with_data,
        levels_empty=levels_empty,
        adm0_pcode=adm0_pcode,
        adm0_name=adm0_name,
    )
