"""Names derived from config values, shared so the exporter and the publisher agree."""

import re


def slugify(value: str) -> str:
    """Config text to a filename and HDX-safe token."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).lower().strip("_")
