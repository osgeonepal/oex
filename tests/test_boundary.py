"""Boundary resolution from user-supplied geometry."""

import json

import pytest

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


def test_resolve_world_shorthand_returns_full_globe() -> None:
    cfg = BoundaryConfig(geom="world")
    boundary = resolve_boundary("WLD", cfg)
    assert boundary.iso3 == "WLD"
    assert boundary.bbox == (-180.0, -90.0, 180.0, 90.0)
    assert "whole planet" in boundary.source


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


def test_buffer_widens_bbox_and_changes_cache_key() -> None:
    fc = {
        "type": "Polygon",
        # 1 deg square at the equator: ~111 km wide.
        "coordinates": [[[10, 0], [11, 0], [11, 1], [10, 1], [10, 0]]],
    }
    base_cfg = BoundaryConfig(geom=json.dumps(fc))
    base = resolve_boundary("BUF", base_cfg)

    buffered_cfg = BoundaryConfig(geom=json.dumps(fc), buffer_meters=1000.0)
    buffered = resolve_boundary("BUF", buffered_cfg)

    # Buffered bbox should be a strict superset.
    assert buffered.bbox[0] < base.bbox[0]
    assert buffered.bbox[1] < base.bbox[1]
    assert buffered.bbox[2] > base.bbox[2]
    assert buffered.bbox[3] > base.bbox[3]

    # 1 km buffer at the equator works out to about 0.009 deg in both axes.
    # Allow generous slack so we just assert order of magnitude is right.
    dx = buffered.bbox[2] - base.bbox[2]
    dy = buffered.bbox[3] - base.bbox[3]
    assert 0.005 < dx < 0.02
    assert 0.005 < dy < 0.02

    # Source string carries the buffer annotation, so users see it in README.
    assert "buffered" in buffered.source
    assert "+1000m" in buffered.source

    # Different buffers must produce distinct cached boundaries.
    assert buffered is not base


def test_zero_buffer_is_identity() -> None:
    fc = {
        "type": "Polygon",
        "coordinates": [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc), buffer_meters=0.0)
    boundary = resolve_boundary("ZRO", cfg)
    assert boundary.bbox == (20.0, 20.0, 21.0, 21.0)
    assert "buffered" not in boundary.source


def test_negative_buffer_meters_raises() -> None:
    fc = {
        "type": "Polygon",
        "coordinates": [[[30, 30], [31, 30], [31, 31], [30, 31], [30, 30]]],
    }
    cfg = BoundaryConfig(geom=json.dumps(fc), buffer_meters=-100.0)
    with pytest.raises(ValueError, match="buffer_meters must be >= 0"):
        resolve_boundary("NEG", cfg)
