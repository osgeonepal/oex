"""Geofabrik country-PBF URL lookup via the public `index-v1.json`."""

import threading
from dataclasses import dataclass
from typing import Any

import pycountry
import requests

from oex.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_INDEX_URL = "https://download.geofabrik.de/index-v1.json"

# Country-level extracts sit directly under a continent in Geofabrik's tree;
# anything deeper (e.g. a US state) shares a country's alpha2 and would
# otherwise match by accident.
_CONTINENT_PARENTS = frozenset(
    {
        "africa",
        "antarctica",
        "asia",
        "australia-oceania",
        "central-america",
        "europe",
        "north-america",
        "russia",
        "south-america",
    }
)

_index_lock = threading.Lock()
_index_cache: dict[str, list[dict[str, Any]]] = {}


@dataclass(frozen=True)
class GeofabrikExtract:
    iso3: str
    iso2: str
    geofabrik_id: str
    name: str
    pbf_url: str
    md5_url: str


class GeofabrikLookupError(LookupError):
    """Raised when the index does not contain a country-level extract."""


def _iso3_to_iso2(iso3: str) -> str:
    country = pycountry.countries.get(alpha_3=iso3.upper())
    if country is None:
        raise GeofabrikLookupError(f"Unknown ISO3 country code: {iso3!r}")
    return str(country.alpha_2)


def _load_index(index_url: str) -> list[dict[str, Any]]:
    with _index_lock:
        cached = _index_cache.get(index_url)
    if cached is not None:
        return cached

    logger.info("Fetching Geofabrik index: %s", index_url)
    resp = requests.get(index_url, timeout=120)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("type") != "FeatureCollection":
        raise RuntimeError(f"Unexpected Geofabrik index format: {payload.get('type')!r}")
    features: list[dict[str, Any]] = [f["properties"] for f in payload["features"]]
    with _index_lock:
        _index_cache[index_url] = features
    return features


def lookup_country(iso3: str, *, index_url: str = DEFAULT_INDEX_URL) -> GeofabrikExtract:
    iso2 = _iso3_to_iso2(iso3)
    features = _load_index(index_url)

    candidates: list[dict[str, Any]] = []
    for props in features:
        codes = props.get("iso3166-1:alpha2") or []
        if iso2 not in codes:
            continue
        parent = props.get("parent")
        if parent in _CONTINENT_PARENTS or parent is None:
            candidates.append(props)

    if not candidates:
        raise GeofabrikLookupError(
            f"No Geofabrik country-level extract found for ISO3 {iso3!r} "
            f"(alpha2 {iso2!r}). Geofabrik may not publish this country directly."
        )

    # Prefer an unambiguous country-level entry whose alpha2 list is exactly [iso2].
    candidates.sort(key=lambda p: (p.get("iso3166-1:alpha2") != [iso2], p.get("id", "")))
    chosen = candidates[0]

    pbf_url = chosen.get("urls", {}).get("pbf")
    if not pbf_url:
        raise GeofabrikLookupError(f"Geofabrik entry {chosen.get('id')!r} has no PBF URL")

    return GeofabrikExtract(
        iso3=iso3.upper(),
        iso2=iso2,
        geofabrik_id=str(chosen.get("id", "")),
        name=str(chosen.get("name", "")),
        pbf_url=str(pbf_url),
        md5_url=f"{pbf_url}.md5",
    )
