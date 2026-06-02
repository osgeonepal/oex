"""Country boundary resolution: user-supplied geom or geoBoundaries ADM0."""

import json
import threading
from dataclasses import dataclass
from typing import Any

import requests
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from oex.config.schema import BoundaryConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)

_GEOBOUNDARIES_TPL = "https://www.geoboundaries.org/api/current/gbOpen/{iso3}/{level}/"

WORLD_GEOJSON: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]],
}

_to_3857 = Transformer.from_crs(4326, 3857, always_xy=True).transform
_to_4326 = Transformer.from_crs(3857, 4326, always_xy=True).transform


@dataclass(frozen=True)
class Boundary:
    iso3: str
    bbox: tuple[float, float, float, float]
    geojson: str
    source: str


_lock = threading.Lock()
_cache: dict[tuple[str, str, str, str], Boundary] = {}


def _bbox_from_geometry(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    coords: list[float] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, list)
            and len(node) == 2
            and all(isinstance(v, (int, float)) for v in node)
        ):
            coords.extend(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(geometry.get("coordinates", []))
    if not coords:
        raise ValueError("No coordinates found in geometry")
    xs = coords[0::2]
    ys = coords[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def _featurecollection_to_geometry(fc: dict[str, Any]) -> dict[str, Any]:
    # ST_GeomFromGeoJSON accepts a single geometry or a GeometryCollection,
    # not a FeatureCollection.
    features = fc.get("features", [])
    geometries = [f["geometry"] for f in features if f.get("geometry")]
    if len(geometries) == 1:
        return geometries[0]
    return {"type": "GeometryCollection", "geometries": geometries}


def _fetch_geoboundaries(iso3: str, release: str, level: str) -> Boundary:
    url = _GEOBOUNDARIES_TPL.format(iso3=iso3.upper(), level=level)
    logger.info("Fetching boundary metadata: %s", url)
    meta = requests.get(url, timeout=60)
    meta.raise_for_status()
    payload = meta.json()
    geojson_url = payload.get("gjDownloadURL") or payload.get("simplifiedGeometryGeoJSON")
    if not geojson_url:
        raise RuntimeError(f"geoBoundaries response missing GeoJSON URL for {iso3}")

    logger.info("Downloading boundary geometry: %s", geojson_url)
    resp = requests.get(geojson_url, timeout=180)
    resp.raise_for_status()
    fc = resp.json()
    geometry = _featurecollection_to_geometry(fc) if fc.get("type") == "FeatureCollection" else fc
    bbox = _bbox_from_geometry(geometry)
    return Boundary(
        iso3=iso3.upper(),
        bbox=bbox,
        geojson=json.dumps(geometry),
        source=f"geoBoundaries {release} {level}",
    )


def _world_boundary(iso3: str) -> Boundary:
    return Boundary(
        iso3=iso3.upper(),
        bbox=(-180.0, -90.0, 180.0, 90.0),
        geojson=json.dumps(WORLD_GEOJSON),
        source="whole planet (boundary.geom: world)",
    )


def _from_user_geom(iso3: str, geom_str: str) -> Boundary:
    fc = json.loads(geom_str)
    geometry = _featurecollection_to_geometry(fc) if fc.get("type") == "FeatureCollection" else fc
    bbox = _bbox_from_geometry(geometry)
    return Boundary(
        iso3=iso3.upper(),
        bbox=bbox,
        geojson=json.dumps(geometry),
        source="user-provided",
    )


def _buffered(boundary: Boundary, buffer_meters: float) -> Boundary:
    # shapely.buffer operates in the geometry's own CRS units. The boundary is
    # in EPSG:4326 (degrees), so we project to EPSG:3857 to make the metre
    # value mean what the config says it means before reprojecting back.
    geom = shape(json.loads(boundary.geojson))
    projected = transform(_to_3857, geom)
    buffered = projected.buffer(buffer_meters)
    back = transform(_to_4326, buffered)
    return Boundary(
        iso3=boundary.iso3,
        bbox=back.bounds,
        geojson=json.dumps(mapping(back)),
        source=f"{boundary.source} (buffered +{buffer_meters:g}m)",
    )


def resolve_boundary(iso3: str, cfg: BoundaryConfig) -> Boundary:
    if not iso3:
        raise ValueError("iso3 must be set on the config")
    if cfg.buffer_meters < 0:
        raise ValueError(
            f"boundary.buffer_meters must be >= 0; got {cfg.buffer_meters}. "
            "Inward buffers are not supported (would shrink the export area)."
        )
    key = (
        iso3.upper(),
        cfg.geoboundaries_release,
        cfg.geoboundaries_level,
        f"buf{cfg.buffer_meters:g}",
    )
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    if cfg.geom:
        if cfg.geom.strip().lower() == "world":
            boundary = _world_boundary(iso3)
        else:
            boundary = _from_user_geom(iso3, cfg.geom)
    else:
        boundary = _fetch_geoboundaries(iso3, cfg.geoboundaries_release, cfg.geoboundaries_level)

    if cfg.buffer_meters > 0:
        boundary = _buffered(boundary, cfg.buffer_meters)

    with _lock:
        _cache[key] = boundary
    logger.info(
        "Resolved boundary for %s from %s; bbox=%s",
        boundary.iso3,
        boundary.source,
        boundary.bbox,
    )
    return boundary
