"""Unit tests for the combined-dataset landing page."""

import pytest

from oex.metadata import ColumnReport, MetadataReport
from oex.report import SourceMetadata
from oex.report.landing import CategoryPanel, render_landing


def _cols() -> list[ColumnReport]:
    return [
        ColumnReport("id", "VARCHAR", 0, 0.0, 500, []),
        ColumnReport("name", "VARCHAR", 250, 50.0, 40, []),
        ColumnReport(
            "building",
            "VARCHAR",
            0,
            0.0,
            3,
            [{"value": "yes", "count": 300}, {"value": "house", "count": 150}],
        ),
        ColumnReport("adm2_pcode", "VARCHAR", 0, 0.0, 1, [{"value": "NP0433", "count": 500}]),
    ]


def _meta(feature_count: int = 500) -> MetadataReport:
    return MetadataReport(
        feature_count=feature_count,
        geometry_types={"POLYGON": feature_count - 10, "POINT": 10},
        bbox=(83.0, 28.0, 84.0, 29.0),
        columns=_cols(),
        summary=f"{feature_count} features.",
        temporal=None,
    )


def _source(name: str = "osm", feature_count: int = 500) -> SourceMetadata:
    return SourceMetadata(
        source_name=name,
        snapshot_label="2026-07-01.0",
        dataset_source="OpenStreetMap",
        generated_utc="2026-07-01T14:00:00Z",
        oex_version="0.4.1",
        license_label="ODbL 1.0",
        license_url="https://opendatacommons.org/licenses/odbl/1-0/",
        pcode_source_date=None,
        boundary="user geom",
        metadata=_meta(feature_count),
    )


def _panels() -> list[CategoryPanel]:
    return [
        CategoryPanel(slug="buildings", label="Buildings", sources=[_source(feature_count=500)]),
        CategoryPanel(slug="roads", label="Roads", sources=[_source(feature_count=200)]),
    ]


def test_landing_table_first_then_map() -> None:
    html = render_landing(
        title="Nepal OpenStreetMap vector data",
        subtitle="2 layers",
        panels=_panels(),
        pmtiles_url="https://example.org/c.pmtiles",
        pmtiles_layer="combined",
    )
    assert "<h2>Layers and data quality</h2>" in html
    # Table renders before the map, matching the existing HDX report order.
    assert html.index("Layers and data quality") < html.index('id="map"')
    assert html.index("<table") < html.index('id="map"')


def test_landing_table_has_expected_columns_and_values() -> None:
    html = render_landing(
        title="t", subtitle="s", panels=_panels(), pmtiles_url=None, pmtiles_layer=None
    )
    for header in (
        "Layer",
        "Source",
        "License",
        "Features",
        "Named",
        "Attribute coverage",
        "Geometry",
    ):
        assert f">{header}</th>" in html
    assert "Buildings (OSM)" in html
    assert "500" in html  # feature count
    assert "50%" in html  # named coverage (100 - 50% null)
    assert "Polygon 490, Point 10" in html  # geometry mix, count-desc


def test_landing_table_shows_the_same_attribute_coverage_the_report_measures() -> None:
    """Buckets match the report's thresholds: 50%+ populated is well, 25 to 50 partial."""
    from oex.report.quality import layer_quality

    meta = MetadataReport(
        feature_count=100,
        geometry_types={"POLYGON": 100},
        bbox=(83.0, 28.0, 84.0, 29.0),
        columns=[
            ColumnReport("id", "VARCHAR", 0, 0.0, 100, []),  # 100% populated: well
            ColumnReport("name", "VARCHAR", 60, 60.0, 20, []),  # 40% populated: partial
            ColumnReport("height", "DOUBLE", 90, 90.0, 5, []),  # 10% populated: rare
        ],
        summary="100 features.",
        temporal=None,
    )
    source = SourceMetadata(
        source_name="osm",
        snapshot_label="2026-07-01.0",
        dataset_source="OpenStreetMap",
        generated_utc="2026-07-01T14:00:00Z",
        oex_version="0.4.1",
        license_label="ODbL 1.0",
        license_url=None,
        pcode_source_date=None,
        boundary=None,
        metadata=meta,
    )
    panels = [CategoryPanel(slug="buildings", label="Buildings", sources=[source])]

    # The landing bar and the report's bar are the same measurement.
    quality = layer_quality(meta)
    assert (quality.well, quality.partial, quality.rare) == (1, 1, 1)

    html = render_landing(
        title="t", subtitle="s", panels=panels, pmtiles_url=None, pmtiles_layer=None
    )
    assert "1/3 populated" in html
    assert 'class="cov-well"' in html
    assert 'class="cov-partial"' in html
    assert 'class="cov-rare"' in html
    assert "40%" in html  # named coverage from the same metric


def test_landing_layer_swatch_colors_match_the_map_layers() -> None:
    html = render_landing(
        title="t",
        subtitle="s",
        panels=_panels(),
        pmtiles_url="https://example.org/c.pmtiles",
        pmtiles_layer="combined",
    )
    # The first palette colour paints the first layer's swatch and its map layers.
    assert 'style="background:#e6194B"' in html
    assert "id: 'buildings-osm-fill'" in html
    assert "'fill-color': '#e6194B'" in html
    # Every layer lives in one merged tileset, so each map layer selects its own
    # features by category|source.
    assert (
        "['==', ['concat', ['get', 'category'], '|', ['get', 'source']], 'buildings|osm']" in html
    )


def test_landing_map_carries_a_legend_matching_every_layer() -> None:
    html = render_landing(
        title="t",
        subtitle="s",
        panels=_panels(),
        pmtiles_url="https://example.org/c.pmtiles",
        pmtiles_layer="combined",
    )
    legend = html[html.index('<ul class="legend">') : html.index("</ul>")]
    for label, color in (("Buildings (OSM)", "#e6194B"), ("Roads (OSM)", "#3cb44b")):
        assert f'<span class="sw" style="background:{color}"></span>{label}' in legend


def test_landing_legend_toggles_switch_layer_visibility() -> None:
    html = render_landing(
        title="t",
        subtitle="s",
        panels=_panels(),
        pmtiles_url="https://example.org/c.pmtiles",
        pmtiles_layer="combined",
    )
    legend = html[html.index('<ul class="legend">') : html.index("</ul>")]
    # One checkbox per layer, keyed to that layer's map-layer ids.
    assert legend.count('type="checkbox" checked') == 2
    assert 'data-key="buildings-osm"' in legend
    assert 'data-key="roads-osm"' in legend
    assert "map.setLayoutProperty" in html
    assert "'visibility'" in html


def test_landing_without_pmtiles_omits_map() -> None:
    html = render_landing(
        title="t", subtitle="s", panels=_panels(), pmtiles_url=None, pmtiles_layer=None
    )
    assert 'id="map"' not in html
    assert "maplibre" not in html.lower()
    assert "<table" in html


def test_landing_preserves_panel_order() -> None:
    html = render_landing(
        title="t", subtitle="s", panels=_panels(), pmtiles_url=None, pmtiles_layer=None
    )
    assert html.index("Buildings (OSM)") < html.index("Roads (OSM)")


def test_landing_map_frames_the_export_boundary_not_the_feature_extent() -> None:
    # A river crossing the boundary is selected whole, so its bbox reaches well
    # outside the exported area. The map must still open on the boundary.
    panels = [
        CategoryPanel(slug="buildings", label="Buildings", sources=[_source()]),
        CategoryPanel(slug="rivers", label="Rivers", sources=[_source(name="overture")]),
    ]
    html = render_landing(
        title="t",
        subtitle="s",
        panels=panels,
        pmtiles_url="https://example.org/c.pmtiles",
        pmtiles_layer="combined",
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
    )
    assert "map.fitBounds([[85.3,27.68],[85.36,27.73]]" in html
    # The layers' own bbox (83..84, 28..29) must not drive the view.
    assert "83.0" not in html


def test_landing_map_falls_back_to_feature_bounds_without_a_boundary() -> None:
    html = render_landing(
        title="t",
        subtitle="s",
        panels=_panels(),
        pmtiles_url="https://example.org/c.pmtiles",
        pmtiles_layer="combined",
    )
    assert "map.fitBounds([[83.0,28.0],[84.0,29.0]]" in html


def test_landing_requires_at_least_one_panel() -> None:
    with pytest.raises(ValueError):
        render_landing(title="t", subtitle="s", panels=[], pmtiles_url=None, pmtiles_layer=None)


def test_landing_uses_the_configured_palette() -> None:
    html = render_landing(
        title="t",
        subtitle="s",
        panels=_panels(),
        pmtiles_url="https://example.org/c.pmtiles",
        pmtiles_layer="combined",
        palette=["#111111", "#222222"],
    )
    assert 'style="background:#111111"' in html
    assert "'fill-color': '#222222'" in html
    assert "#e6194B" not in html
