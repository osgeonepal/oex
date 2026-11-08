"""The bundled example schemas load and produce the expected categories; querying data
is not tested here."""

from collections.abc import Callable
from pathlib import Path

import pytest

from oex.config.schema import RootConfig

EXAMPLES = Path(__file__).resolve().parent.parent / "configs" / "examples"

LoadConfig = Callable[[str], RootConfig]


def _load_schema(load_yaml_config: LoadConfig, schema_filename: str) -> RootConfig:
    schema_path = EXAMPLES / schema_filename
    assert schema_path.exists(), f"missing bundled schema {schema_path}"
    return load_yaml_config(f"iso3: NPL\nkey: test\ncategories_file: {schema_path}\n")


@pytest.mark.parametrize(
    "schema_filename", ["custom-schema.yaml", "hot-schema.yaml", "overture-package-schema.yaml"]
)
def test_each_bundled_schema_loads_named_categories_with_a_source_enabled(
    load_yaml_config: LoadConfig, schema_filename: str
) -> None:
    cfg = _load_schema(load_yaml_config, schema_filename)
    assert cfg.categories, f"{schema_filename} loaded no categories"
    for c in cfg.categories:
        assert c.name, f"unnamed category in {schema_filename}"
        assert c.overture.enabled or c.osm.enabled, (
            f"{schema_filename}: category {c.name!r} has no source enabled"
        )


def test_hot_schema_layer_names_match_hdx(load_yaml_config: LoadConfig) -> None:
    """The HOT schema names must match the HOT HDX dataset suffixes."""
    cfg = _load_schema(load_yaml_config, "hot-schema.yaml")
    expected = {
        "buildings",
        "roads",
        "waterways",
        "railways",
        "education_facilities",
        "health_facilities",
        "populated_places",
        "financial_services",
        "airports",
        "sea_ports",
        "points_of_interest",
        "cultural_places",
    }
    assert {c.name for c in cfg.categories} == expected


def test_overture_package_schema_covers_all_themes_with_osm_off(
    load_yaml_config: LoadConfig,
) -> None:
    """Overture data-package schema must cover every theme/type the bucket exposes."""
    cfg = _load_schema(load_yaml_config, "overture-package-schema.yaml")
    pairs = {(c.overture.theme, c.overture.feature_type) for c in cfg.categories}
    expected_pairs = {
        ("addresses", "address"),
        ("base", "bathymetry"),
        ("base", "infrastructure"),
        ("base", "land"),
        ("base", "land_cover"),
        ("base", "land_use"),
        ("base", "water"),
        ("buildings", "building"),
        ("buildings", "building_part"),
        ("divisions", "division_boundary"),
        ("divisions", "division"),
        ("divisions", "division_area"),
        ("places", "place"),
        ("transportation", "connector"),
        ("transportation", "segment"),
    }
    assert pairs == expected_pairs
    assert [c.name for c in cfg.categories if c.osm.enabled] == []
