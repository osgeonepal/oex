"""Per-dataset metadata report (feature counts, geom types, bbox, column stats)."""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

from oex.logging_setup import get_logger

logger = get_logger(__name__)

GEOMETRY_COL = "geom"
ID_COL = "id"
_GEOMETRY_WORDS = {
    "LINESTRING": "line",
    "MULTILINESTRING": "multiline",
    "GEOMETRYCOLLECTION": "geometry collection",
}
ADMIN_COLUMN_RE = re.compile(r"^(adm\d+)_(pcode|name)$")
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
class TemporalReport:
    column: str
    min: str | None
    max: str | None
    non_null_count: int


@dataclass
class MetadataReport:
    feature_count: int
    geometry_types: dict[str, int]
    bbox: tuple[float, float, float, float] | None
    columns: list[ColumnReport]
    summary: str
    temporal: TemporalReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metadata(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    *,
    temporal_column: str | None = None,
) -> MetadataReport:
    feature_count = _count_rows(conn, table_name)
    geometry_types = geometry_breakdown(conn, table_name)
    bbox = bounding_box(conn, table_name) if feature_count else None
    columns = _column_reports(conn, table_name, feature_count)
    summary = _summary(feature_count, geometry_types, columns)
    temporal = (
        _temporal_bounds(conn, table_name, temporal_column)
        if temporal_column and feature_count
        else None
    )
    return MetadataReport(
        feature_count=feature_count,
        geometry_types=geometry_types,
        bbox=bbox,
        columns=columns,
        summary=summary,
        temporal=temporal,
    )


def _count_rows(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def geometry_breakdown(conn: duckdb.DuckDBPyConnection, table: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT ST_GeometryType({GEOMETRY_COL}) AS gt, COUNT(*) "
        f"FROM {table} GROUP BY gt ORDER BY 2 DESC"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def bounding_box(conn: duckdb.DuckDBPyConnection, table: str) -> tuple[float, float, float, float]:
    row = conn.execute(
        f"SELECT MIN(ST_XMin({GEOMETRY_COL})), MIN(ST_YMin({GEOMETRY_COL})), "
        f"MAX(ST_XMax({GEOMETRY_COL})), MAX(ST_YMax({GEOMETRY_COL})) FROM {table}"
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
        if name == GEOMETRY_COL:
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


def is_source_attribute(name: str) -> bool:
    """oex fills `id` and the admin columns itself, so they say nothing about the source."""
    return name != ID_COL and ADMIN_COLUMN_RE.match(name) is None


def _summary(
    feature_count: int,
    geometry_types: dict[str, int],
    columns: list[ColumnReport],
) -> str:
    """One readable line for the STAC description; per-column detail is in `table:columns`."""
    if feature_count == 0:
        return "No features."
    shapes = sorted(geometry_types.items(), key=lambda kv: kv[1], reverse=True)
    counts = ", ".join(f"{n:,} {_geometry_word(g, n)}" for g, n in shapes)
    head = f"{feature_count:,} {_plural('feature', feature_count)}" + (
        f": {counts}." if counts else "."
    )
    attributes = [c for c in columns if is_source_attribute(c.name)]
    if not attributes:
        return head
    filled = sum(1 for c in attributes if c.null_percent < 50.0)
    verb = "is" if len(attributes) == 1 else "are"
    return (
        f"{head} {filled} of {len(attributes)} {_plural('attribute', len(attributes))} {verb} "
        f"filled for at least half of the {_plural('feature', feature_count)}."
    )


def _geometry_word(geometry_type: str, count: int) -> str:
    return _plural(_GEOMETRY_WORDS.get(geometry_type, geometry_type.lower()), count)


def _plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def _temporal_bounds(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
) -> TemporalReport | None:
    schema = {row[0]: row[1] for row in conn.execute(f"DESCRIBE {table}").fetchall()}
    if column not in schema:
        logger.warning(
            "temporal column %r not found in table %s; skipping temporal bounds",
            column,
            table,
        )
        return None
    quoted = _quote(column)
    row = conn.execute(
        f"SELECT MIN({quoted}) AS lo, MAX({quoted}) AS hi, COUNT({quoted}) AS n FROM {table}"
    ).fetchone()
    if not row or row[2] == 0:
        return TemporalReport(column=column, min=None, max=None, non_null_count=0)
    lo, hi, n = row
    return TemporalReport(
        column=column,
        min=_stringify_temporal(lo),
        max=_stringify_temporal(hi),
        non_null_count=int(n),
    )


def _stringify_temporal(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _stringify(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool, list, dict)):
        return value
    return str(value)
