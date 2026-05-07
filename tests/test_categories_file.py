"""Custom schema loading via `categories_file`."""

from pathlib import Path

import pytest

from oex.config.loader import ConfigError, load_config


def _write_schema(path: Path, *, names: list[str]) -> None:
    cats = "\n".join(
        f"""
- name: {n}
  overture:
    theme: x
    feature_type: x
    select: [id]
  osm:
    filter:
      a: true
    select: [feature_id]
"""
        for n in names
    )
    path.write_text(f"categories:\n{cats}\n", encoding="utf-8")


def test_categories_file_replaces_defaults(tmp_path: Path) -> None:
    schema_path = tmp_path / "custom.yaml"
    _write_schema(schema_path, names=["Foo", "Bar"])

    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text(
        f"""
iso3: NPL
key: test
categories_file: {schema_path}
""",
        encoding="utf-8",
    )
    cfg = load_config(user_yaml)
    assert [c.name for c in cfg.categories] == ["Foo", "Bar"]


def test_user_categories_block_wins_over_categories_file(tmp_path: Path) -> None:
    schema_path = tmp_path / "custom.yaml"
    _write_schema(schema_path, names=["Foo"])

    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text(
        f"""
iso3: NPL
key: test
categories_file: {schema_path}
categories:
  - name: Inline
    overture:
      theme: x
      feature_type: x
      select: [id]
    osm:
      filter:
        a: true
      select: [feature_id]
""",
        encoding="utf-8",
    )
    cfg = load_config(user_yaml)
    assert [c.name for c in cfg.categories] == ["Inline"]


def test_categories_file_must_have_categories_key(tmp_path: Path) -> None:
    schema_path = tmp_path / "bad.yaml"
    schema_path.write_text("not_categories: []\n", encoding="utf-8")

    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text(
        f"""
iso3: NPL
key: test
categories_file: {schema_path}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="categories"):
        load_config(user_yaml)
