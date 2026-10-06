"""The sections a dataset page shows for each layer."""

import json

from oex.config.schema import ThemeConfig
from oex.metadata import ColumnReport, MetadataReport, TemporalReport
from oex.palette import DEFAULT_PALETTE
from oex.report import SourceMetadata
from oex.report.map_block import (
    AOI_LABEL,
    MapEntry,
    _display_boundary,
    render_map,
)
from oex.report.page import PageLayer, render_dataset_page
from oex.sources.labels import long_label


def render_report(
    sources: dict[str, SourceMetadata],
    tilesets: dict[str, tuple[str, str]] | None = None,
    boundary_bbox: tuple[float, float, float, float] | None = None,
    layer_label: str | None = None,
    palette: list[str] | None = None,
    map_assets=None,  # noqa: ANN001 - MapAssetsConfig
    *,
    boundary_geojson: str | None = None,
) -> str:
    """The dataset page for these sources."""
    colours = palette or DEFAULT_PALETTE
    layers = [
        PageLayer(
            key=name,
            label=f"{layer_label} ({long_label(name)})" if layer_label else long_label(name),
            color=colours[index % len(colours)],
            source=source,
            downloads=[],
            tileset=(tilesets or {}).get(name),
        )
        for index, (name, source) in enumerate(sorted(sources.items()))
    ]
    return render_dataset_page(
        title="Buildings of Nepal",
        layers=layers,
        boundary_bbox=boundary_bbox,
        boundary_geojson=boundary_geojson,
        map_assets=map_assets,
    )


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
    # The only script is inline, wiring the download control; nothing is fetched.
    assert "cdn." not in html.lower(), "no external CDN refs"
    assert "<script src" not in html.lower()


def test_render_report_without_tilesets_has_no_map() -> None:
    html = render_report({"overture": _source()})
    assert 'id="map"' not in html
    assert "maplibregl.Map(" not in html.lower()


def test_render_report_renders_one_map_entry_per_source_tileset() -> None:
    """A per-category dataset has one tileset per source, each its own map entry."""
    html = render_report(
        {"osm": _source("osm"), "overture": _source("overture")},
        {
            "osm": ("https://example.org/b_osm.pmtiles", "hotosm_npl_buildings_osm"),
            "overture": ("https://example.org/b_ovt.pmtiles", "hotosm_npl_buildings_overture"),
        },
        (85.30, 27.68, 85.36, 27.73),
    )
    assert 'id="map"' in html
    # Each source gets its own vector source and its own coloured layers.
    assert "pmtiles://https://example.org/b_osm.pmtiles" in html
    assert "pmtiles://https://example.org/b_ovt.pmtiles" in html
    assert "'source-layer': 'hotosm_npl_buildings_osm'" in html
    assert "id: 'osm-fill'" in html and "id: 'overture-fill'" in html
    # Each entry owns its whole tileset, so no category|source filter is needed.
    assert "['get', 'category']" not in html
    # Legend toggles both, and the map frames the export boundary.
    legend = html[html.index('<details class="legend"') : html.index("</details>")]
    assert legend.count('type="checkbox" checked') == 2
    assert "map.fitBounds([[85.3,27.68],[85.36,27.73]]" in html


def test_map_legend_names_the_layer_and_the_source() -> None:
    """The legend reads like the combined page: "Buildings (Overture)", not "Overture"."""
    tilesets = {
        "osm": ("https://example.org/b_osm.pmtiles", "hotosm_npl_buildings_osm"),
        "overture": ("https://example.org/b_ovt.pmtiles", "hotosm_npl_buildings_overture"),
    }
    html = render_report(
        {"osm": _source("osm"), "overture": _source("overture")},
        tilesets,
        None,
        "Buildings",
    )
    legend = html[html.index('<details class="legend"') : html.index("</details>")]
    assert "Buildings (OpenStreetMap)" in legend
    assert "Buildings (Overture)" in legend

    # With no layer name to show, the source alone still labels the entry.
    bare = render_report({"osm": _source("osm")}, tilesets)
    bare_legend = bare[bare.index('<details class="legend"') : bare.index("</details>")]
    assert "OpenStreetMap" in bare_legend
    assert "Buildings" not in bare_legend


def test_render_report_lists_every_source_as_its_own_layer() -> None:
    html = render_report(
        {
            "overture": _source("overture", generated_utc="2026-05-09T14:00:00Z"),
            "osm": _source("osm", generated_utc="2026-05-09T15:30:00Z"),
        }
    )
    assert "lv-overture" in html
    assert "lv-osm" in html
    assert ">OpenStreetMap<" in html
    assert ">Overture<" in html


def test_switching_between_layers_needs_no_javascript() -> None:
    """The page is served from object storage, so switching must survive a blocked script."""
    html = render_report(
        {
            "overture": _source("overture"),
            "osm": _source("osm", generated_utc="2026-05-09T15:30:00Z"),
        }
    )
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


def test_about_section_carries_snapshot_facts() -> None:
    html = render_report({"overture": _source(pcode_source_date="2025-07-29")})
    assert "<dt>Data</dt><dd>Overture Maps (2026-04-15.0)</dd>" in html
    assert "<dt>Snapshot</dt><dd>2026-04-15.0</dd>" in html
    assert "fieldmaps.io edge-matched humanitarian, 2025-07-29" in html
    assert 'href="https://opendatacommons.org/licenses/odbl/1-0/"' in html
    assert "oex 0.2.1" in html


def test_about_section_omits_pcode_when_not_tagged() -> None:
    html = render_report({"overture": _source(pcode_source_date=None)})
    assert "fieldmaps.io" not in html


def test_about_section_includes_boundary_when_provided() -> None:
    html = render_report(
        {"overture": _source(boundary="geoBoundaries CGAZ ADM0 (buffered +5000m)")}
    )
    assert "<dt>Boundary</dt><dd>geoBoundaries CGAZ ADM0 (buffered +5000m)</dd>" in html


def test_about_section_omits_boundary_when_absent() -> None:
    html = render_report({"overture": _source(boundary=None)})
    assert "<dt>Boundary</dt>" not in html


def test_render_report_omits_admin_coverage_when_no_pcode_columns() -> None:
    html = render_report({"overture": _source()})
    assert "Admin coverage" not in html


def test_admin_coverage_lists_tagged_levels_and_names_the_empty_ones() -> None:
    """A level with no boundaries is one line, not a row of zeros in two tables."""
    meta = _meta(
        columns=[
            ColumnReport("adm1_pcode", "VARCHAR", 0, 0.0, 7, [{"value": "NP03", "count": 100}]),
            ColumnReport("adm1_name", "VARCHAR", 0, 0.0, 7, [{"value": "Bagmati", "count": 100}]),
            ColumnReport("adm4_pcode", "VARCHAR", 100, 100.0, 0, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    admin = html.split("Admin coverage", 1)[1]
    assert '<tr><td>adm1</td><td class="num">100.00%</td>' in admin
    assert "<td>adm4</td>" not in admin
    assert "No adm4 boundaries for this area." in admin
    # Admin columns are summarised here, so the attribute table leaves them out.
    assert "Attributes</h2>" not in html


def test_source_mix_section_only_appears_with_more_than_one_underlying_source() -> None:
    one = _meta(
        columns=[
            ColumnReport("source", "VARCHAR", 0, 0.0, 1, [{"value": "OSM", "count": 100}]),
        ]
    )
    html_one = render_report({"osm": _source(metadata=one)})
    assert "Source mix" not in html_one
    assert '<span class="col-name">source</span>' in html_one

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


def _stac_payload(src: SourceMetadata) -> dict:
    """What the exporter writes: a STAC Item, which is the only metadata oex publishes."""
    from oex.config.schema import BoundaryConfig, CategoryConfig, RootConfig
    from oex.stac import LayerAssets, build_item, to_payload

    cfg = RootConfig(iso3="NPL", key="hotosm", boundary=BoundaryConfig())
    category = CategoryConfig(name="buildings")
    category.hdx.license_url = src.license_url
    layer = LayerAssets(category=category, source_metadata=src, dataset_name="hotosm_npl_buildings")
    return to_payload(build_item(layer, cfg))


def test_source_metadata_payload_roundtrip() -> None:
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 0, 0.0, 5, [{"value": "x", "count": 10}]),
        ]
    )
    src = _source(pcode_source_date="2025-07-29", metadata=meta)
    serialised = json.dumps(_stac_payload(src))
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

    with pytest.raises(ValueError, match="at least one layer"):
        render_report({})


def test_completeness_bar_segments_reflect_bucket_counts() -> None:
    meta = _meta(
        columns=[
            ColumnReport("a", "VARCHAR", 0, 0.0, 1, []),
            ColumnReport("b", "VARCHAR", 10, 10.0, 1, []),
            ColumnReport("c", "VARCHAR", 70, 70.0, 1, []),
            ColumnReport("d", "VARCHAR", 90, 90.0, 1, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert '<i class="well" style="width:50.00%"></i>' in html
    assert '<i class="partial" style="width:25.00%"></i>' in html
    assert '<i class="rare" style="width:25.00%"></i>' in html


def test_completeness_counts_only_the_sources_own_attributes() -> None:
    """oex fills `id` and the admin columns itself, so counting them would flatter the data."""
    meta = _meta(
        columns=[
            ColumnReport("id", "VARCHAR", 0, 0.0, 100, []),
            ColumnReport("adm1_pcode", "VARCHAR", 0, 0.0, 7, []),
            ColumnReport("adm1_name", "VARCHAR", 0, 0.0, 7, []),
            ColumnReport("building", "VARCHAR", 0, 0.0, 3, []),
            ColumnReport("name", "VARCHAR", 90, 90.0, 5, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert '<div class="kpi-value">1 of 2</div>' in html


def test_completeness_value_counts_well_filled_columns() -> None:
    def value(nulls: list[float]) -> str:
        columns = [ColumnReport(f"c{i}", "VARCHAR", 0, n, 1, []) for i, n in enumerate(nulls)]
        html = render_report({"overture": _source(metadata=_meta(columns=columns))})
        return html.split("Attributes filled</div>", 1)[1].split("</div>", 1)[0]

    assert value([0.0, 80.0, 90.0]) == '<div class="kpi-value">1 of 3'
    assert value([0.0, 10.0]) == '<div class="kpi-value">2 of 2'
    assert value([80.0, 90.0]) == '<div class="kpi-value">0 of 2'


def test_completeness_says_none_when_no_attribute_columns() -> None:
    html = render_report({"overture": _source(metadata=_meta(columns=[]))})
    assert '<div class="ratio-bar">' not in html
    assert "no attribute columns" in html


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


def test_about_section_notes_transliteration_when_latin_column_present() -> None:
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 0, 0.0, 5, []),
            ColumnReport("name_latin", "VARCHAR", 0, 0.0, 5, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "transliterated with unidecode" in html


def test_about_section_omits_transliteration_note_when_absent() -> None:
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
    restored = SourceMetadata.from_payload(json.loads(json.dumps(_stac_payload(src))))
    assert restored.metadata.temporal is not None
    assert restored.metadata.temporal.column == "update_time"
    assert restored.metadata.temporal.min == "2024-01-01T00:00:00"
    assert restored.metadata.temporal.non_null_count == 42


def test_attribute_bars_are_coloured_by_how_filled_the_column_is() -> None:
    """Red reads as bad, so only a sparse column may draw red, whatever the theme accent."""
    meta = _meta(
        columns=[
            ColumnReport("name", "VARCHAR", 82, 82.0, 5, []),
            ColumnReport("building", "VARCHAR", 0, 0.0, 3, []),
            ColumnReport("id", "VARCHAR", 0, 0.0, 100, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert '<i class="rare" style="width:18.00%">' in html
    assert '<i class="well" style="width:100.00%">' in html
    assert '<span class="col-name">id</span>' not in html


def test_geometry_types_table_appears_only_for_a_mixed_layer() -> None:
    mostly = _meta(geometry_types={"POLYGON": 995, "POINT": 5}, feature_count=1000)
    assert "Geometry types" not in render_report({"overture": _source(metadata=mostly)})
    mixed = _meta(geometry_types={"POLYGON": 800, "POINT": 200}, feature_count=1000)
    html = render_report({"overture": _source(metadata=mixed)})
    assert "Geometry types" in html
    # A share is not good or bad, so it is drawn neutral.
    assert '<i class="share" style="width:80.00%">' in html


def test_header_names_each_source_and_licence_once() -> None:
    html = render_report({"osm": _source("osm"), "overture": _source("overture")})
    meta = html.split('<p class="ds-meta">', 1)[1].split("</p>", 1)[0]
    assert "OpenStreetMap 2026-04-15.0" in meta and "Overture 2026-04-15.0" in meta
    assert meta.count("ODbL 1.0") == 1


def _entry(key: str = "buildings-osm") -> MapEntry:
    return MapEntry(
        key=key,
        label="Buildings (OSM)",
        color="#1f77b4",
        tileset_url="https://example.invalid/x.pmtiles",
        source_layer="x",
        match_key="buildings|osm",
    )


SMALL_AOI = (
    '{"type": "Polygon", "coordinates": '
    "[[[85.0, 27.0], [85.1, 27.0], [85.1, 27.1], [85.0, 27.1], [85.0, 27.0]]]}"
)


def test_map_draws_the_boundary_when_one_is_given() -> None:
    html = render_map([_entry()], None, boundary_geojson=SMALL_AOI)
    assert "aoi: { type: 'geojson'" in html
    assert "id: 'aoi-line'" in html
    assert ThemeConfig().boundary_outline_color in html
    assert AOI_LABEL in html
    assert 'data-key="aoi"' in html


def test_the_boundary_outline_takes_its_colour_from_the_theme() -> None:
    """The default crimson disappears against a red theme, so it is configurable."""
    theme = ThemeConfig(boundary_outline_color="#1d4ed8")
    html = render_map([_entry()], None, boundary_geojson=SMALL_AOI, theme=theme)
    assert "'line-color': '#1d4ed8'" in html


def test_the_legend_can_turn_every_layer_on_or_off_at_once() -> None:
    """Fourteen checkboxes is not a way to isolate one layer."""
    html = render_map([_entry("a"), _entry("b")], None)
    assert 'data-all="on"' in html and 'data-all="off"' in html
    assert "toggles.forEach((t) => { t.checked = on; applyToggle(t); });" in html


def test_the_boundary_is_toggled_with_the_layers() -> None:
    """Clear all has to clear the outline too, and it is not one of the three style layers."""
    html = render_map([_entry()], None, boundary_geojson=SMALL_AOI)
    assert "if (toggle.dataset.key === 'aoi')" in html


def test_hovering_names_the_feature_before_a_click_opens_it() -> None:
    html = render_map([_entry()], None)
    assert "map.on('mousemove'" in html
    assert 'id="hovercard"' in html


def test_map_has_no_boundary_layer_without_one() -> None:
    html = render_map([_entry()], None)
    assert "id: 'aoi-line'" not in html
    assert AOI_LABEL not in html


def test_a_small_boundary_is_inlined_unchanged() -> None:
    assert _display_boundary(SMALL_AOI) == SMALL_AOI


def test_an_oversized_boundary_is_thinned_before_inlining() -> None:
    """A full-resolution country outline would otherwise bloat every report page."""
    import json as _json

    ring = [[85.0 + i * 1e-6, 27.0 + (i % 2) * 1e-6] for i in range(30000)]
    ring.append(ring[0])
    big = _json.dumps({"type": "Polygon", "coordinates": [ring]})
    assert len(big) > 200_000
    thinned = _display_boundary(big)
    assert len(thinned) < len(big)
    assert _json.loads(thinned)["type"] in ("Polygon", "MultiPolygon")


def test_feature_attributes_reach_the_popup_as_text_not_markup() -> None:
    """Anyone can set an OSM tag to `<img onerror=...>`, and the page is published publicly."""
    html = render_map([_entry()], None)
    assert ".setDOMContent(content)" in html
    assert "row.insertCell().textContent = value;" in html
    assert ".setHTML(" not in html
    assert "innerHTML" not in html


def test_a_layer_with_no_features_says_so_instead_of_reporting_full_columns() -> None:
    """With no features every column has no nulls, which would read as 100% filled."""
    meta = _meta(feature_count=0, columns=[ColumnReport("name", "VARCHAR", 0, 0.0, 0, [])])
    html = render_report({"overture": _source(metadata=meta)})
    assert "This layer has no features." in html
    assert "Attributes filled" not in html
    assert "<dt>Snapshot</dt>" in html


def test_a_column_with_one_value_in_a_million_is_not_called_empty() -> None:
    """null_percent rounds that to 100, so emptiness is read off the counts."""
    meta = _meta(
        feature_count=1_000_000,
        columns=[
            ColumnReport("adm4_pcode", "VARCHAR", 999_999, 100.0, 1, []),
            ColumnReport("office", "VARCHAR", 999_999, 100.0, 1, [{"value": "ngo", "count": 1}]),
        ],
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert '<td>adm4</td><td class="num">&lt;0.01%</td>' in html
    assert "No adm4 boundaries" not in html
    assert "<code>ngo</code>" in html


def test_common_values_explain_columns_without_a_top_list() -> None:
    meta = _meta(
        feature_count=10,
        columns=[
            ColumnReport("ref", "VARCHAR", 0, 0.0, 10, []),
            ColumnReport("note", "VARCHAR", 10, 100.0, 0, []),
            ColumnReport("height", "DOUBLE", 0, 0.0, 4000, []),
        ],
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "unique per feature" in html
    assert '<span class="muted">empty</span>' in html
    assert "4,000 distinct values" in html


def test_geometry_box_without_types_does_not_claim_there_are_no_features() -> None:
    meta = MetadataReport(
        feature_count=5, geometry_types={}, bbox=None, columns=[], summary="5 features."
    )
    html = render_report({"overture": _source(metadata=meta)})
    assert "geometry type not reported" in html
    assert "no features" not in html


def test_source_mix_and_its_column_are_not_both_listed() -> None:
    many = [{"value": "OSM", "count": 60}, {"value": "Microsoft", "count": 40}]
    meta = _meta(columns=[ColumnReport("source", "VARCHAR", 0, 0.0, 2, many)])
    html = render_report({"overture": _source(metadata=meta)})
    assert "Source mix" in html
    assert '<span class="col-name">source</span>' not in html


def test_admin_tagging_reports_the_deepest_tagged_level() -> None:
    meta = _meta(
        columns=[
            ColumnReport("adm1_pcode", "VARCHAR", 0, 0.0, 7, []),
            ColumnReport("adm3_pcode", "VARCHAR", 25, 25.0, 700, []),
            ColumnReport("adm4_pcode", "VARCHAR", 100, 100.0, 0, []),
        ]
    )
    html = render_report({"overture": _source(metadata=meta)})
    tagging = html.split("Admin tagging</div>", 1)[1].split("</div></div>", 1)[0]
    assert "75.00%" in tagging and "to adm3" in tagging
    assert '<i class="well" style="width:75.00%">' in html


def test_source_column_stays_listed_when_the_mix_does_not_cover_it() -> None:
    """Untagged features fall outside the mix, so the column's own fill still matters."""
    partial = [{"value": "OSM", "count": 50}, {"value": "Microsoft", "count": 30}]
    meta = _meta(columns=[ColumnReport("source", "VARCHAR", 20, 20.0, 2, partial)])
    html = render_report({"overture": _source(metadata=meta)})
    assert "Source mix" in html
    assert '<span class="col-name">source</span>' in html


def test_geometry_table_starts_at_one_percent_of_other_types() -> None:
    edge = _meta(geometry_types={"POLYGON": 99, "POINT": 1}, feature_count=100)
    assert "Geometry types" not in render_report({"overture": _source(metadata=edge)})
    over = _meta(geometry_types={"POLYGON": 98, "POINT": 2}, feature_count=100)
    assert "Geometry types" in render_report({"overture": _source(metadata=over)})
