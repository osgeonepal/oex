"""One definition of a layer's data quality, shared by both report pages.

The per-category report and the combined landing page describe the same layers, so
they measure them the same way: how many attribute columns carry data, and how
much of the layer is named.
"""

from dataclasses import dataclass

from oex.metadata import MetadataReport

_WELL_POPULATED_PERCENT = 50.0
_PARTIAL_PERCENT = 25.0
_NAME_COLUMN = "name"


@dataclass(frozen=True)
class LayerQuality:
    feature_count: int
    # Share of features carrying a name, or None when the layer has no name column.
    named_percent: float | None
    well: int
    partial: int
    rare: int

    @property
    def total_columns(self) -> int:
        return self.well + self.partial + self.rare

    @property
    def caption(self) -> str:
        total = self.total_columns
        if total == 0:
            return "No attribute columns."
        if self.well == total:
            return f"All {total} attribute columns are well-populated."
        if self.well == 0:
            return f"None of the {total} attribute columns are well-populated."
        return f"{self.well} of {total} attribute columns are well-populated."


def layer_quality(metadata: MetadataReport) -> LayerQuality:
    counts = {"well": 0, "partial": 0, "rare": 0}
    for column in metadata.columns:
        counts[coverage_bucket(100.0 - column.null_percent)] += 1
    return LayerQuality(
        feature_count=metadata.feature_count,
        named_percent=_named_percent(metadata),
        well=counts["well"],
        partial=counts["partial"],
        rare=counts["rare"],
    )


def coverage_bucket(coverage_percent: float) -> str:
    if coverage_percent >= _WELL_POPULATED_PERCENT:
        return "well"
    if coverage_percent >= _PARTIAL_PERCENT:
        return "partial"
    return "rare"


def _named_percent(metadata: MetadataReport) -> float | None:
    column = next((c for c in metadata.columns if c.name == _NAME_COLUMN), None)
    if column is None:
        return None
    return max(0.0, min(100.0, 100.0 - column.null_percent))
