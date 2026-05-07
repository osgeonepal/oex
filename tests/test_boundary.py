"""Boundary resolution from user-supplied geometry."""

import json

from oex.boundary import resolve_boundary
from oex.config.schema import BoundaryConfig


def test_resolve_from_user_geom_polygon() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[80, 27], [85, 27], [85, 30], [80, 30], [80, 27]]],
                },
            }
        ],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc))
    boundary = resolve_boundary("NPL", cfg)
    assert boundary.iso3 == "NPL"
    assert boundary.bbox == (80, 27, 85, 30)
    assert boundary.source == "user-provided"


def test_resolve_caches_repeat_calls() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]],
                },
            }
        ],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc))
    a = resolve_boundary("ABC", cfg)
    b = resolve_boundary("ABC", cfg)
    assert a is b
