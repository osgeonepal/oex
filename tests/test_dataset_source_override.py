"""Per-category hdx.dataset_source override is honoured in README + HDX publish."""

from datetime import UTC, datetime

from oex.boundary import Boundary
from oex.config.schema import CategoryConfig, CategoryHdx, RootConfig
from oex.exporter import Exporter
from oex.sources.base import SourceQuery, SourceRunner


class _StubRunner(SourceRunner):
    name = "stub"

    def prepare(self, cfg):
        return None

    def query_for(self, cfg, category):
        raise NotImplementedError


def _make_query(dataset_source: str) -> SourceQuery:
    return SourceQuery(
        source_expr="(SELECT 1)",
        select_fields=["id"],
        where_conditions=[],
        bbox_cols="bbox",
        dataset_source=dataset_source,
        source_url="https://example.org/source",
        source_description="A short source description.",
        snapshot_date=datetime(2026, 5, 7, tzinfo=UTC),
        snapshot_label="2026-05-07",
        extra_readme_lines=[],
    )


def _make_boundary() -> Boundary:
    return Boundary(
        iso3="NPL",
        bbox=(80.0, 27.0, 85.0, 30.0),
        geojson='{"type":"Polygon","coordinates":[[[80,27],[85,27],[85,30],[80,30],[80,27]]]}',
        source="user-provided",
    )


def _find_source_line(readme: list[str]) -> str:
    for line in readme:
        if line.startswith("Source:"):
            return line
    raise AssertionError("Source line missing from README")


def test_category_dataset_source_override_wins_over_runner_default() -> None:
    cfg = RootConfig(iso3="NPL", key="hotosm")
    exporter = Exporter(cfg, _StubRunner())
    category = CategoryConfig(
        name="buildings",
        hdx=CategoryHdx(dataset_source="OpenStreetMap contributors"),
    )
    query = _make_query("OpenStreetMap (Geofabrik NPL 2026-05-07)")

    readme = exporter._build_readme(
        fmt="gpkg",
        category=category,
        query=query,
        boundary=_make_boundary(),
        feature_count=1234,
    )

    src_line = _find_source_line(readme)
    assert "OpenStreetMap contributors" in src_line
    assert "Geofabrik" not in src_line


def test_category_dataset_source_falls_back_to_runner_default() -> None:
    cfg = RootConfig(iso3="NPL", key="hotosm")
    exporter = Exporter(cfg, _StubRunner())
    category = CategoryConfig(name="buildings")  # no override
    query = _make_query("OpenStreetMap (Geofabrik NPL 2026-05-07)")

    readme = exporter._build_readme(
        fmt="gpkg",
        category=category,
        query=query,
        boundary=_make_boundary(),
        feature_count=1234,
    )

    src_line = _find_source_line(readme)
    assert "Geofabrik NPL" in src_line
