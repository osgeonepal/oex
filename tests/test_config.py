"""Config loading and validation."""

from pathlib import Path

import pytest

from oex.config import (
    ConfigError,
    apply_overrides,
    iter_configs,
    load_config,
    select_categories,
)


def test_defaults_load_with_eight_categories() -> None:
    cfg = load_config()
    names = [c.name for c in cfg.categories]
    assert names == [
        "Buildings",
        "Roads",
        "Hospitals",
        "Schools",
        "Rivers",
        "Land Use",
        "Transportation Hubs",
        "Settlements",
    ]
    assert cfg.source["overture"].enabled is True
    assert cfg.source["osm"].enabled is True


def test_user_yaml_overrides_iso3_and_formats(tmp_path: Path) -> None:
    yaml_path = tmp_path / "country.yaml"
    yaml_path.write_text("iso3: NPL\nkey: test\noutput:\n  formats: [geojson]\n", encoding="utf-8")
    cfg = load_config(yaml_path)
    assert cfg.iso3 == "NPL"
    assert cfg.key == "test"
    assert cfg.output.formats == ["geojson"]


def test_user_yaml_replaces_categories_wholesale(tmp_path: Path) -> None:
    yaml_path = tmp_path / "country.yaml"
    yaml_path.write_text(
        """
iso3: NPL
key: test
categories:
  - name: Buildings
    overture:
      theme: buildings
      feature_type: building
      select: [id]
    osm:
      filter:
        building: true
      select: [feature_id]
""",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert len(cfg.categories) == 1
    assert cfg.categories[0].name == "Buildings"


def test_apply_overrides_dotted_keys() -> None:
    cfg = load_config()
    new_cfg = apply_overrides(cfg, {"iso3": "NPL", "hdx.push": True})
    assert new_cfg.iso3 == "NPL"
    assert new_cfg.hdx.push is True


def test_select_categories_filters_by_name() -> None:
    cfg = load_config()
    cfg = apply_overrides(cfg, {"iso3": "NPL"})
    only_buildings = select_categories(cfg, "buildings")
    assert [c.name for c in only_buildings.categories] == ["Buildings"]


def test_select_categories_unknown_theme_raises() -> None:
    cfg = load_config()
    with pytest.raises(ConfigError):
        select_categories(cfg, "nonexistent_theme")


def test_iter_configs_yields_yamls(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("iso3: ABC\n", encoding="utf-8")
    (tmp_path / "b.yml").write_text("iso3: BCD\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("nope", encoding="utf-8")
    paths = list(iter_configs(tmp_path))
    assert [p.name for p in paths] == ["a.yaml", "b.yml"]
