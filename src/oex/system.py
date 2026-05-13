"""Defaults for thread count and DuckDB memory limit, derived from psutil."""

import os

import psutil


def total_memory_gb() -> float:
    """Return effective memory in GB.

    OEX_MEMORY_GB env var overrides psutil (use this inside Docker where
    --memory sets the container limit but psutil reads the host RAM).
    """
    env = os.environ.get("OEX_MEMORY_GB")
    if env:
        return float(env)
    return psutil.virtual_memory().total / (1024**3)


def cpu_count() -> int:
    return psutil.cpu_count(logical=True) or 1


def default_thread_count() -> int:
    return max(1, cpu_count() - 1)


def default_memory_limit_gb() -> int:
    return max(1, int(total_memory_gb() * 0.7))


def adaptive_parallel_resources() -> tuple[int, int]:
    """Compute (parallel_workers, memory_gb_per_worker) scaled to total system RAM.

    Always returns 1 worker. DuckDB's intra-query pipeline engine parallelises
    every operation (joins, scans, aggregations) across all CPU cores within one
    session. Concurrent sessions split the RAM budget with zero cross-session
    coordination and OOM-kill each other on large countries (BRA, IND, CHN).

    Memory: 60% of total RAM, DuckDB's recommended safe fraction for a single
    session. Leaves headroom for GDAL write allocations, string heaps, and spatial
    index structures that bypass the buffer manager.

    Uses total memory (cgroup-aware on Linux >= 5.0, so a Docker container with
    --memory set reports the container limit here).
    """
    total_gb = total_memory_gb()
    memory_gb = max(1, int(total_gb * 0.60))
    return 1, memory_gb
