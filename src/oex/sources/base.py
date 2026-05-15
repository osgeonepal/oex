"""Abstract source interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from oex.config.schema import CategoryConfig, RootConfig


class CategorySkippedError(RuntimeError):
    """Raised by a source when a category is not applicable to it."""


@dataclass(frozen=True)
class SourceQuery:
    source_expr: str
    select_fields: list[str]
    where_conditions: list[str]
    # "bbox" for tables with an upstream bbox struct (Overture); "geom" to derive
    # bbox from the geometry column (OSM cache).
    bbox_cols: str
    dataset_source: str
    source_url: str
    source_description: str
    snapshot_date: datetime
    snapshot_label: str
    extra_readme_lines: list[str]


class SourceRunner(ABC):
    name: str

    @abstractmethod
    def prepare(self, cfg: RootConfig) -> None: ...

    @abstractmethod
    def query_for(self, cfg: RootConfig, category: CategoryConfig) -> SourceQuery: ...

    def peek_snapshot_label(self, cfg: RootConfig) -> str | None:
        """Best-effort snapshot label without doing network work or running prepare.

        Returns the label the runner would adopt in prepare(), or None if it can't
        be determined cheaply. Used by the exporter to short-circuit the run when
        every category is already uploaded for that label.
        """
        return None
