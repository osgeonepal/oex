"""`output.zip_formats` decides which formats are zipped and which publish as bare files."""

from collections.abc import Callable

import pytest

from oex.config import ConfigError
from oex.config.schema import CategoryConfig, RootConfig
from oex.exporter import Exporter

LoadConfig = Callable[[str], RootConfig]


@pytest.mark.parametrize(
    ("output", "category_zip_formats", "zipped", "bare"),
    [
        ("", None, ["gpkg", "geojson", "kml", "shp"], []),
        ("output:\n  zip_formats: [gpkg]\n", None, ["gpkg"], ["geojson", "kml"]),
        ("output:\n  zip_formats: []\n", None, ["shp"], ["geojson", "gpkg", "kml"]),
        ("output:\n  zip_formats: []\n", ["geojson"], ["geojson", "shp"], ["kml"]),
    ],
    ids=["default zips every format", "listed formats", "empty list", "category override"],
)
def test_only_the_configured_formats_and_shapefiles_are_zipped(
    load_yaml_config: LoadConfig,
    make_exporter: Callable[..., Exporter],
    output: str,
    category_zip_formats: list[str] | None,
    zipped: list[str],
    bare: list[str],
) -> None:
    """A shapefile is a set of sidecar files, so a bare .shp would be unusable."""
    exporter = make_exporter(load_yaml_config(f"iso3: NPL\nkey: t\n{output}"))
    category = CategoryConfig(name="c", zip_formats=category_zip_formats)
    assert [fmt for fmt in zipped if exporter._should_zip(category, fmt)] == zipped
    assert [fmt for fmt in bare if exporter._should_zip(category, fmt)] == []


@pytest.mark.parametrize(
    ("zip_formats", "message"),
    [("[gepkg]", "gepkg"), ("[gpkg, geoparquet]", "no effect")],
    ids=["unsupported format", "geoparquet"],
)
def test_an_unusable_zip_format_is_rejected_at_config_load(
    load_yaml_config: LoadConfig, zip_formats: str, message: str
) -> None:
    """Zipping GeoParquet would defeat reading it in place, so asking for it is a mistake."""
    with pytest.raises(ConfigError, match=message):
        load_yaml_config(f"iso3: NPL\noutput:\n  zip_formats: {zip_formats}\n")


def test_geoparquet_is_still_a_valid_output_format(load_yaml_config: LoadConfig) -> None:
    """Only zip_formats rejects it; asking for the format itself is fine."""
    cfg = load_yaml_config("iso3: NPL\noutput:\n  formats: [geoparquet]\n")
    assert cfg.output.formats == ["geoparquet"]
