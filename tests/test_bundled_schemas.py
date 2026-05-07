"""Smoke tests for the bundled example schemas in `configs/examples/`.

We only verify that the schemas load cleanly and produce the expected
category shape, not that they query data successfully (that's the job of
the integration tests).
"""

from pathlib import Path

import pytest

from oex.config.loader import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("schema_filename", "expected_count", "description"),
    [
        ("custom-schema.yaml", 2, "Hospitals + Healthcare POIs"),
        ("hot-schema.yaml", 11, "11 HOT HDX layers"),
        ("overture-package-schema.yaml", 15, "15 Overture (theme,type) pairs"),
    ],
)
def test_bundled_schema_loads(
    tmp_path: Path,
    schema_filename: str,
    expected_count: int,
    description: str,
) -> None:
    schema_path = REPO_ROOT / "configs" / "examples" / schema_filename
    assert schema_path.exists(), f"missing bundled schema {schema_path}"

    user_yaml = tmp_path / "country.yaml"
    user_yaml.write_text(
        f"""
iso3: NPL
key: test
categories_file: {schema_path}
""",
        encoding="utf-8",
    )
    cfg = load_config(user_yaml)
    assert len(cfg.categories) == expected_count, description
    # Every category must have a non-empty name and at least one source enabled.
    for c in cfg.categories:
        assert c.name, f"unnamed category in {schema_filename}"
        assert c.overture.enabled or c.osm.enabled, (
            f"{schema_filename}: category {c.name!r} has no source enabled"
        )


def test_hot_schema_layer_names_match_hdx() -> None:
    """The HOT schema names must match the HOT HDX dataset suffixes."""
    schema_path = REPO_ROOT / "configs" / "examples" / "hot-schema.yaml"
    assert schema_path.exists()

    cfg = load_config(_with_categories_file(schema_path))
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
    }
    assert {c.name for c in cfg.categories} == expected


def test_overture_package_schema_covers_all_themes() -> None:
    """Overture data-package schema must cover every theme/type the bucket exposes."""
    schema_path = REPO_ROOT / "configs" / "examples" / "overture-package-schema.yaml"
    cfg = load_config(_with_categories_file(schema_path))

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

    # All categories should have OSM disabled in this Overture-only schema.
    for c in cfg.categories:
        assert c.osm.enabled is False, f"{c.name}: osm should be disabled"


def _with_categories_file(schema_path: Path) -> str:
    """Write a tiny user YAML that points at `schema_path` as a temp file path."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(f"iso3: NPL\nkey: test\ncategories_file: {schema_path}\n")
    tmp.close()
    return tmp.name
