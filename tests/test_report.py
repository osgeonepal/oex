"""Unit tests for the multi-source HTML report."""

import json

from oex.metadata import ColumnReport, MetadataReport
from oex.report import SourceMetadata, render_report


def _meta(
    *,
    feature_count: int = 100,
    geometry_types: dict[str, int] | None = None,
    columns: list[ColumnReport] | None = None,
    bbox: tuple[float, float, float, float] | None = (0.0, 0.0, 1.0, 1.0),
    summary: str = "100 features.",
) -> MetadataReport:
    return MetadataReport(
        feature_count=feature_count,
        geometry_types=geometry_types or {"POLYGON": feature_count},
        bbox=bbox,
        columns=columns or [],
        summary=summary,
    )


def _source(
    name: str = "overture",
    *,
    generated_utc: str = "2026-05-09T14:00:00Z",
    metadata: MetadataReport | None = None,
    pcode_source_date: str | None = None,
    license_url: str | None = "https://opendatacommons.org/licenses/odbl/1-0/",
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


def test_quality_strip_emits_one_cell_per_attribute_column() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 50, 50.0, 1, []),
            ColumnReport("c", "VARCHAR", 99, 99.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert html.count('<span class="qc qc-') == 3 + 3  # 3 strip cells + 3 legend swatches


def test_quality_strip_classes_reflect_coverage_buckets() -> None:
    meta = _meta(
        columns=[
            ColumnReport("filled", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("medium", "VARCHAR", 35, 35.0, 1, []),
            ColumnReport("sparse", "VARCHAR", 70, 70.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert 'qc-high" title="filled (100.00% filled)"' in html
    assert 'qc-mid" title="medium (65.00% filled)"' in html
    assert 'qc-low" title="sparse (30.00% filled)"' in html


def test_quality_caption_counts_sparse_columns() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 80, 80.0, 1, []),
            ColumnReport("c", "VARCHAR", 90, 90.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "2 of 3 attribute columns are over 50% null" in html


def test_quality_caption_when_no_columns_are_sparse() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 10, 10.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "All 2 attribute columns are at least 50% filled" in html
    assert (
        "over 50% null"
        not in html.replace("under 50%", "").replace("over 80% filled", "").split("All 2")[0]
    )


def test_quality_strip_omitted_when_no_attribute_columns() -> None:
    html = render_report({"overture": _source(metadata=_meta(columns=[]))})
    assert '<div class="quality">' not in html
    assert '<div class="quality-strip">' not in html


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
