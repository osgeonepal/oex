import pytest

from oex.config.schema import CategoryConfig, RootConfig
from oex.sources.base import exports_category


def _category(**overture) -> CategoryConfig:
    category = CategoryConfig(name="schools")
    for key, value in overture.items():
        setattr(category.overture, key, value)
    return category


@pytest.mark.parametrize(
    ("overture", "exported"),
    [
        ({"enabled": True, "theme": "places", "feature_type": "place"}, True),
        ({"enabled": True, "theme": "", "feature_type": "place"}, False),
        ({"enabled": False, "theme": "places", "feature_type": "place"}, False),
    ],
)
def test_overture_exports_only_a_category_with_theme_and_type(overture, exported):
    """The Overture runner skips the rest, so they never get an Item."""
    assert exports_category(RootConfig(), _category(**overture), "overture") is exported


def test_a_category_turned_off_for_a_source_is_not_exported():
    category = CategoryConfig(name="schools")
    category.osm.enabled = False
    assert not exports_category(RootConfig(), category, "osm")


def test_a_category_without_formats_is_not_exported():
    cfg = RootConfig()
    cfg.output.formats = []
    assert not exports_category(cfg, CategoryConfig(name="schools"), "osm")
    assert exports_category(RootConfig(), CategoryConfig(name="schools"), "osm")
