"""Layered YAML config: bundled defaults < user YAML < dotlist overrides."""

import os
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any, cast

from omegaconf import DictConfig, ListConfig, OmegaConf

from oex.config.schema import RootConfig


class ConfigError(ValueError):
    """Raised when a configuration is malformed."""


def _load_yaml(source: str | os.PathLike[str]) -> DictConfig:
    text: str
    if isinstance(source, (str, os.PathLike)) and Path(source).exists():
        text = Path(source).read_text(encoding="utf-8")
    elif isinstance(source, str):
        text = source
    else:
        raise ConfigError(f"Cannot load config from {source!r}")
    cfg = OmegaConf.create(text)
    if not isinstance(cfg, DictConfig):
        raise ConfigError("Top-level YAML must be a mapping")
    return cfg


def _load_defaults() -> DictConfig:
    pkg = resources.files("oex.defaults")
    text = (pkg / "base.yaml").read_text(encoding="utf-8")
    cfg = OmegaConf.create(text)
    if not isinstance(cfg, DictConfig):
        raise ConfigError("base.yaml is malformed")
    return cfg


def _load_categories_file(path: str | os.PathLike[str]) -> ListConfig:
    raw = _load_yaml(path) if Path(str(path)).exists() else _load_yaml(str(path))
    if "categories" in raw and isinstance(raw.categories, ListConfig):
        return raw.categories
    raise ConfigError(f"categories_file {path!r} must contain a top-level `categories:` list")


def load_config(
    user_config: str | os.PathLike[str] | None = None,
    overrides: list[str] | None = None,
) -> RootConfig:
    """Build a RootConfig. categories precedence: defaults < categories_file < inline `categories:`."""
    # Merge plain (untyped) configs first so user YAML can replace the
    # categories list wholesale without tripping the structured-list type check.
    merged: DictConfig = _load_defaults()

    if user_config is not None:
        user = _load_yaml(user_config)

        if "categories_file" in user and user.categories_file:
            merged.categories = _load_categories_file(str(user.categories_file))

        if "categories" in user and isinstance(user.categories, ListConfig):
            merged.categories = user.categories
            del user["categories"]

        merged = cast(DictConfig, OmegaConf.merge(merged, user))

    if overrides:
        merged = cast(DictConfig, OmegaConf.merge(merged, OmegaConf.from_dotlist(overrides)))

    OmegaConf.resolve(merged)

    schema = OmegaConf.structured(RootConfig)
    typed = cast(DictConfig, OmegaConf.merge(schema, merged))
    container: Any = OmegaConf.to_object(typed)
    if not isinstance(container, RootConfig):
        raise ConfigError("Merged config did not resolve to RootConfig")
    return container


def apply_overrides(cfg: RootConfig, overrides: dict[str, Any]) -> RootConfig:
    """Apply a dict of dotted overrides to an already-loaded config."""
    structured: DictConfig = cast(DictConfig, OmegaConf.structured(cfg))
    dotlist = [f"{k}={v}" for k, v in overrides.items() if v is not None]
    if dotlist:
        structured = cast(DictConfig, OmegaConf.merge(structured, OmegaConf.from_dotlist(dotlist)))
    OmegaConf.resolve(structured)
    container: Any = OmegaConf.to_object(structured)
    if not isinstance(container, RootConfig):
        raise ConfigError("Override merge did not resolve to RootConfig")
    return container


def select_categories(cfg: RootConfig, theme: str | None) -> RootConfig:
    """Restrict the config to a single category whose slugified name matches `theme`."""
    if theme is None:
        return cfg
    needle = theme.strip().lower().replace("-", "_").replace(" ", "_")
    kept = [c for c in cfg.categories if c.name.lower().replace(" ", "_") == needle]
    if not kept:
        available = ", ".join(c.name for c in cfg.categories) or "<none>"
        raise ConfigError(f"Theme {theme!r} not found. Available: {available}")
    new_cfg = OmegaConf.to_object(OmegaConf.structured(cfg))
    if not isinstance(new_cfg, RootConfig):
        raise ConfigError("select_categories failed to round-trip RootConfig")
    new_cfg.categories = kept
    return new_cfg


def iter_configs(configs_dir: str | os.PathLike[str]) -> Iterator[Path]:
    root = Path(configs_dir)
    if not root.is_dir():
        raise ConfigError(f"Not a directory: {root}")
    for path in sorted(root.glob("*.yaml")):
        yield path
    for path in sorted(root.glob("*.yml")):
        yield path
