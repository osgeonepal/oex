"""The country parquet built by quackosm from a real PBF."""

from pathlib import Path

import duckdb

from oex.config.schema import CategoryConfig, CategoryOsm, RootConfig
from oex.osm.runner import OsmRunner

# One hospital node tagged amenity=hospital, name=Test Hospital and source=Bing.
PBF = Path(__file__).parent / "fixtures" / "source_tag.osm.pbf"


def test_the_source_tag_survives_into_the_country_parquet(tmp_path: Path) -> None:
    category = CategoryConfig(name="health_facilities")
    category.osm = CategoryOsm(filter={"amenity": ["hospital"]})
    cfg = RootConfig(iso3="NPL", categories=[category])
    parquet = tmp_path / "country.parquet"

    OsmRunner()._build_country_parquet(cfg, PBF, parquet, tmp_path, engine="planet")

    row = duckdb.sql(f"SELECT tags['name'], tags['source'] FROM '{parquet}'").fetchone()
    assert row == ("Test Hospital", "Bing")
