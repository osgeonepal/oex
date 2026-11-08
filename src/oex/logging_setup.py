"""Idempotent root logger setup."""

import logging
import os

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_configured = False


def setup_logging(level: str | None = None, log_format: str | None = None) -> None:
    global _configured
    if _configured:
        # get_logger configures at import time, so an explicit level from the CLI must still apply.
        if level:
            logging.getLogger().setLevel(level.upper())
        return

    resolved_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    resolved_format = log_format or os.environ.get("LOG_FORMAT", _DEFAULT_FORMAT)

    logging.basicConfig(level=resolved_level, format=resolved_format)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("s3fs").setLevel(logging.WARNING)
    logging.getLogger("fsspec").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
