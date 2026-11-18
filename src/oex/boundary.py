"""Country boundary resolution: user-supplied geom, geoBoundaries or fieldmaps ADM0."""

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import antimeridian
import duckdb
import requests
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from oex.config.schema import BoundaryConfig
from oex.logging_setup import get_logger
from oex.sources.file import declared_crs, geometry_expr, layer_names
from oex.sql import sql_string

logger = get_logger(__name__)

_GEOBOUNDARIES_RELEASE = "gbOpen"
_GEOBOUNDARIES_URL_TEMPLATE = (
    f"https://www.geoboundaries.org/api/current/{_GEOBOUNDARIES_RELEASE}/{{iso3}}/{{level}}/"
)

BOUNDARY_SOURCES = ("geoboundaries", "fieldmaps")

WORLD_GEOJSON: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]],
}


@dataclass(frozen=True)
class Boundary:
    iso3: str
    bbox: tuple[float, float, float, float]
    geojson: str
    source: str


def area_sq_km(geometry: dict[str, Any]) -> float:
    """Geodesic area on the WGS84 ellipsoid, holes excluded."""
    # pyproj signs each ring by its winding, and drawn boundaries do not always follow the
    # right-hand rule, so an unoriented hole would add area.
    area, _perimeter = Geod(ellps="WGS84").geometry_area_perimeter(
        shapely.orient_polygons(shape(geometry))
    )
    return abs(area) / 1_000_000


_boundary_cache_lock = threading.Lock()
_boundary_cache: dict[tuple[str, ...], Boundary] = {}


def _bbox_from_geometry(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    coordinates: list[float] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, list)
            and len(node) == 2
            and all(isinstance(value, (int, float)) for value in node)
        ):
            coordinates.extend(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(geometry.get("coordinates", []))
    if not coordinates:
        raise ValueError("No coordinates found in geometry")
    longitudes = coordinates[0::2]
    latitudes = coordinates[1::2]
    return (min(longitudes), min(latitudes), max(longitudes), max(latitudes))


def _fetch_geoboundaries(iso3: str, level: str) -> Boundary:
    url = _GEOBOUNDARIES_URL_TEMPLATE.format(iso3=iso3.upper(), level=level)
    logger.info("Fetching boundary metadata: %s", url)
    metadata_response = requests.get(url, timeout=60)
    metadata_response.raise_for_status()
    payload = metadata_response.json()
    geojson_url = payload.get("gjDownloadURL") or payload.get("simplifiedGeometryGeoJSON")
    if not geojson_url:
        raise RuntimeError(f"geoBoundaries response missing GeoJSON URL for {iso3}")

    logger.info("Downloading boundary geometry: %s", geojson_url)
    geometry_response = requests.get(geojson_url, timeout=180)
    geometry_response.raise_for_status()
    geometry = read_geometry(geometry_response.text)
    bbox = _bbox_from_geometry(geometry)
    return Boundary(
        iso3=iso3.upper(),
        bbox=bbox,
        geojson=json.dumps(geometry),
        source=f"geoBoundaries {_GEOBOUNDARIES_RELEASE} {level}",
    )


def _disputed_areas_of(
    country: list[BaseGeometry], disputed: list[tuple[bytes, str | None]], iso3: str
) -> list[BaseGeometry]:
    parts = shapely.STRtree(shapely.get_parts(country))
    areas = [(shapely.from_wkb(bytes(wkb)), group) for wkb, group in disputed]
    return [area for area, group in areas if group == iso3 or len(parts.query(area, "intersects"))]


def _fetch_fieldmaps(iso3: str, url: str, join_disputed: bool) -> Boundary:
    logger.info("Reading fieldmaps adm0 for %s: %s", iso3.upper(), url)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs")
        rows = conn.execute(
            "SELECT ST_AsWKB(geometry), wld_update FROM read_parquet(?) WHERE iso_3 = ?",
            [url, iso3.upper()],
        ).fetchall()
        disputed = (
            conn.execute(
                "SELECT ST_AsWKB(geometry), iso_3_grp FROM read_parquet(?) "
                "WHERE status_nm = 'Sovereignty unsettled'",
                [url],
            ).fetchall()
            if join_disputed
            else []
        )
    if not rows:
        raise ValueError(f"fieldmaps has no adm0 for {iso3.upper()} in {url}")
    releases = [row[1] for row in rows if row[1] is not None]
    if not releases:
        raise ValueError(f"fieldmaps rows for {iso3.upper()} in {url} have no wld_update date")
    country = list(shapely.from_wkb([bytes(row[0]) for row in rows]))
    outline = shapely.union_all([*country, *_disputed_areas_of(country, disputed, iso3.upper())])
    geometry = json.loads(shapely.to_geojson(outline))
    release = max(releases)
    return Boundary(
        iso3=iso3.upper(),
        bbox=_bbox_from_geometry(geometry),
        geojson=json.dumps(geometry),
        source=f"FieldMaps, U.S. Department of State, OpenStreetMap (adm0 intl, {release})",
    )


def _world_boundary(iso3: str) -> Boundary:
    return Boundary(
        iso3=iso3.upper(),
        bbox=(-180.0, -90.0, 180.0, 90.0),
        geojson=json.dumps(WORLD_GEOJSON),
        source="whole planet (boundary.geom: world)",
    )


def _from_user_geom(iso3: str, geom: str) -> Boundary:
    geometry = read_geometry(geom)
    return Boundary(
        iso3=iso3.upper(),
        bbox=_bbox_from_geometry(geometry),
        geojson=json.dumps(geometry),
        source="user-provided",
    )


def read_geometry(geom: str) -> dict[str, Any]:
    """Inline GeoJSON, or a file or URL GDAL reads, merged into one Polygon or MultiPolygon."""
    text = geom.strip()
    parts = _inline_geojson_parts(text) if text.startswith("{") else _spatial_file_parts(text)
    if not parts:
        raise ValueError(f"boundary holds no geometry: {_preview(text)}")
    not_polygonal = sorted(
        {part.geom_type for part in parts if part.geom_type not in ("Polygon", "MultiPolygon")}
    )
    if not_polygonal:
        raise ValueError(
            f"boundary must hold only polygons; it has {', '.join(not_polygonal)}: {_preview(text)}"
        )
    if len(parts) == 1:
        return json.loads(shapely.to_geojson(parts[0]))
    try:
        merged = shapely.union_all(parts)
    except shapely.errors.GEOSException as error:
        raise ValueError(
            f"boundary features could not be merged, one is invalid ({error}): {_preview(text)}"
        ) from error
    return json.loads(shapely.to_geojson(merged))


def _inline_geojson_parts(text: str) -> list[BaseGeometry]:
    parsed = json.loads(text)
    if parsed.get("type") == "FeatureCollection":
        geometries = [feature.get("geometry") for feature in parsed.get("features", [])]
    elif parsed.get("type") == "Feature":
        geometries = [parsed.get("geometry")]
    else:
        geometries = [parsed]
    return [shape(geometry) for geometry in geometries if geometry]


def _spatial_file_parts(location: str) -> list[BaseGeometry]:
    logger.info("Reading boundary: %s", location)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial")
        if urlparse(location).scheme == "s3":
            conn.execute("INSTALL httpfs; LOAD httpfs")
        layers = layer_names(conn, location)
        if not layers:
            raise ValueError(f"boundary {location} could not be read")
        if len(layers) > 1:
            raise ValueError(
                f"boundary {location} holds {len(layers)} layers {sorted(layers)}; "
                "give a file with one"
            )
        crs = declared_crs(conn, location)
        if crs is None:
            raise ValueError(f"boundary {location} declares no CRS")
        rows = conn.execute(
            f"SELECT ST_AsWKB({geometry_expr(crs)}) "
            f"FROM ST_Read({sql_string(location)}) WHERE geom IS NOT NULL"
        ).fetchall()
    return list(shapely.from_wkb([bytes(row[0]) for row in rows]))


def _preview(text: str) -> str:
    return text if len(text) <= 80 else f"{text[:77]}..."


def _projection_centre(geometry: BaseGeometry) -> Point:
    """The centroid with longitudes read over [0, 360) when that makes the shape narrower,
    so land spread across 180 degrees is centred where it lies."""
    shifted = shapely.transform(
        geometry, lambda coordinates: coordinates + [360.0, 0.0] * (coordinates[:, :1] < 0)
    )
    if _width(shifted) >= _width(geometry):
        return geometry.centroid
    centre = shifted.centroid
    return Point((centre.x + 180.0) % 360.0 - 180.0, centre.y)


def _width(geometry: BaseGeometry) -> float:
    return geometry.bounds[2] - geometry.bounds[0]


def _buffered(boundary: Boundary, buffer_meters: float, simplify_meters: float) -> Boundary:
    # A metre in EPSG:3857 is inflated by 1/cos(latitude); an azimuthal equidistant
    # projection centred on the boundary keeps the buffer true at any latitude.
    geometry = shape(json.loads(boundary.geojson))
    centre = _projection_centre(geometry)
    local = CRS.from_proj4(
        f"+proj=aeqd +lat_0={centre.y} +lon_0={centre.x} +datum=WGS84 +units=m +no_defs"
    )
    to_local = Transformer.from_crs(4326, local, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(local, 4326, always_xy=True).transform
    # GEOS needs tens of GB to buffer a fjord coast at full detail. Each simplify moves edges
    # by up to its tolerance, so two of them stay inside the buffer below half of it.
    local_geometry = transform(to_local, geometry).simplify(simplify_meters)
    widened = local_geometry.buffer(buffer_meters).simplify(simplify_meters)
    # Land on both sides of 180 degrees comes back as rings that jump across the globe;
    # antimeridian splits them at the meridian again.
    buffered = shape(
        antimeridian.fix_shape(mapping(transform(to_wgs84, widened)), fix_winding=True)
    )
    simplified = f", simplified {simplify_meters:g}m" if simplify_meters else ""
    return Boundary(
        iso3=boundary.iso3,
        bbox=buffered.bounds,
        geojson=json.dumps(mapping(buffered)),
        source=f"{boundary.source} (buffered +{buffer_meters:g}m{simplified})",
    )


def resolve_boundary(iso3: str, boundary_config: BoundaryConfig) -> Boundary:
    if not iso3 and not boundary_config.geom:
        raise ValueError("iso3 must be set on the config, or boundary.geom provided")
    if boundary_config.buffer_meters < 0:
        raise ValueError(
            f"boundary.buffer_meters must be >= 0; got {boundary_config.buffer_meters}. "
            "Inward buffers are not supported (would shrink the export area)."
        )
    if boundary_config.simplify_meters < 0 or (
        boundary_config.simplify_meters
        and boundary_config.simplify_meters * 2 >= boundary_config.buffer_meters
    ):
        raise ValueError(
            f"boundary.simplify_meters must be >= 0 and below half of buffer_meters; "
            f"got simplify_meters={boundary_config.simplify_meters}, buffer_meters={boundary_config.buffer_meters}"
        )
    if boundary_config.source not in BOUNDARY_SOURCES:
        raise ValueError(
            f"boundary.source must be one of {list(BOUNDARY_SOURCES)}; "
            f"got {boundary_config.source!r}"
        )
    # Without boundary.geom in the key, two configs with different user geometries for
    # one country share an entry and the second exports the first one's area.
    key = (
        iso3.upper(),
        boundary_config.source,
        _GEOBOUNDARIES_RELEASE,
        boundary_config.geoboundaries_level,
        boundary_config.fieldmaps_url,
        f"disputed{boundary_config.fieldmaps_join_disputed}",
        f"buf{boundary_config.buffer_meters:g}",
        f"simplify{boundary_config.simplify_meters:g}",
        hashlib.sha256((boundary_config.geom or "").encode()).hexdigest(),
    )
    with _boundary_cache_lock:
        cached = _boundary_cache.get(key)
    if cached is not None:
        return cached

    if boundary_config.geom:
        if boundary_config.geom.strip().lower() == "world":
            boundary = _world_boundary(iso3)
        else:
            boundary = _from_user_geom(iso3, boundary_config.geom)
    elif boundary_config.source == "fieldmaps":
        boundary = _fetch_fieldmaps(
            iso3, boundary_config.fieldmaps_url, boundary_config.fieldmaps_join_disputed
        )
    else:
        boundary = _fetch_geoboundaries(iso3, boundary_config.geoboundaries_level)

    if boundary_config.buffer_meters > 0:
        boundary = _buffered(
            boundary, boundary_config.buffer_meters, boundary_config.simplify_meters
        )

    with _boundary_cache_lock:
        _boundary_cache[key] = boundary
    logger.info(
        "Resolved boundary for %s from %s; bbox=%s",
        boundary.iso3,
        boundary.source,
        boundary.bbox,
    )
    return boundary
