"""Per-dataset metadata report (feature counts, geom types, bbox, column stats)."""

from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

from oex.logging_setup import get_logger

logger = get_logger(__name__)

_GEOMETRY_COL = "geom"
_TOP_VALUES_LIMIT = 5
_TOP_VALUES_MAX_COL_DISTINCT = 1000


@dataclass
class ColumnReport:
    name: str
    type: str
    null_count: int
    null_percent: float
    distinct_count: int
    top_values: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MetadataReport:
    feature_count: int
    geometry_types: dict[str, int]
    bbox: tuple[float, float, float, float] | None
    columns: list[ColumnReport]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metadata(conn: duckdb.DuckDBPyConnection, table_name: str) -> MetadataReport:
    feature_count = _count_rows(conn, table_name)
    geometry_types = _geometry_breakdown(conn, table_name)
    bbox = _bbox(conn, table_name) if feature_count else None
    columns = _column_reports(conn, table_name, feature_count)
    summary = _summary(feature_count, geometry_types, columns)
    return MetadataReport(
        feature_count=feature_count,
        geometry_types=geometry_types,
        bbox=bbox,
        columns=columns,
        summary=summary,
    )


def _count_rows(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _geometry_breakdown(conn: duckdb.DuckDBPyConnection, table: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT ST_GeometryType({_GEOMETRY_COL}) AS gt, COUNT(*) "
        f"FROM {table} GROUP BY gt ORDER BY 2 DESC"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _bbox(conn: duckdb.DuckDBPyConnection, table: str) -> tuple[float, float, float, float]:
    row = conn.execute(
        f"SELECT MIN(ST_XMin({_GEOMETRY_COL})), MIN(ST_YMin({_GEOMETRY_COL})), "
        f"MAX(ST_XMax({_GEOMETRY_COL})), MAX(ST_YMax({_GEOMETRY_COL})) FROM {table}"
    ).fetchone()
    if not row or row[0] is None:
        return (0.0, 0.0, 0.0, 0.0)
    return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))


def _column_reports(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    feature_count: int,
) -> list[ColumnReport]:
    schema = conn.execute(f"DESCRIBE {table}").fetchall()
    reports: list[ColumnReport] = []
    for row in schema:
        name = row[0]
        col_type = row[1]
        if name == _GEOMETRY_COL:
            continue
        null_count, distinct_count = _null_and_distinct(conn, table, name)
        null_percent = (null_count / feature_count * 100.0) if feature_count else 0.0
        top_values = _top_values(conn, table, name, distinct_count)
        reports.append(
            ColumnReport(
                name=name,
                type=col_type,
                null_count=null_count,
                null_percent=round(null_percent, 2),
                distinct_count=distinct_count,
                top_values=top_values,
            )
        )
    return reports


def _null_and_distinct(conn: duckdb.DuckDBPyConnection, table: str, column: str) -> tuple[int, int]:
    quoted = _quote(column)
    row = conn.execute(
        f"SELECT SUM(CASE WHEN {quoted} IS NULL THEN 1 ELSE 0 END) AS null_count, "
        f"COUNT(DISTINCT {quoted}) AS distinct_count FROM {table}"
    ).fetchone()
    if not row:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


def _top_values(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    distinct_count: int,
) -> list[dict[str, Any]]:
    if distinct_count == 0 or distinct_count > _TOP_VALUES_MAX_COL_DISTINCT:
        return []
    quoted = _quote(column)
    rows = conn.execute(
        f"SELECT {quoted} AS v, COUNT(*) AS n FROM {table} "
        f"WHERE {quoted} IS NOT NULL GROUP BY v ORDER BY n DESC LIMIT {_TOP_VALUES_LIMIT}"
    ).fetchall()
    return [{"value": _stringify(r[0]), "count": int(r[1])} for r in rows]


def _summary(
    feature_count: int,
    geometry_types: dict[str, int],
    columns: list[ColumnReport],
) -> str:
    if feature_count == 0:
        return "Empty dataset."
    parts = [f"{feature_count:,} features"]
    if geometry_types:
        gt_summary = ", ".join(f"{n:,} {gt}" for gt, n in geometry_types.items())
        parts.append(f"geometry types: {gt_summary}")
    sparse = [c.name for c in columns if c.null_percent >= 50.0]
    if sparse:
        parts.append(
            f"{len(sparse)} of {len(columns)} columns are >=50% null ({', '.join(sparse)})"
        )
    else:
        parts.append(f"all {len(columns)} attribute columns have <50% null share")
    return ". ".join(parts) + "."


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _stringify(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool, list, dict)):
        return value
    return str(value)
