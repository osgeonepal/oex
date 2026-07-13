"""The OSM country parquet is keyed by everything that shapes its contents."""

import json

from oex.config.schema import BoundaryConfig, CategoryConfig, CategoryOsm, RootConfig
from oex.osm.runner import _parquet_fingerprint

_KATHMANDU = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [
            [[85.30, 27.68], [85.36, 27.68], [85.36, 27.73], [85.30, 27.73], [85.30, 27.68]]
        ],
    }
)
_WIDER = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [
            [[85.20, 27.60], [85.50, 27.60], [85.50, 27.80], [85.20, 27.80], [85.20, 27.60]]
        ],
    }
)


def _cfg(geom: str, filters: dict) -> RootConfig:
    return RootConfig(
        iso3="NPL",
        key="demo",
        boundary=BoundaryConfig(geom=geom),
        categories=[
            CategoryConfig(name="Buildings", osm=CategoryOsm(enabled=True, filter=filters))
        ],
    )


def test_a_wider_boundary_builds_a_fresh_parquet() -> None:
    """The cached parquet is clipped, so reusing it after widening the boundary
    would silently drop every feature in the newly added area."""
    narrow = _parquet_fingerprint(_cfg(_KATHMANDU, {"building": True}), clip=True)
    wide = _parquet_fingerprint(_cfg(_WIDER, {"building": True}), clip=True)
    assert narrow != wide


def test_new_category_tags_build_a_fresh_parquet() -> None:
    """The parquet holds only the union of the category tag filters, so adding a
    category with a new tag key must not reuse a parquet built without it."""
    before = _parquet_fingerprint(_cfg(_KATHMANDU, {"building": True}), clip=True)
    after = _parquet_fingerprint(_cfg(_KATHMANDU, {"building": True, "highway": True}), clip=True)
    assert before != after


def test_same_inputs_reuse_the_same_parquet() -> None:
    cfg = _cfg(_KATHMANDU, {"building": True})
    assert _parquet_fingerprint(cfg, clip=True) == _parquet_fingerprint(cfg, clip=True)


def test_clipping_off_ignores_the_boundary() -> None:
    """Without a clip the parquet covers the whole extract, so the boundary cannot change it."""
    a = _parquet_fingerprint(_cfg(_KATHMANDU, {"building": True}), clip=False)
    b = _parquet_fingerprint(_cfg(_WIDER, {"building": True}), clip=False)
    assert a == b


def test_boundary_cache_does_not_confuse_two_geometries_for_one_country() -> None:
    """resolve_boundary memoises per country; the user geometry must be part of that key."""
    from oex.boundary import resolve_boundary

    narrow = resolve_boundary("NPL", BoundaryConfig(geom=_KATHMANDU))
    wide = resolve_boundary("NPL", BoundaryConfig(geom=_WIDER))
    assert narrow.bbox != wide.bbox
    assert narrow.bbox == (85.30, 27.68, 85.36, 27.73)
