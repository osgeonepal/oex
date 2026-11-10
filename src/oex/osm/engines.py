"""The OSM source engines, named once so config validation and dispatch agree."""

import functools
import json
from importlib import resources
from typing import Any

ENGINE_NAMES = ("geofabrik", "planet", "postpass", "rawdata")

# wiki.openstreetmap.org/wiki/Lifecycle_prefix for features that existed, plus `damaged:`,
# which responses map with.
_LIFECYCLE_PREFIXES = (
    "abandoned",
    "damaged",
    "demolished",
    "destroyed",
    "disused",
    "razed",
    "removed",
    "ruins",
    "was",
)


def lifecycle_area_rules() -> dict[str, Any]:
    """quackosm's area rules repeated under each lifecycle prefix, plus `emergency:helipad`
    as an always-area key."""
    rules = _quackosm_area_rules()
    # A key already carrying a prefix, such as `demolished:building`, is not prefixed again.
    plain = {
        kind: [key for key in rules[kind] if key.split(":")[0] not in _LIFECYCLE_PREFIXES]
        for kind in ("all", "allowlist", "denylist")
    }
    return {
        "all": [f"{prefix}:{key}" for prefix in _LIFECYCLE_PREFIXES for key in plain["all"]]
        + ["emergency:helipad"],
        "allowlist": {
            f"{prefix}:{key}": rules["allowlist"][key]
            for prefix in _LIFECYCLE_PREFIXES
            for key in plain["allowlist"]
        },
        "denylist": {
            f"{prefix}:{key}": rules["denylist"][key]
            for prefix in _LIFECYCLE_PREFIXES
            for key in plain["denylist"]
        },
    }


def area_rules() -> dict[str, Any]:
    """quackosm's area rules with the lifecycle ones added, and without `junction`, since a
    roundabout is a closed road (wiki.openstreetmap.org/wiki/Tag:junction=roundabout)."""
    rules = _quackosm_area_rules()
    extra = lifecycle_area_rules()
    return {
        "all": sorted({*rules["all"], *extra["all"]} - {"junction"}),
        "allowlist": {**rules["allowlist"], **extra["allowlist"]},
        "denylist": {**rules["denylist"], **extra["denylist"]},
    }


@functools.cache
def _quackosm_area_rules() -> dict[str, Any]:
    return json.loads(
        (resources.files("quackosm") / "osm_way_polygon_features.json").read_text("utf-8")
    )
