"""P-code tagging via fieldmaps.io edge-matched humanitarian admin polygons."""

from typing import Any

from oex.config.schema import PcodesSourceConfig
from oex.pcodes.cache import (
    PcodeCacheEntry,
    PcodeCacheError,
    ensure_admin_parquets,
)
from oex.pcodes.tagger import PcodeTagReport, tag_table


def resolve_pcodes_config(source: dict[str, Any]) -> PcodesSourceConfig:
    """Pull the pcodes config out of `cfg.source`, normalising dict vs dataclass.

    The `source` field on RootConfig is `dict[str, Any]`, so OmegaConf does
    not preserve the dataclass type when user YAML overrides values. We
    accept both shapes and return a typed `PcodesSourceConfig`.
    """
    raw = source.get("pcodes")
    if raw is None:
        return PcodesSourceConfig()
    if isinstance(raw, PcodesSourceConfig):
        return raw
    if isinstance(raw, dict):
        return PcodesSourceConfig(**raw)
    raise TypeError(
        f"source.pcodes has unexpected type {type(raw).__name__}; "
        "expected PcodesSourceConfig or dict"
    )


__all__ = [
    "PcodeCacheEntry",
    "PcodeCacheError",
    "PcodeTagReport",
    "ensure_admin_parquets",
    "resolve_pcodes_config",
    "tag_table",
]
