"""Unit tests for the multi-source HTML report."""

import json

from oex.metadata import ColumnReport, MetadataReport, TemporalReport
from oex.report import SourceMetadata, render_report


def _meta(
    *,
    feature_count: int = 100,
    geometry_types: dict[str, int] | None = None,
    columns: list[ColumnReport] | None = None,
    bbox: tuple[float, float, float, float] | None = (0.0, 0.0, 1.0, 1.0),
    summary: str = "100 features.",
    temporal: TemporalReport | None = None,
) -> MetadataReport:
    return MetadataReport(
        feature_count=feature_count,
        geometry_types=geometry_types or {"POLYGON": feature_count},
        bbox=bbox,
        columns=columns or [],
        summary=summary,
        temporal=temporal,
    )


def _source(
    name: str = "overture",
    *,
    generated_utc: str = "2026-05-09T14:00:00Z",
    metadata: MetadataReport | None = None,
    pcode_source_date: str | None = None,
    license_url: str | None = "https://opendatacommons.org/licenses/odbl/1-0/",
    boundary: str | None = None,
) -> SourceMetadata:
    return SourceMetadata(
        source_name=name,
        snapshot_label="2026-04-15.0",
        dataset_source="Overture Maps (2026-04-15.0)",
        generated_utc=generated_utc,
        oex_version="0.2.1",
        license_label="ODbL 1.0",
        license_url=license_url,
        pcode_source_date=pcode_source_date,
        boundary=boundary,
        metadata=metadata or _meta(),
    )


def test_render_report_is_self_contained_html() -> None:
    html = render_report({"overture": _source()})
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert "<style>" in html
    assert "<script" not in html.lower(), "no JS in minimal mode"
    assert "cdn." not in html.lower(), "no external CDN refs"


def test_render_report_single_source_omits_tabs_nav() -> None:
    html = render_report({"overture": _source()})
    assert '<div class="tabs">' not in html
    assert "panel-overture" in html


def test_render_report_dual_source_emits_both_panels_and_tabs() -> None:
    html = render_report(
        {
            "overture": _source("overture", generated_utc="2026-05-09T14:00:00Z"),
            "osm": _source("osm", generated_utc="2026-05-09T15:30:00Z"),
        }
    )
    assert '<div class="tabs">' in html
    assert "panel-overture" in html
    assert "panel-osm" in html
    assert ">OpenStreetMap<" in html
    assert ">Overture<" in html


def test_default_tab_is_most_recently_generated_source() -> None:
    html = render_report(
        {
            "overture": _source("overture", generated_utc="2026-05-09T14:00:00Z"),
            "osm": _source("osm", generated_utc="2026-05-09T15:30:00Z"),
        }
    )
    assert 'id="tab-osm" class="tab-input" checked' in html
    assert 'id="tab-overture" class="tab-input"' in html
    assert 'id="tab-overture" class="tab-input" checked' not in html


def test_render_report_uses_only_css_for_tab_switching() -> None:
    html = render_report(
        {
            "overture": _source("overture"),
            "osm": _source("osm", generated_utc="2026-05-09T15:30:00Z"),
        }
    )
    assert "<script" not in html.lower()
    assert "onclick" not in html.lower()
    assert ":checked ~" in html, "must rely on CSS sibling selectors"


def test_render_report_escapes_user_values() -> None:
    meta = _meta(
        columns=[
            ColumnReport(
                name="class",
                type="VARCHAR",
                null_count=0,
                null_percent=0.0,
                distinct_count=2,
                top_values=[
                    {"value": "<script>alert(1)</script>", "count": 50},
                    {"value": "ok", "count": 50},
                ],
            ),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_per_tab_footer_carries_snapshot_facts() -> None:
    html = render_report({"overture": _source(pcode_source_date="2025-07-29")})
    assert "Overture Maps (2026-04-15.0)" in html
    assert "snapshot 2026-04-15.0" in html
    assert "fieldmaps.io edge-matched humanitarian (2025-07-29)" in html
    assert "ODbL 1.0" in html
    assert 'href="https://opendatacommons.org/licenses/odbl/1-0/"' in html
    assert "oex 0.2.1" in html


def test_per_tab_footer_omits_pcode_when_not_tagged() -> None:
    html = render_report({"overture": _source(pcode_source_date=None)})
    assert "fieldmaps.io" not in html


def test_per_tab_footer_includes_boundary_when_provided() -> None:
    html = render_report(
        {"overture": _source(boundary="geoBoundaries CGAZ ADM0 (buffered +5000m)")}
    )
    assert "boundary geoBoundaries CGAZ ADM0 (buffered +5000m)" in html


def test_per_tab_footer_omits_boundary_when_absent() -> None:
    html = render_report({"overture": _source(boundary=None)})
    assert "boundary " not in html


def test_render_report_omits_pcode_section_when_no_pcode_columns() -> None:
    html = render_report({"overture": _source()})
    assert "Pcode coverage" not in html


def test_render_report_includes_pcode_table_when_columns_present() -> None:
    meta = _meta(
        columns=[
            ColumnReport("adm1_pcode", "VARCHAR", 0, 0.0, 7, [{"value": "NP03", "count": 100}]),
            ColumnReport("adm4_pcode", "VARCHAR", 100, 100.0, 0, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "Pcode coverage" in html
    assert "adm1_pcode" in html
    assert "adm4_pcode" in html


def test_source_mix_section_only_appears_with_more_than_one_underlying_source() -> None:
    one = _meta(
        columns=[
            ColumnReport("source", "VARCHAR", 0, 0.0, 1, [{"value": "OSM", "count": 100}]),
        ]
    )
    html_one = render_report({"osm": _source(metadata=one)})
    assert "Source mix" not in html_one

    many = _meta(
        columns=[
            ColumnReport(
                "source",
                "VARCHAR",
                0,
                0.0,
                3,
                [
                    {"value": "OSM", "count": 60},
                    {"value": "Microsoft", "count": 30},
                    {"value": "Google", "count": 10},
                ],
            ),
        ]
    )
    html_many = render_report({"overture": _source(metadata=many)})
    assert "Source mix" in html_many


def test_source_metadata_payload_roundtrip() -> None:
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 0, 0.0, 5, [{"value": "x", "count": 10}]),
        ]
    )
    src = _source(pcode_source_date="2025-07-29", metadata=meta)
    payload = src.to_payload()
    serialised = json.dumps(payload)
    restored = SourceMetadata.from_payload(json.loads(serialised))

    assert restored.source_name == src.source_name
    assert restored.snapshot_label == src.snapshot_label
    assert restored.pcode_source_date == "2025-07-29"
    assert restored.metadata.feature_count == src.metadata.feature_count
    assert restored.metadata.bbox == src.metadata.bbox
    assert restored.metadata.columns[0].name == "name"
    assert restored.metadata.columns[0].top_values == [{"value": "x", "count": 10}]


def test_render_report_raises_on_empty_sources() -> None:
    import pytest

    with pytest.raises(ValueError, match="at least one source"):
        render_report({})


def test_ratio_bar_segments_reflect_bucket_counts() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 10, 10.0, 1, []),
            ColumnReport("c", "VARCHAR", 70, 70.0, 1, []),
            ColumnReport("d", "VARCHAR", 90, 90.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert '<i class="qc-high" style="width:50.00%"></i>' in html
    assert '<i class="qc-mid"  style="width:25.00%"></i>' in html
    assert '<i class="qc-low"  style="width:25.00%"></i>' in html


def test_ratio_legend_carries_per_bucket_counts() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 70, 70.0, 1, []),
            ColumnReport("c", "VARCHAR", 90, 90.0, 1, []),
            ColumnReport("d", "VARCHAR", 95, 95.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "1 well-populated (50% or more)" in html
    assert "1 partial (25 to 50%)" in html
    assert "2 rare (under 25%)" in html


def test_quality_caption_when_some_columns_are_well_populated() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 80, 80.0, 1, []),
            ColumnReport("c", "VARCHAR", 90, 90.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "1 of 3 attribute columns are well-populated" in html


def test_quality_caption_when_all_columns_are_well_populated() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 10, 10.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "All 2 attribute columns are well-populated" in html


def test_quality_caption_when_no_columns_are_well_populated() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 80, 80.0, 1, []),
            ColumnReport("b", "VARCHAR", 90, 90.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "None of the 2 attribute columns are well-populated" in html


def test_quality_block_omitted_when_no_attribute_columns() -> None:
    html = render_report({"overture": _source(metadata=_meta(columns=[]))})
    assert '<div class="quality">' not in html
    assert '<div class="ratio-bar">' not in html


def test_languages_kpi_lists_name_columns_in_order() -> None:
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 0, 0.0, 5, []),
            ColumnReport("name_en", "VARCHAR", 0, 0.0, 5, []),
            ColumnReport("name_ne", "VARCHAR", 0, 0.0, 5, []),
            ColumnReport("name_hi", "VARCHAR", 0, 0.0, 5, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert ">Languages<" in html
    assert ">4</div>" in html
    assert ">local, en, ne, hi<" in html


def test_languages_kpi_excludes_translit_column() -> None:
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 0, 0.0, 5, []),
            ColumnReport("name_en", "VARCHAR", 0, 0.0, 5, []),
            ColumnReport("name_latin", "VARCHAR", 0, 0.0, 5, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert ">2</div>" in html
    assert ">local, en<" in html
    assert "name_latin" not in html.split('class="kpis"')[1].split("</div></div>")[0]


def test_languages_kpi_says_none_when_no_name_columns() -> None:
    meta = _meta(columns=[ColumnReport("id", "VARCHAR", 0, 0.0, 5, [])])
    html = render_report({"overture": _source(metadata=meta)})
    assert "no name columns" in html


def test_bbox_line_replaces_bbox_kpi() -> None:
    html = render_report({"overture": _source(metadata=_meta(bbox=(0.5, 1.5, 2.5, 3.5)))})
    assert "Bounding box: 0.50, 1.50 to 2.50, 3.50 (EPSG:4326)" in html
    assert ">Bounding box</div>" not in html


def test_bbox_line_omitted_when_bbox_missing() -> None:
    html = render_report({"overture": _source(metadata=_meta(bbox=None))})
    assert "Bounding box" not in html


def test_footer_notes_transliteration_when_latin_column_present() -> None:
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 0, 0.0, 5, []),
            ColumnReport("name_latin", "VARCHAR", 0, 0.0, 5, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "Latin transliteration via unidecode" in html


def test_footer_omits_transliteration_note_when_absent() -> None:
    meta = _meta(columns=[ColumnReport("name", "VARCHAR", 0, 0.0, 5, [])])
    html = render_report({"overture": _source(metadata=meta)})
    assert "transliteration" not in html


def test_temporal_section_renders_when_bounds_present() -> None:
    temporal = TemporalReport(
        column="update_time",
        min="2023-01-15T10:00:00",
        max="2025-12-01T08:00:00",
        non_null_count=87,
    )
    meta = _meta(feature_count=100, temporal=temporal)
    html = render_report({"overture": _source(metadata=meta)})
    assert "Temporal coverage" in html
    assert "2023-01-15T10:00:00" in html
    assert "2025-12-01T08:00:00" in html
    assert "<code>update_time</code>" in html
    # Compact single-line form: percentage + total count, no two-line block.
    assert "87.0% of 100 features" in html


def test_temporal_section_omitted_when_bounds_absent() -> None:
    html = render_report({"overture": _source()})
    assert "Temporal coverage" not in html


def test_temporal_payload_roundtrip() -> None:
    temporal = TemporalReport(
        column="update_time",
        min="2024-01-01T00:00:00",
        max="2024-12-31T23:59:59",
        non_null_count=42,
    )
    meta = _meta(temporal=temporal)
    src = _source(metadata=meta)
    restored = SourceMetadata.from_payload(json.loads(json.dumps(src.to_payload())))
    assert restored.metadata.temporal is not None
    assert restored.metadata.temporal.column == "update_time"
    assert restored.metadata.temporal.min == "2024-01-01T00:00:00"
    assert restored.metadata.temporal.non_null_count == 42


def test_attribute_table_uses_coverage_not_null_percent() -> None:
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 82, 82.14, 5, []),
            ColumnReport("id", "VARCHAR", 0, 0.0, 100, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert ">Coverage<" in html
    assert "Null %" not in html
    assert "17.86%" in html
    assert "100.00%" in html
    assert 'style="width:17.86%"' in html
    assert 'style="width:100.00%"' in html
