from datetime import datetime
from pathlib import Path

import pytest

from oex.config.loader import ConfigError, load_config
from oex.hdx_publisher import PublishContext, apply_time_period

SNAPSHOT = datetime(2026, 10, 4)


class _Dataset:
    def set_time_period(self, start, end=None) -> None:
        self.period = (start, end)


def _period(start: str | None, **ctx_fields) -> tuple:
    dataset = _Dataset()
    apply_time_period(dataset, PublishContext("osm", SNAPSHOT, **ctx_fields), start)
    return dataset.period


def test_a_configured_start_runs_the_period_to_the_snapshot() -> None:
    assert _period("2026-08-26") == (datetime(2026, 8, 26), SNAPSHOT)


def test_without_a_start_the_period_is_the_snapshot_day() -> None:
    assert _period(None) == (SNAPSHOT, None)


def test_feature_dates_take_precedence_over_a_configured_start() -> None:
    first, last = datetime(2026, 9, 1), datetime(2026, 9, 30)
    assert _period("2026-08-26", temporal_min=first, temporal_max=last) == (first, last)


def test_a_start_after_the_snapshot_is_refused() -> None:
    """HDX accepts a reversed range and shows it as is."""
    with pytest.raises(ValueError, match="after the snapshot"):
        _period("2026-10-10")


def test_a_start_that_is_not_a_date_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text("iso3: NPL\nhdx:\n  time_period_start: 26-08-2026\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="time_period_start"):
        load_config(config)
