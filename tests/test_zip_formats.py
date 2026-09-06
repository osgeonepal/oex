"""`output.zip_formats` decides which formats are zipped and which publish as bare files."""

from pathlib import Path

import pytest

from oex.config.loader import load_config
from oex.config.schema import CategoryConfig, RootConfig
from oex.exporter import Exporter
from oex.sources.base import SourceQuery, SourceRunner


class _NoRunner(SourceRunner):
    """`_should_zip` reads only the config, but Exporter needs a runner to construct."""

    name = "test"

    def prepare(self, cfg: RootConfig) -> None:
        raise NotImplementedError

    def query_for(self, cfg: RootConfig, category: CategoryConfig) -> SourceQuery:
        raise NotImplementedError


def exporter_for(tmp_path: Path, body: str) -> Exporter:
    path = tmp_path / "c.yaml"
    path.write_text(f"iso3: NPL\nkey: t\n{body}", encoding="utf-8")
    return Exporter(load_config(path), runner=_NoRunner())


def test_every_format_is_zipped_by_default(tmp_path):
    """Existing configs must keep publishing zips."""
    exporter = exporter_for(tmp_path, "")
    for fmt in ("gpkg", "geojson", "kml", "shp"):
        assert exporter._should_zip(CategoryConfig(name="c"), fmt) is True


def test_only_the_listed_formats_are_zipped(tmp_path):
    exporter = exporter_for(tmp_path, "output:\n  zip_formats: [gpkg]\n")
    category = CategoryConfig(name="c")
    assert exporter._should_zip(category, "gpkg") is True
    assert exporter._should_zip(category, "geojson") is False
    assert exporter._should_zip(category, "kml") is False


def test_a_shapefile_is_always_zipped(tmp_path):
    """A shapefile is a set of sidecar files, so a bare .shp would be unusable."""
    exporter = exporter_for(tmp_path, "output:\n  zip_formats: []\n")
    assert exporter._should_zip(CategoryConfig(name="c"), "shp") is True


def test_an_empty_list_zips_nothing_else(tmp_path):
    exporter = exporter_for(tmp_path, "output:\n  zip_formats: []\n")
    assert exporter._should_zip(CategoryConfig(name="c"), "geojson") is False


def test_a_category_overrides_the_output_setting(tmp_path):
    exporter = exporter_for(tmp_path, "output:\n  zip_formats: []\n")
    category = CategoryConfig(name="c", zip_formats=["geojson"])
    assert exporter._should_zip(category, "geojson") is True
    assert exporter._should_zip(category, "kml") is False


def test_an_unsupported_zip_format_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("iso3: NPL\noutput:\n  zip_formats: [gepkg]\n", encoding="utf-8")
    from oex.config import ConfigError

    with pytest.raises(ConfigError, match="gepkg"):
        load_config(path)


def test_geoparquet_cannot_be_asked_to_zip(tmp_path):
    """Zipping it would defeat reading it in place, so a config saying so is a mistake."""
    path = tmp_path / "c.yaml"
    path.write_text("iso3: NPL\noutput:\n  zip_formats: [gpkg, geoparquet]\n", encoding="utf-8")
    from oex.config import ConfigError

    with pytest.raises(ConfigError, match="no effect"):
        load_config(path)


def test_geoparquet_is_still_a_valid_output_format(tmp_path):
    """Only zip_formats rejects it; asking for the format itself is fine."""
    path = tmp_path / "c.yaml"
    path.write_text("iso3: NPL\noutput:\n  formats: [geoparquet]\n", encoding="utf-8")
    assert load_config(path).output.formats == ["geoparquet"]
