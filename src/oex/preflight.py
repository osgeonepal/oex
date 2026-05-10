"""Pre-run sanity checks. Fail loud before doing any expensive work."""

import os
import tempfile
from pathlib import Path

from oex.config.schema import RootConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)


class PreflightError(RuntimeError):
    """A required precondition is not satisfied."""


def check_writable_paths(cfg: RootConfig) -> None:
    """Verify every directory the run needs to write to is writable.

    Catches read-only filesystems and permission errors before downloading
    PBFs or running quackosm. Tests by creating, writing, then deleting a
    tiny temp file in each candidate path.
    """
    candidates: list[Path] = [Path(cfg.output.dir)]

    osm_cfg = cfg.source.get("osm")
    if osm_cfg is not None and getattr(osm_cfg, "enabled", True):
        candidates.append(Path(osm_cfg.cache_dir))

    pcodes_cfg = cfg.source.get("pcodes")
    if pcodes_cfg is not None and getattr(pcodes_cfg, "enabled", False):
        candidates.append(Path(pcodes_cfg.cache_dir))

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
        ) as fh:
            fh.write("ok")
            probe = Path(fh.name)
    except OSError as exc:
        raise PreflightError(
            f"cannot write inside {path}: {exc}. Filesystem may be read-only or quota-full."
        ) from exc

    try:
        os.replace(probe, probe)
    except OSError as exc:
        probe.unlink(missing_ok=True)
        raise PreflightError(f"cannot rename inside {path}: {exc}") from exc
    finally:
        probe.unlink(missing_ok=True)

    logger.info("preflight: %s is writable", path)
