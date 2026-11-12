"""Unit tests for the transliteration engine."""

from collections.abc import Iterator

import duckdb
import pytest

from oex.config.schema import TransliterateRule
from oex.translit import transliterate_table


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    c = duckdb.connect(":memory:")
    try:
        yield c
    finally:
        c.close()


def _seed(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE features AS SELECT * FROM (VALUES
            (1, 'नेपाल पशुपति विद्यालय', NULL),
            (2, 'काठमाडौँ', NULL),
            (3, 'Москва', 'Moscow'),
            (4, 'Pokhara School', 'Pokhara School'),
            (5, NULL, NULL),
            (6, NULL, 'Only English Name')
        ) AS t(id, name, name_en)
        """
    )


def test_transliterate_adds_column_with_prefer_fallback(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    transliterate_table(
        conn,
        table="features",
        rules=[TransliterateRule(target="name_latin", source="name", prefer="name_en")],
    )
    rows = {
        r[0]: r[1]
        for r in conn.execute("SELECT id, name_latin FROM features ORDER BY id").fetchall()
    }
    assert rows[1] == "nepaal pshupti vidyaaly"
    assert rows[2] == "kaatthmaaddauN"
    assert rows[3] == "Moscow"
    assert rows[4] == "Pokhara School"
    assert rows[5] is None
    assert rows[6] == "Only English Name"


def test_transliterate_without_prefer_falls_back_to_source(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    transliterate_table(
        conn,
        table="features",
        rules=[TransliterateRule(target="name_latin", source="name")],
    )
    rows = {
        r[0]: r[1]
        for r in conn.execute("SELECT id, name_latin FROM features ORDER BY id").fetchall()
    }
    assert rows[3] == "Moskva"
    assert rows[6] is None


def test_transliterate_replaces_existing_target_column(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    conn.execute("ALTER TABLE features ADD COLUMN name_latin VARCHAR")
    conn.execute("UPDATE features SET name_latin = 'stale'")
    transliterate_table(
        conn,
        table="features",
        rules=[TransliterateRule(target="name_latin", source="name")],
    )
    row = conn.execute("SELECT name_latin FROM features WHERE id = 4").fetchone()
    assert row is not None
    assert row[0] == "Pokhara School", "stale value must be overwritten"


def test_transliterate_raises_on_unknown_source(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    with pytest.raises(ValueError, match="not in table"):
        transliterate_table(
            conn,
            table="features",
            rules=[TransliterateRule(target="x", source="missing")],
        )


def test_a_layer_without_the_preferred_column_transliterates_its_source(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Overture carries no `name_en`, so its layers transliterate `name` alone."""
    _seed(conn)
    conn.execute("ALTER TABLE features DROP COLUMN name_en")
    transliterate_table(
        conn,
        table="features",
        rules=[TransliterateRule(target="name_latin", source="name", prefer="name_en")],
    )
    latin = dict(conn.execute("SELECT id, name_latin FROM features").fetchall())
    assert (latin[3], latin[6]) == ("Moskva", None)


def test_a_prefer_no_source_selects_is_refused_at_load(tmp_path) -> None:
    from oex.config.loader import ConfigError, load_config

    config = tmp_path / "x.yaml"
    config.write_text(
        "categories:\n"
        "  - name: schools\n"
        "    osm:\n"
        "      select: [\"tags['name'] AS name\", \"tags['name:en'] AS name_en\"]\n"
        "    overture:\n"
        "      select: [names.primary AS name]\n"
        "    transliterate:\n"
        "      - {target: name_latin, source: name, prefer: name_english}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="name_english"):
        load_config(config)


def test_transliterate_rejects_json_typed_source_with_actionable_hint(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    conn.execute("CREATE TABLE features (name VARCHAR, name_en JSON)")
    conn.execute("INSERT INTO features VALUES ('Foo', '\"Foo\"')")
    with pytest.raises(ValueError, match=r"->>'en'"):
        transliterate_table(
            conn,
            table="features",
            rules=[
                TransliterateRule(target="name_latin", source="name", prefer="name_en"),
            ],
        )


def test_transliterate_empty_rules_is_a_noop(conn: duckdb.DuckDBPyConnection) -> None:
    _seed(conn)
    cols_before = {r[0] for r in conn.execute("DESCRIBE features").fetchall()}
    transliterate_table(conn, table="features", rules=[])
    cols_after = {r[0] for r in conn.execute("DESCRIBE features").fetchall()}
    assert cols_before == cols_after


def test_every_rule_in_one_call_writes_its_target_column(conn: duckdb.DuckDBPyConnection) -> None:
    """Only the first rule prefers name_en, so row 3 differs between the two targets."""
    _seed(conn)
    conn.execute("ALTER TABLE features ADD COLUMN name_ne VARCHAR")
    conn.execute("UPDATE features SET name_ne = name")
    transliterate_table(
        conn,
        table="features",
        rules=[
            TransliterateRule(target="name_latin", source="name", prefer="name_en"),
            TransliterateRule(target="name_ne_latin", source="name_ne"),
        ],
    )
    rows = conn.execute(
        "SELECT name_latin, name_ne_latin FROM features WHERE id IN (2, 3) ORDER BY id"
    ).fetchall()
    assert rows == [("kaatthmaaddauN", "kaatthmaaddauN"), ("Moscow", "Moskva")]


def test_transliterating_twice_on_one_connection_adds_both_columns(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    transliterate_table(
        conn, table="features", rules=[TransliterateRule(target="a", source="name")]
    )
    transliterate_table(
        conn, table="features", rules=[TransliterateRule(target="b", source="name")]
    )
    assert conn.execute("SELECT a, b FROM features WHERE id = 2").fetchone() == (
        "kaatthmaaddauN",
        "kaatthmaaddauN",
    )


def test_a_prefer_one_source_selects_loads(tmp_path) -> None:
    from oex.config.loader import load_config

    config = tmp_path / "x.yaml"
    config.write_text(
        "categories:\n"
        "  - name: schools\n"
        "    osm:\n"
        "      select: [\"tags['name'] AS name\", \"tags['name:en'] AS name_en\"]\n"
        "    overture:\n"
        "      select: [names.primary AS name]\n"
        "    transliterate:\n"
        "      - {target: name_latin, source: name, prefer: name_en}\n",
        encoding="utf-8",
    )
    assert load_config(config).categories[0].transliterate[0].prefer == "name_en"


def test_a_transliteration_is_trimmed_and_one_that_comes_out_empty_is_null(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    conn.execute(
        "CREATE TABLE features AS SELECT * FROM (VALUES (1, '北京'), (2, '🙂'), (3, NULL)) t(id, name)"
    )
    transliterate_table(
        conn, table="features", rules=[TransliterateRule(target="name_latin", source="name")]
    )
    rows = conn.execute("SELECT id, name_latin FROM features ORDER BY id").fetchall()
    assert rows == [(1, "Bei Jing"), (2, None), (3, None)]
