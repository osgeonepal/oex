"""Pcode tagging by H3 hash join at resolution 7. Features on admin borders fall back to
a 1-ring H3 lookup, which bounds memory, or an exact GEOS join, which can exhaust it.
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import duckdb

from oex.logging_setup import get_logger
from oex.pcodes.cache import PcodeCacheEntry

logger = get_logger(__name__)

BoundaryResolution = Literal["h3_neighbor", "geos"]

_VALID_BOUNDARY_RESOLUTIONS: tuple[BoundaryResolution, ...] = ("h3_neighbor", "geos")


def parse_boundary_resolution(value: str) -> BoundaryResolution:
    for known in _VALID_BOUNDARY_RESOLUTIONS:
        if value == known:
            return known
    raise ValueError(
        f"boundary_resolution must be one of {list(_VALID_BOUNDARY_RESOLUTIONS)}, got {value!r}"
    )


@dataclass(frozen=True)
class PcodeTagReport:
    iso3: str
    levels_tagged: list[int]
    levels_empty: list[int]
    adm0_pcode: str | None
    adm0_name: str | None


_ADMIN_SIMPLIFY_TOLERANCE_DEGREES = 0.0001

# Resolution 7 cells average 5.16 km2.
_H3_RESOLUTION = 7


def _country_admin_table_name(table: str, level: int) -> str:
    return f"_pcodes_adm{level}_{table}"


def _tagged_table_name(table: str) -> str:
    return f"_pcodes_tagged_{table}"


def _slim_pcode_table_name(table: str) -> str:
    return f"_pcodes_slim_{table}"


def _h3_admin_table_name(table: str, level: int) -> str:
    return f"_pcodes_h3adm{level}_{table}"


def _feature_cell_table_name(table: str) -> str:
    return f"_pcodes_cells_{table}"


def _slim_level_table_name(table: str, level: int) -> str:
    return f"_pcodes_slimL{level}_{table}"


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
            CAST(geometry AS GEOMETRY) AS admin_geom
        FROM read_parquet(?)
        WHERE iso_3 = ?
        """,
        [str(parquet_path), iso3],
    )
    row = conn.execute(f"SELECT COUNT(*) FROM {target_table}").fetchone()
    return int(row[0]) if row else 0


def write_country_admin_parquets(
    conn: duckdb.DuckDBPyConnection,
    *,
    iso3: str,
    cache_entries: dict[int, PcodeCacheEntry],
    target_dir: Path,
) -> dict[int, PcodeCacheEntry]:
    """Every layer reloads the admin polygons, and simplifying a coast like Chile's takes
    minutes, so each level is filtered to the country and simplified here, once per run."""
    iso3_upper = iso3.upper()
    target_dir.mkdir(parents=True, exist_ok=True)
    country_entries: dict[int, PcodeCacheEntry] = {}
    for level, entry in cache_entries.items():
        path = target_dir / f"adm{level}_{iso3_upper}.parquet"
        conn.sql(
            f"""
            SELECT * REPLACE (
                ST_SimplifyPreserveTopology(
                    CAST(geometry AS GEOMETRY), {_ADMIN_SIMPLIFY_TOLERANCE_DEGREES}
                ) AS geometry
            )
            FROM read_parquet(?)
            WHERE iso_3 = ?
            """,
            params=[str(entry.path), iso3_upper],
        ).write_parquet(str(path))
        country_entries[level] = replace(entry, path=path)
    return country_entries


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


def _add_null_columns_without_admin_data(
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


def _tessellate_admin_to_h3(
    conn: duckdb.DuckDBPyConnection,
    *,
    admin_table: str,
    target_table: str,
) -> int:
    # h3_polygon_wkt_to_cells returns no cells for MULTIPOLYGON WKT, so the parts go in one by one.
    conn.execute(f"""
        CREATE TABLE {target_table} AS
        WITH parts AS (
            SELECT pcode, name, (UNNEST(st_dump(admin_geom))).geom AS part
            FROM {admin_table}
        )
        SELECT pcode, name,
               UNNEST(h3_polygon_wkt_to_cells(ST_AsText(part), {_H3_RESOLUTION})) AS cell
        FROM parts
    """)
    row = conn.execute(f"SELECT COUNT(*) FROM {target_table}").fetchone()
    return int(row[0]) if row else 0


def _build_feature_cells(
    conn: duckdb.DuckDBPyConnection,
    *,
    feature_table: str,
    geom_column: str,
    target_table: str,
) -> int:
    # centroid_pt is reused by the GEOS fallback, so it never has to scan the full
    # feature table. Keeps memory bounded on countries with millions of features.
    conn.execute(f"""
        CREATE TABLE {target_table} AS
        SELECT rowid AS _rid,
               ST_Centroid({geom_column}) AS centroid_pt,
               h3_latlng_to_cell(
                   ST_Y(ST_Centroid({geom_column})),
                   ST_X(ST_Centroid({geom_column})),
                   {_H3_RESOLUTION}
               ) AS cell
        FROM {feature_table}
    """)
    row = conn.execute(f"SELECT COUNT(*) FROM {target_table}").fetchone()
    return int(row[0]) if row else 0


def _build_geos_fallback(
    conn: duckdb.DuckDBPyConnection,
    *,
    h3_matches_table: str,
    feature_cell_table: str,
    admin_table: str,
    level: int,
    fallback_table: str,
) -> None:
    conn.execute(f"""
        CREATE TABLE {fallback_table} AS
        SELECT c._rid,
               a.pcode AS adm{level}_pcode,
               a.name  AS adm{level}_name
        FROM (SELECT _rid FROM {h3_matches_table} WHERE adm{level}_pcode IS NULL) AS h
        JOIN {feature_cell_table} AS c ON h._rid = c._rid
        LEFT JOIN {admin_table} AS a
               ON ST_Contains(a.admin_geom, c.centroid_pt)
    """)


def _build_h3_neighbor_fallback(
    conn: duckdb.DuckDBPyConnection,
    *,
    h3_matches_table: str,
    feature_cell_table: str,
    admin_h3_table: str,
    level: int,
    fallback_table: str,
) -> None:
    # Tie-break by hit count so a residual whose 1-ring touches multiple admins
    # picks the majority-overlap admin, not an arbitrary one.
    conn.execute(f"""
        CREATE TABLE {fallback_table} AS
        WITH residuals AS (
            SELECT c._rid, c.cell
            FROM (SELECT _rid FROM {h3_matches_table} WHERE adm{level}_pcode IS NULL) AS h
            JOIN {feature_cell_table} AS c ON h._rid = c._rid
        ),
        neighbor_hits AS (
            SELECT r._rid, a.pcode, a.name, COUNT(*) AS hits
            FROM residuals AS r,
                 UNNEST(h3_grid_disk(r.cell, 1)) AS t(ncell)
            JOIN {admin_h3_table} AS a ON t.ncell = a.cell
            GROUP BY r._rid, a.pcode, a.name
        )
        SELECT DISTINCT ON (_rid)
            _rid,
            pcode AS adm{level}_pcode,
            name  AS adm{level}_name
        FROM neighbor_hits
        ORDER BY _rid, hits DESC
    """)


def _h3_join_one_level(
    conn: duckdb.DuckDBPyConnection,
    *,
    feature_cell_table: str,
    admin_h3_table: str,
    admin_table: str,
    level: int,
    target_table: str,
    boundary_resolution: BoundaryResolution,
) -> tuple[int, int]:
    """(matched, total) feature counts once the H3 join and the boundary fallback have run."""
    h3_matches_table = f"_pcodes_h3s{level}_{feature_cell_table}"
    conn.execute(f"DROP TABLE IF EXISTS {h3_matches_table}")
    conn.execute(f"""
        CREATE TABLE {h3_matches_table} AS
        SELECT DISTINCT ON (b._rid)
            b._rid,
            a.pcode AS adm{level}_pcode,
            a.name  AS adm{level}_name
        FROM {feature_cell_table} AS b
        LEFT JOIN {admin_h3_table} AS a ON b.cell = a.cell
    """)

    counts = conn.execute(
        f"SELECT COUNT(*), COUNT(adm{level}_pcode) FROM {h3_matches_table}"
    ).fetchone()
    total, matched_h3 = (int(counts[0]), int(counts[1])) if counts else (0, 0)
    null_count = total - matched_h3

    logger.info(
        "[pcodes] adm%d h3: %d matched, %d -> %s boundary fallback (%.1f%%)",
        level,
        matched_h3,
        null_count,
        boundary_resolution,
        100.0 * null_count / total if total else 0.0,
    )

    if null_count == 0:
        conn.execute(f"ALTER TABLE {h3_matches_table} RENAME TO {target_table}")
    else:
        fallback_table = f"_pcodes_fb{level}_{feature_cell_table}"
        conn.execute(f"DROP TABLE IF EXISTS {fallback_table}")
        if boundary_resolution == "geos":
            _build_geos_fallback(
                conn,
                h3_matches_table=h3_matches_table,
                feature_cell_table=feature_cell_table,
                admin_table=admin_table,
                level=level,
                fallback_table=fallback_table,
            )
        else:
            _build_h3_neighbor_fallback(
                conn,
                h3_matches_table=h3_matches_table,
                feature_cell_table=feature_cell_table,
                admin_h3_table=admin_h3_table,
                level=level,
                fallback_table=fallback_table,
            )
        conn.execute(f"""
            CREATE TABLE {target_table} AS
            SELECT h._rid,
                   COALESCE(h.adm{level}_pcode, f.adm{level}_pcode) AS adm{level}_pcode,
                   COALESCE(h.adm{level}_name,  f.adm{level}_name)  AS adm{level}_name
            FROM {h3_matches_table} AS h
            LEFT JOIN {fallback_table} AS f ON h._rid = f._rid
        """)
        conn.execute(f"DROP TABLE {h3_matches_table}")
        conn.execute(f"DROP TABLE {fallback_table}")

    final_counts = conn.execute(
        f"SELECT COUNT(*), COUNT(adm{level}_pcode) FROM {target_table}"
    ).fetchone()
    total_final, matched_final = (
        (int(final_counts[0]), int(final_counts[1])) if final_counts else (0, 0)
    )

    logger.info(
        "[pcodes] adm%d final: %d/%d matched (%.1f%% null after fallback)",
        level,
        matched_final,
        total_final,
        100.0 * (total_final - matched_final) / total_final if total_final else 0.0,
    )
    return matched_final, total_final


def _build_full_tagged_select(
    *,
    feature_table: str,
    slim_table: str,
    geom_column: str,
    requested_levels: list[int],
) -> str:
    """Rejoin slim pcode results back to full feature table via rowid."""
    pcode_columns = ["p.adm0_pcode", "p.adm0_name"]
    for level in requested_levels:
        pcode_columns.append(f"p.adm{level}_pcode")
        pcode_columns.append(f"p.adm{level}_name")
    column_list = ", ".join(pcode_columns)
    return (
        f"SELECT f.* EXCLUDE ({geom_column}), f.{geom_column}, {column_list}\n"
        f"FROM {feature_table} AS f\n"
        f"LEFT JOIN {slim_table} AS p ON f.rowid = p._rid"
    )


def _replace_table_with_tagged(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    tagged_table: str,
) -> None:
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tagged_table} RENAME TO {table}")
        conn.execute("COMMIT")
    except duckdb.Error:
        conn.execute("ROLLBACK")
        raise


def _levels_with_pcodes(
    conn: duckdb.DuckDBPyConnection,
    *,
    admin_tables: dict[int, str],
    levels_with_data: list[int],
    iso3: str,
) -> list[int]:
    """Levels whose polygons all have NULL pcodes (e.g. IND adm4) can never produce a useful
    tag, so they skip the H3 tessellation."""
    kept: list[int] = []
    for level in levels_with_data:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {admin_tables[level]} WHERE pcode IS NOT NULL"
        ).fetchone()
        if row is None or row[0] == 0:
            logger.info(
                "[pcodes] adm%d: no pcode values for ISO3=%s, skipping H3 tessellation",
                level,
                iso3,
            )
            continue
        kept.append(level)
    return kept


def _build_h3_lookups(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    admin_tables: dict[int, str],
    levels: list[int],
    iso3: str,
) -> dict[int, str]:
    h3_tables: dict[int, str] = {}
    for level in levels:
        target = _h3_admin_table_name(table, level)
        conn.execute(f"DROP TABLE IF EXISTS {target}")
        count = _tessellate_admin_to_h3(
            conn,
            admin_table=admin_tables[level],
            target_table=target,
        )
        h3_tables[level] = target
        logger.info("[pcodes] adm%d h3 lookup: %d cells for %s", level, count, iso3)
    return h3_tables


def _create_null_level_table(
    conn: duckdb.DuckDBPyConnection, *, level: int, feature_cell_table: str, target_table: str
) -> None:
    conn.execute(f"""
        CREATE TABLE {target_table} AS
        SELECT _rid,
               NULL::VARCHAR AS adm{level}_pcode,
               NULL::VARCHAR AS adm{level}_name
        FROM {feature_cell_table}
    """)


def _build_slim_level_tables(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    requested: list[int],
    feature_cell_table: str,
    h3_tables: dict[int, str],
    admin_tables: dict[int, str],
    boundary_resolution: BoundaryResolution,
) -> tuple[dict[int, str], list[int]]:
    """One (_rid, pcode, name) table per requested level, plus the levels that matched any
    feature."""
    slim_level_tables: dict[int, str] = {}
    levels_tagged: list[int] = []
    for level in requested:
        slim_level_table = _slim_level_table_name(table, level)
        conn.execute(f"DROP TABLE IF EXISTS {slim_level_table}")
        if level in h3_tables:
            matched, _ = _h3_join_one_level(
                conn,
                feature_cell_table=feature_cell_table,
                admin_h3_table=h3_tables[level],
                admin_table=admin_tables[level],
                level=level,
                target_table=slim_level_table,
                boundary_resolution=boundary_resolution,
            )
            conn.execute(f"DROP TABLE IF EXISTS {h3_tables[level]}")
            if matched > 0:
                levels_tagged.append(level)
        else:
            _create_null_level_table(
                conn,
                level=level,
                feature_cell_table=feature_cell_table,
                target_table=slim_level_table,
            )
        slim_level_tables[level] = slim_level_table
    return slim_level_tables, levels_tagged


def _combine_slim_levels(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    requested: list[int],
    slim_level_tables: dict[int, str],
    adm0_pcode: str,
    adm0_name: str | None,
) -> str:
    """Join the per-level tables into one slim pcode table; returns its name."""
    slim_table = _slim_pcode_table_name(table)
    first_level = requested[0]
    join_clauses = "\n".join(
        f"JOIN {slim_level_tables[level]} AS p{level} ON p{first_level}._rid = p{level}._rid"
        for level in requested[1:]
    )
    level_columns = ", ".join(
        column
        for level in requested
        for column in (f"p{level}.adm{level}_pcode", f"p{level}.adm{level}_name")
    )
    conn.execute(f"DROP TABLE IF EXISTS {slim_table}")
    conn.execute(
        f"""
        CREATE TABLE {slim_table} AS
        SELECT p{first_level}._rid,
               ?::VARCHAR AS adm0_pcode,
               ?::VARCHAR AS adm0_name,
               {level_columns}
        FROM {slim_level_tables[first_level]} AS p{first_level}
        {join_clauses}
        """,
        [adm0_pcode, adm0_name],
    )
    _drop_tables(conn, list(slim_level_tables.values()))
    return slim_table


def _rejoin_and_swap(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    slim_table: str,
    geom_column: str,
    requested: list[int],
) -> None:
    """Replace the feature table with itself plus the pcode columns."""
    tagged_table = _tagged_table_name(table)
    rejoin_sql = _build_full_tagged_select(
        feature_table=table,
        slim_table=slim_table,
        geom_column=geom_column,
        requested_levels=requested,
    )
    conn.execute(f"DROP TABLE IF EXISTS {tagged_table}")
    conn.execute(f"CREATE TABLE {tagged_table} AS\n{rejoin_sql}")
    conn.execute(f"DROP TABLE IF EXISTS {slim_table}")
    _replace_table_with_tagged(conn, table=table, tagged_table=tagged_table)


def tag_table(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    iso3: str,
    cache_entries: dict[int, PcodeCacheEntry],
    levels: list[int],
    geom_column: str = "geom",
    boundary_resolution: BoundaryResolution = "h3_neighbor",
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
        return _add_null_columns_without_admin_data(
            conn,
            table=table,
            iso3=iso3_upper,
            requested_levels=requested,
            levels_empty=levels_empty,
            admin_tables=admin_tables,
        )

    adm0_pcode, adm0_name = _read_adm0(conn, admin_tables[levels_with_data[0]])
    if adm0_pcode is None:
        adm0_pcode = iso3_upper

    h3_tables = _build_h3_lookups(
        conn,
        table=table,
        admin_tables=admin_tables,
        levels=_levels_with_pcodes(
            conn, admin_tables=admin_tables, levels_with_data=levels_with_data, iso3=iso3_upper
        ),
        iso3=iso3_upper,
    )
    cell_table = _feature_cell_table_name(table)
    conn.execute(f"DROP TABLE IF EXISTS {cell_table}")
    cell_count = _build_feature_cells(
        conn,
        feature_table=table,
        geom_column=geom_column,
        target_table=cell_table,
    )
    logger.info("[pcodes] %d feature cells built for %s", cell_count, iso3_upper)

    slim_level_tables, levels_tagged = _build_slim_level_tables(
        conn,
        table=table,
        requested=requested,
        feature_cell_table=cell_table,
        h3_tables=h3_tables,
        admin_tables=admin_tables,
        boundary_resolution=boundary_resolution,
    )
    conn.execute(f"DROP TABLE IF EXISTS {cell_table}")
    slim_table = _combine_slim_levels(
        conn,
        table=table,
        requested=requested,
        slim_level_tables=slim_level_tables,
        adm0_pcode=adm0_pcode,
        adm0_name=adm0_name,
    )
    _rejoin_and_swap(
        conn, table=table, slim_table=slim_table, geom_column=geom_column, requested=requested
    )
    _drop_tables(conn, list(admin_tables.values()))

    all_levels_empty = [level for level in requested if level not in levels_tagged]
    logger.info(
        "[pcodes] tagged %s: ISO3=%s adm0=(%s, %s) levels_tagged=%s levels_empty=%s",
        table,
        iso3_upper,
        adm0_pcode,
        adm0_name,
        levels_tagged,
        all_levels_empty,
    )
    return PcodeTagReport(
        iso3=iso3_upper,
        levels_tagged=levels_tagged,
        levels_empty=all_levels_empty,
        adm0_pcode=adm0_pcode,
        adm0_name=adm0_name,
    )
