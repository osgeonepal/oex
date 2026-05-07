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
