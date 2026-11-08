"""Pre-run sanity checks. Fail loud before doing any expensive work."""

import os
import tempfile
from pathlib import Path

from oex.config.schema import RootConfig
from oex.logging_setup import get_logger
from oex.writers import tile_builder

logger = get_logger(__name__)


class PreflightError(RuntimeError):
    """A required precondition is not satisfied."""


def check_writable_paths(cfg: RootConfig) -> None:
    """Probe every directory the run writes to with a temp file, so a read-only filesystem
    fails before PBFs are downloaded or quackosm runs."""
    candidates: list[Path] = [Path(cfg.output.dir)]

    osm_config = cfg.source.get("osm")
    if osm_config is not None and getattr(osm_config, "enabled", True):
        candidates.append(Path(osm_config.cache_dir))

    pcodes_config = cfg.source.get("pcodes")
    if pcodes_config is not None and getattr(pcodes_config, "enabled", False):
        candidates.append(Path(pcodes_config.cache_dir))

    duckdb_temp = getattr(cfg.duckdb, "temp_dir", None)
    if duckdb_temp:
        candidates.append(Path(duckdb_temp))

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        _verify_writable(resolved)


def _verify_writable(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreflightError(
            f"cannot create {path}: {exc}. "
            "Check the parent filesystem is writable and the path is correct."
        ) from exc

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path,
            prefix=".oex_preflight.",
            delete=False,
            encoding="utf-8",
        ) as probe_file:
            probe_file.write("ok")
            probe = Path(probe_file.name)
    except OSError as exc:
        raise PreflightError(
            f"cannot write inside {path}: {exc}. Filesystem may be read-only or quota-full."
        ) from exc

    renamed = probe.with_name(probe.name + ".renamed")
    try:
        os.replace(probe, renamed)
    except OSError as exc:
        probe.unlink()
        raise PreflightError(f"cannot rename inside {path}: {exc}") from exc
    renamed.unlink()

    logger.info("preflight: %s is writable", path)


def check_tile_builder(cfg: RootConfig) -> None:
    """Tiles are built after every layer is exported, so a missing tippecanoe would
    otherwise surface hours into a run."""
    if not cfg.output.pmtiles.enabled:
        return
    logger.info("preflight: tiles will be built with %s", tile_builder())
