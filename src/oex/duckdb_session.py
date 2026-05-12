import os
from pathlib import Path

import duckdb

from oex.logging_setup import get_logger
from oex.system import default_memory_limit_gb, default_thread_count

logger = get_logger(__name__)


def connect(
    *,
    path: str | os.PathLike[str],
    threads: int | None = None,
    memory_gb: int | None = None,
    s3_region: str = "us-west-2",
    temp_dir: str | os.PathLike[str] | None = None,
    http_retries: int = 8,
    http_retry_wait_ms: int = 500,
    http_retry_backoff: float = 2.0,
    http_timeout_ms: int = 120_000,
) -> duckdb.DuckDBPyConnection:
    resolved_threads = max(2, threads or default_thread_count())
    resolved_memory = max(1, memory_gb or default_memory_limit_gb())
    resolved_temp = Path(temp_dir or "/var/tmp/duckdb_temp")
    resolved_temp.mkdir(parents=True, exist_ok=True)

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path))
    for stmt in (
        "INSTALL spatial",
        "LOAD spatial",
        "INSTALL httpfs",
        "LOAD httpfs",
        f"SET s3_region='{s3_region}'",
        f"PRAGMA memory_limit='{resolved_memory}GB'",
        f"PRAGMA threads={resolved_threads}",
        "SET preserve_insertion_order=false",
        f"PRAGMA temp_directory='{resolved_temp}'",
        "SET allocator_background_threads=true",
        f"SET http_retries={http_retries}",
        f"SET http_retry_wait_ms={http_retry_wait_ms}",
        f"SET http_retry_backoff={http_retry_backoff}",
        f"SET http_timeout={http_timeout_ms}",
        "SET http_keep_alive=true",
    ):
        conn.execute(stmt)

    logger.debug(
        "DuckDB session ready: path=%s threads=%d memory=%dGB temp=%s",
        db_path,
        resolved_threads,
        resolved_memory,
        resolved_temp,
    )
    return conn
