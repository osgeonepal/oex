"""GEOMETRY is longitude,latitude; the session's `geometry_always_xy` keeps GDAL writing it so."""

import duckdb
import pytest

from oex.duckdb_session import connect
from oex.writers import write_format

HAITI = (-72.3, 18.5)
LAOS = (102.6, 17.9)


@pytest.fixture
def conn(tmp_path):
    session = connect(path=tmp_path / "session.duckdb", temp_dir=tmp_path / "tmp")
    yield session
    session.close()


@pytest.mark.parametrize(("lon", "lat"), [HAITI, LAOS])
def test_kml_coordinates_survive_a_round_trip(conn, tmp_path, lon, lat):
    conn.execute(f"CREATE OR REPLACE TABLE layer1 AS SELECT ST_Point({lon}, {lat}) AS geom")
    written = write_format(conn, "layer1", "layer1", "kml", tmp_path / "kml")
    got = conn.execute(f"SELECT ST_X(geom), ST_Y(geom) FROM ST_Read('{written[0]}')").fetchone()
    assert got[0] == pytest.approx(lon, abs=0.01)
    assert got[1] == pytest.approx(lat, abs=0.01)


def test_geopackage_declares_epsg_4326(conn, tmp_path):
    """OGC:CRS84 writes srs_id 100000 with organization NONE, which consumers do not expect."""
    conn.execute("CREATE OR REPLACE TABLE layer1 AS SELECT ST_Point(-72.3, 18.5) AS geom")
    written = write_format(conn, "layer1", "layer1", "gpkg", tmp_path / "gpkg")
    reader = duckdb.connect()
    reader.execute("INSTALL sqlite; LOAD sqlite;")
    contents = reader.execute(
        f"SELECT srs_id FROM sqlite_scan('{written[0]}', 'gpkg_contents')"
    ).fetchone()
    assert contents is not None, "the GeoPackage declares no layer"
    declared = reader.execute(
        f"SELECT srs_id, organization FROM sqlite_scan('{written[0]}', 'gpkg_spatial_ref_sys') "
        f"WHERE srs_id = {contents[0]}"
    ).fetchone()
    assert declared == (4326, "EPSG")
