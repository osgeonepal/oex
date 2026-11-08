from collections.abc import Callable
from datetime import UTC, datetime

from oex.timestamps import parse_utc


def test_a_value_without_an_offset_is_read_as_utc_on_a_host_west_of_utc(
    set_timezone: Callable[[str], None],
) -> None:
    set_timezone("America/Los_Angeles")
    assert parse_utc("2026-06-17") == datetime(2026, 6, 17, tzinfo=UTC)


def test_a_value_with_an_offset_is_converted_to_utc() -> None:
    parsed = parse_utc("2026-06-17T05:45:00+05:45")
    assert parsed == datetime(2026, 6, 17, tzinfo=UTC)
    assert parsed.tzinfo is UTC
