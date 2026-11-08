from datetime import UTC, datetime


def parse_utc(text: str) -> datetime:
    """An ISO-8601 value in UTC; one without an offset is UTC, so the host timezone never applies."""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
