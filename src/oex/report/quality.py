"""How many of a layer's attributes carry data for most of its features."""

from dataclasses import dataclass

from oex.metadata import ColumnReport

_WELL_POPULATED_PERCENT = 50.0
_PARTIAL_PERCENT = 25.0


@dataclass(frozen=True)
class LayerQuality:
    well: int
    partial: int
    rare: int

    @property
    def total_columns(self) -> int:
        return self.well + self.partial + self.rare


def layer_quality(columns: list[ColumnReport]) -> LayerQuality:
    counts = {"well": 0, "partial": 0, "rare": 0}
    for column in columns:
        counts[coverage_bucket(100.0 - column.null_percent)] += 1
    return LayerQuality(**counts)


def coverage_bucket(coverage_percent: float) -> str:
    if coverage_percent >= _WELL_POPULATED_PERCENT:
        return "well"
    if coverage_percent >= _PARTIAL_PERCENT:
        return "partial"
    return "rare"
