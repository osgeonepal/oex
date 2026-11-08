"""Add Latin display columns to a materialised table via unidecode."""

from typing import TYPE_CHECKING

import duckdb
from unidecode import unidecode

from oex.logging_setup import get_logger
from oex.sql import sql_identifier

if TYPE_CHECKING:
    from oex.config.schema import TransliterateRule

logger = get_logger(__name__)

_UDF_NAME = "oex_translit"


def _to_latin(text: str | None) -> str | None:
    if text is None:
        return None
    return unidecode(text)


def _ensure_udf(conn: duckdb.DuckDBPyConnection) -> None:
    registered = conn.execute(
        "SELECT 1 FROM duckdb_functions() WHERE function_name = ?", [_UDF_NAME]
    ).fetchone()
    if registered is None:
        conn.create_function(_UDF_NAME, _to_latin, ["VARCHAR"], "VARCHAR")


def _require_varchar(table: str, column: str, schema: dict[str, str], role: str) -> None:
    column_type = schema[column]
    if column_type.upper().startswith("VARCHAR"):
        return
    raise ValueError(
        f"transliterate {role} column {column!r} on table {table} has type "
        f"{column_type!r}; expected VARCHAR. For Overture name fields use "
        f"`names.common->>'en'` (text) instead of `names.common->'en'` (JSON)."
    )


def transliterate_table(
    conn: duckdb.DuckDBPyConnection,
    *,
    table: str,
    rules: list["TransliterateRule"],
) -> None:
    if not rules:
        return
    _ensure_udf(conn)
    schema = {row[0]: row[1] for row in conn.execute(f"DESCRIBE {table}").fetchall()}
    existing = set(schema)
    for rule in rules:
        if not rule.target or not rule.source:
            raise ValueError(f"transliterate rule needs both target and source: {rule!r}")
        if rule.source not in existing:
            raise ValueError(
                f"transliterate source column {rule.source!r} not in table {table}; "
                f"available: {sorted(existing)}"
            )
        if rule.prefer is not None and rule.prefer not in existing:
            raise ValueError(
                f"transliterate prefer column {rule.prefer!r} not in table {table}; "
                f"available: {sorted(existing)}"
            )
        _require_varchar(table, rule.source, schema, "source")
        if rule.prefer is not None:
            _require_varchar(table, rule.prefer, schema, "prefer")

        quoted_target = sql_identifier(rule.target)
        quoted_source = sql_identifier(rule.source)
        if rule.target in existing:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {quoted_target}")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {quoted_target} VARCHAR")

        if rule.prefer is not None:
            quoted_prefer = sql_identifier(rule.prefer)
            update_sql = f"UPDATE {table} SET {quoted_target} = COALESCE({quoted_prefer}, {_UDF_NAME}({quoted_source}))"
        else:
            update_sql = f"UPDATE {table} SET {quoted_target} = {_UDF_NAME}({quoted_source})"
        conn.execute(update_sql)
        existing.add(rule.target)
        logger.info(
            "[translit] %s.%s <- %s%s",
            table,
            rule.target,
            rule.source,
            f" (prefer {rule.prefer})" if rule.prefer else "",
        )
