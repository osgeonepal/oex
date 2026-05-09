"""DuckDB-side p-code tagging."""

from dataclasses import dataclass
from pathlib import Path

import duckdb

from oex.logging_setup import get_logger
from oex.pcodes.cache import PcodeCacheEntry

logger = get_logger(__name__)


@dataclass(frozen=True)
class PcodeTagReport:
    """Outcome of tagging one materialised table."""

    iso3: str
    levels_tagged: list[int]
    levels_empty: list[int]
    adm0_pcode: str | None
    adm0_name: str | None


def _country_filtered_table_name(table: str, level: int) -> str:
    return f"_pcodes_adm{level}_{table}"


def _build_country_admin_table(
    conn: duckdb.DuckDBPyConnection,
    *,
    parquet_path: Path,
    iso3: str,
    level: int,
    target_table: str,
) -> int:
    # iso_3, not adm0_id: adm0_id carries fieldmaps' versioned key (NPL-20250729).
    # Cast geometry to plain GEOMETRY: RTREE rejects GEOMETRY('OGC:CRS84').
    sql = f"""
    CREATE OR REPLACE TABLE {target_table} AS
    SELECT
        adm{level}_src AS pcode,
        adm{level}_name AS name,
        adm0_src AS adm0_pcode,
        adm0_name AS adm0_name,
        CAST(geometry AS GEOMETRY) AS admin_geom
    FROM read_parquet(?)
    WHERE iso_3 = ?
    """
    conn.execute(sql, [str(parquet_path), iso3])
    row = conn.execute(f"SELECT COUNT(*) FROM {target_table}").fetchone()
    count = int(row[0]) if row else 0
    if count > 0:
        conn.execute(f"CREATE INDEX {target_table}_idx ON {target_table} USING RTREE (admin_geom)")
    return count


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
            logger.warning(
                "[pcodes] level %d requested but missing from cache; emitting NULLs",
                level,
            )
            levels_empty.append(level)
            continue
        admin_table = _country_filtered_table_name(table, level)
        admin_tables[level] = admin_table
        count = _build_country_admin_table(
            conn,
            parquet_path=entry.path,
            iso3=iso3,
            level=level,
            target_table=admin_table,
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


def _build_rewrite_sql(
    *,
    source_table: str,
    target_table: str,
    geom_column: str,
    requested_levels: list[int],
    levels_with_data: list[int],
    admin_tables: dict[int, str],
    adm0_pcode: str,
    adm0_name: str | None,
) -> str:
    # NULL::VARCHAR keeps the output schema constant when a level has no country rows.
    adm0_pcode_sql = adm0_pcode.replace("'", "''")
    select_extra: list[str] = [f"'{adm0_pcode_sql}' AS adm0_pcode"]
    if adm0_name is not None:
        adm0_name_sql = adm0_name.replace("'", "''")
        select_extra.append(f"'{adm0_name_sql}' AS adm0_name")
    else:
        select_extra.append("NULL::VARCHAR AS adm0_name")

    join_clauses: list[str] = []
    for level in requested_levels:
        if level in levels_with_data:
            alias = f"a{level}"
            select_extra.append(f"{alias}.pcode AS adm{level}_pcode")
            select_extra.append(f"{alias}.name  AS adm{level}_name")
            join_clauses.append(
                f"LEFT JOIN {admin_tables[level]} {alias} "
                f"ON ST_Within(ST_Centroid(t.{geom_column}), {alias}.admin_geom)"
            )
        else:
            select_extra.append(f"NULL::VARCHAR AS adm{level}_pcode")
            select_extra.append(f"NULL::VARCHAR AS adm{level}_name")

    return (
        f"CREATE OR REPLACE TABLE {target_table} AS\n"
        f"SELECT t.*,\n       "
        + ",\n       ".join(select_extra)
        + f"\nFROM {source_table} t\n"
        + "\n".join(join_clauses)
    )


def _drop_tables(conn: duckdb.DuckDBPyConnection, names: list[str]) -> None:
    for name in names:
        conn.execute(f"DROP TABLE IF EXISTS {name}")


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
        _drop_tables(conn, list(admin_tables.values()))
        logger.warning(
            "[pcodes] no admin polygons for ISO3=%s at any requested level; table %s not tagged",
            iso3_upper,
            table,
        )
        return PcodeTagReport(
            iso3=iso3_upper,
            levels_tagged=[],
            levels_empty=levels_empty,
            adm0_pcode=None,
            adm0_name=None,
        )

    seed_table = admin_tables[levels_with_data[0]]
    adm0_pcode, adm0_name = _read_adm0(conn, seed_table)
    if adm0_pcode is None:
        adm0_pcode = iso3_upper

    tagged_table = f"{table}__tagged"
    rewrite_sql = _build_rewrite_sql(
        source_table=table,
        target_table=tagged_table,
        geom_column=geom_column,
        requested_levels=requested,
        levels_with_data=levels_with_data,
        admin_tables=admin_tables,
        adm0_pcode=adm0_pcode,
        adm0_name=adm0_name,
    )
    logger.debug("[pcodes] rewrite SQL:\n%s", rewrite_sql)
    conn.execute(rewrite_sql)
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tagged_table} RENAME TO {table}")
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
