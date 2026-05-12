"""Defaults for thread count and DuckDB memory limit, derived from psutil."""

import psutil


def total_memory_gb() -> float:
    return psutil.virtual_memory().total / (1024**3)


def cpu_count() -> int:
    return psutil.cpu_count(logical=True) or 1


def default_thread_count() -> int:
    return max(1, cpu_count() - 1)


def default_memory_limit_gb() -> int:
    return max(1, int(total_memory_gb() * 0.7))


def adaptive_parallel_resources(
    target_mem_per_worker_gb: float = 12.0,
    os_reserve_fraction: float = 0.20,
) -> tuple[int, int]:
    """Compute (parallel_workers, memory_gb_per_worker) scaled to currently available RAM.

    Uses available memory (free + reclaimable buffers), not total, so it accounts for
    other processes already running. Inside a Docker container with --memory set, the
    kernel reports the container limit as both total and available (cgroup-aware since
    Linux 5.0), so this scales correctly to the container allocation.
    """
    avail_gb = psutil.virtual_memory().available / (1024**3)
    usable_gb = max(1.0, avail_gb * (1.0 - os_reserve_fraction))
    workers_by_mem = max(1, int(usable_gb / target_mem_per_worker_gb))
    workers_by_cpu = max(1, cpu_count() // 2)
    parallel_workers = min(workers_by_mem, workers_by_cpu)
    mem_per_worker_gb = max(1, int(usable_gb // parallel_workers))
    return parallel_workers, mem_per_worker_gb
