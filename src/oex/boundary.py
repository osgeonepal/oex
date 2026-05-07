"""Country boundary resolution: user-supplied geom or geoBoundaries ADM0."""

import json
import threading
from dataclasses import dataclass
from typing import Any

import requests

from oex.config.schema import BoundaryConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)

_GEOBOUNDARIES_TPL = "https://www.geoboundaries.org/api/current/gbOpen/{iso3}/{level}/"


@dataclass(frozen=True)
class Boundary:
    iso3: str
    bbox: tuple[float, float, float, float]
    geojson: str
    source: str


_lock = threading.Lock()
_cache: dict[tuple[str, str, str], Boundary] = {}


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


def resolve_boundary(iso3: str, cfg: BoundaryConfig) -> Boundary:
    if not iso3:
        raise ValueError("iso3 must be set on the config")
    key = (iso3.upper(), cfg.geoboundaries_release, cfg.geoboundaries_level)
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    if cfg.geom:
        boundary = _from_user_geom(iso3, cfg.geom)
    else:
        boundary = _fetch_geoboundaries(iso3, cfg.geoboundaries_release, cfg.geoboundaries_level)

    with _lock:
        _cache[key] = boundary
    logger.info(
        "Resolved boundary for %s from %s; bbox=%s",
        boundary.iso3,
        boundary.source,
        boundary.bbox,
    )
    return boundary
