"""Thread count and memory limit defaults, read from the cgroup before psutil.

psutil reports the machine, so under a container or unit limit it oversizes the run.
"""

import os
from pathlib import Path

import psutil

_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _cgroup_chain() -> list[Path]:
    """This process's cgroup directory and its ancestors, since a limit on any
    ancestor still binds."""
    try:
        entry = Path("/proc/self/cgroup").read_text().strip().rsplit(":", 1)[-1]
    except OSError:
        return []
    parts = [p for p in entry.split("/") if p]
    return [_CGROUP_ROOT.joinpath(*parts[:depth]) for depth in range(len(parts), -1, -1)]


def _tightest_limit(filename: str, parse) -> float | None:
    """The smallest limit set anywhere in the cgroup chain, or None if uncapped."""
    tightest = None
    for directory in _cgroup_chain():
        try:
            raw = (directory / filename).read_text().strip()
        except OSError:
            continue
        value = parse(raw)
        if value is not None:
            tightest = value if tightest is None else min(tightest, value)
    return tightest


def _parse_cpu_max(raw: str) -> float | None:
    quota, _, period = raw.partition(" ")
    if quota == "max" or not period:
        return None
    return int(quota) / int(period)


def _parse_memory_max(raw: str) -> float | None:
    return None if raw == "max" else float(raw)


def total_memory_gb() -> float:
    """Memory this process may use, in GB. OEX_MEMORY_GB overrides the detection."""
    env = os.environ.get("OEX_MEMORY_GB")
    if env:
        return float(env)
    host_bytes = float(psutil.virtual_memory().total)
    capped = _tightest_limit("memory.max", _parse_memory_max)
    return min(host_bytes, capped if capped is not None else host_bytes) / (1024**3)


def cpu_count() -> int:
    """CPUs this process may use, honouring cgroup quota and scheduler affinity."""
    counts = [psutil.cpu_count(logical=True) or 1]
    if hasattr(os, "sched_getaffinity"):
        counts.append(len(os.sched_getaffinity(0)))
    quota = _tightest_limit("cpu.max", _parse_cpu_max)
    if quota is not None:
        counts.append(int(quota))
    return max(1, min(counts))


def default_thread_count() -> int:
    return max(1, cpu_count() - 1)


def default_memory_limit_gb() -> int:
    return max(1, int(total_memory_gb() * 0.7))


def adaptive_parallel_resources() -> tuple[int, int]:
    """Compute (parallel_workers, memory_gb_per_worker) scaled to available RAM.

    Always returns 1 worker. DuckDB's intra-query pipeline engine parallelises
    every operation (joins, scans, aggregations) across all CPU cores within one
    session. Concurrent sessions split the RAM budget with zero cross-session
    coordination and OOM-kill each other on large countries (BRA, IND, CHN).

    Memory: 60% of available RAM, DuckDB's recommended safe fraction for a single
    session. Leaves headroom for GDAL write allocations, string heaps, and spatial
    index structures that bypass the buffer manager.
    """
    total_gb = total_memory_gb()
    memory_gb = max(1, int(total_gb * 0.60))
    return 1, memory_gb
