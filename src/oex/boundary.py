"""Country boundary resolution: user-supplied geom or geoBoundaries ADM0."""

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

import antimeridian
import requests
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from oex.config.schema import BoundaryConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)

_GEOBOUNDARIES_RELEASE = "gbOpen"
_GEOBOUNDARIES_URL_TEMPLATE = (
    f"https://www.geoboundaries.org/api/current/{_GEOBOUNDARIES_RELEASE}/{{iso3}}/{{level}}/"
)

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
_boundary_cache: dict[tuple[str, str, str, str, str, str], Boundary] = {}


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


def _featurecollection_to_geometry(feature_collection: dict[str, Any]) -> dict[str, Any]:
    # ST_GeomFromGeoJSON accepts a single geometry or a GeometryCollection,
    # not a FeatureCollection.
    features = feature_collection.get("features", [])
    geometries = [feature["geometry"] for feature in features if feature.get("geometry")]
    if len(geometries) == 1:
        return geometries[0]
    return {"type": "GeometryCollection", "geometries": geometries}


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
    parsed = geometry_response.json()
    geometry = (
        _featurecollection_to_geometry(parsed)
        if parsed.get("type") == "FeatureCollection"
        else parsed
    )
    bbox = _bbox_from_geometry(geometry)
    return Boundary(
        iso3=iso3.upper(),
        bbox=bbox,
        geojson=json.dumps(geometry),
        source=f"geoBoundaries {_GEOBOUNDARIES_RELEASE} {level}",
    )


def _world_boundary(iso3: str) -> Boundary:
    return Boundary(
        iso3=iso3.upper(),
        bbox=(-180.0, -90.0, 180.0, 90.0),
        geojson=json.dumps(WORLD_GEOJSON),
        source="whole planet (boundary.geom: world)",
    )


def _from_user_geom(iso3: str, geometry_json: str) -> Boundary:
    parsed = json.loads(geometry_json)
    geometry = (
        _featurecollection_to_geometry(parsed)
        if parsed.get("type") == "FeatureCollection"
        else parsed
    )
    bbox = _bbox_from_geometry(geometry)
    return Boundary(
        iso3=iso3.upper(),
        bbox=bbox,
        geojson=json.dumps(geometry),
        source="user-provided",
    )


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
    # Without boundary.geom in the key, two configs with different user geometries for
    # one country share an entry and the second exports the first one's area.
    key = (
        iso3.upper(),
        _GEOBOUNDARIES_RELEASE,
        boundary_config.geoboundaries_level,
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
