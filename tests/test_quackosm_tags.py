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

    row = duckdb.execute(f"SELECT tags['name'], tags['source'] FROM '{parquet}'").fetchone()
    assert row == ("Test Hospital", "Bing")


def test_lifecycle_outlines_are_areas_and_a_roundabout_stays_a_road(tmp_path: Path) -> None:
    """A responder measures damaged footprints; a roundabout is a closed road, not an area."""
    category = CategoryConfig(name="mixed")
    category.osm = CategoryOsm(
        filter={"destroyed:building": True, "destroyed:aeroway": True, "junction": True}
    )
    cfg = RootConfig(iso3="NPL", categories=[category])
    parquet = tmp_path / "country.parquet"
    pbf = Path(__file__).parent / "fixtures" / "destroyed_building.osm.pbf"

    OsmRunner()._build_country_parquet(cfg, pbf, parquet, tmp_path, engine="planet")

    connection = duckdb.connect()
    connection.execute("INSTALL spatial; LOAD spatial")
    kinds = dict(
        connection.execute(
            f"SELECT feature_id, ST_GeometryType(geometry) FROM '{parquet}'"
        ).fetchall()
    )
    assert kinds == {"way/10": "POLYGON", "way/11": "POLYGON", "way/12": "LINESTRING"}
