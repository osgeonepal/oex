"""The shared map component emits the JavaScript both report pages depend on."""

import json
import re

import pytest

from oex.config.schema import MapAssetsConfig, ThemeConfig
from oex.report.map_block import (
    _AOI_INLINE_LIMIT_BYTES,
    AOI_LABEL,
    MapEntry,
    _display_boundary,
    head_scripts,
    render_map,
)


def _entries(match: bool = False) -> list[MapEntry]:
    return [
        MapEntry(
            key="buildings-osm",
            label="Buildings (OSM)",
            color="#e6194B",
            tileset_url="https://example.org/a.pmtiles",
            source_layer="combined",
            match_key="buildings|osm" if match else None,
        ),
        MapEntry(
            key="roads-osm",
            label="Roads (OSM)",
            color="#3cb44b",
            tileset_url="https://example.org/b.pmtiles",
            source_layer="roads",
        ),
    ]


def _script(html: str) -> str:
    return html[html.index("<script>") + len("<script>") : html.rindex("</script>")]


@pytest.mark.parametrize("match", [False, True])
def test_generated_script_braces_are_balanced(match: bool) -> None:
    """f-string brace escaping is easy to get wrong and yields JS that will not parse."""
    script = _script(render_map(_entries(match), (0.0, 0.0, 1.0, 1.0)))
    assert script.count("{") == script.count("}")
    # A doubled brace means an unescaped `{{` reached the output.
    assert "}}" not in script
    assert "{{" not in script


def test_map_offers_a_fullscreen_control() -> None:
    assert "FullscreenControl" in render_map(_entries(), None)


def test_each_entry_owns_three_toggleable_layers() -> None:
    html = render_map(_entries(), None)
    for key in ("buildings-osm", "roads-osm"):
        for suffix in ("fill", "line", "point"):
            assert f'id: "{key}-{suffix}"' in html
        assert f'data-key="{key}"' in html
    assert "map.setLayoutProperty" in html


def test_entries_from_distinct_tilesets_get_distinct_sources() -> None:
    html = render_map(_entries(), None)
    assert "pmtiles://https://example.org/a.pmtiles" in html
    assert "pmtiles://https://example.org/b.pmtiles" in html


def test_no_entries_renders_nothing() -> None:
    assert render_map([], (0.0, 0.0, 1.0, 1.0)) == ""


def test_map_assets_are_configurable() -> None:
    """The page fetches these at view time, so a run must be able to repoint them."""
    assets = MapAssetsConfig(
        basemap_tiles="https://tiles.internal/{z}/{x}/{y}.png",
        basemap_attribution="Internal",
        maplibre_css="https://internal/maplibre.css",
        maplibre_js="https://internal/maplibre.js",
        pmtiles_js="https://internal/pmtiles.js",
    )
    html = render_map(_entries(), None, assets)
    assert "https://tiles.internal/{z}/{x}/{y}.png" in html
    assert 'attribution: "Internal"' in html
    assert "unpkg.com" not in html

    scripts = head_scripts(assets)
    assert "https://internal/maplibre.js" in scripts
    assert "unpkg.com" not in scripts


def _literal_after(script: str, marker: str) -> object:
    """The JSON-compatible JS literal that follows `marker` in the script."""
    value, _ = json.JSONDecoder().raw_decode(script, script.index(marker) + len(marker))
    return value


def test_an_attribution_with_quotes_is_written_as_a_valid_js_string() -> None:
    attribution = 'O\'Brien "tiles"'
    assets = MapAssetsConfig(basemap_attribution=attribution)
    script = _script(render_map(_entries(), (0.0, 0.0, 1.0, 1.0), assets))
    assert _literal_after(script, "attribution: ") == attribution


def test_a_source_layer_and_match_key_with_quotes_are_written_as_valid_js_strings() -> None:
    entry = MapEntry(
        key="people's-places",
        label="People's places",
        color="#e6194B",
        tileset_url="https://example.org/a.pmtiles",
        source_layer="it's",
        match_key="people's|o'sm",
    )
    script = _script(render_map([entry], (0.0, 0.0, 1.0, 1.0)))
    assert _literal_after(script, "'source-layer': ") == "it's"
    assert "id: " + json.dumps("people's-places-fill") in script
    assert _literal_after(script, "'|', ['get', 'source']], ") == "people's|o'sm"


def test_an_attribution_containing_a_closing_script_tag_stays_inside_the_literal() -> None:
    attribution = "</script><script>alert(1)</script>"
    assets = MapAssetsConfig(basemap_attribution=attribution)
    html = render_map(_entries(), (0.0, 0.0, 1.0, 1.0), assets)
    script = html[html.index("<script>") + len("<script>") : html.index("</script>")]
    assert _literal_after(script, "attribution: ") == attribution


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
    assert "'line-color': \"#1d4ed8\"" in html


def test_the_legend_can_turn_every_layer_on_or_off_at_once() -> None:
    """Fourteen checkboxes is not a way to isolate one layer."""
    html = render_map([_entry("a"), _entry("b")], None)
    assert 'data-all="on"' in html and 'data-all="off"' in html


def test_a_legend_of_one_layer_has_no_select_all_or_clear_all() -> None:
    html = render_map([_entry("a")], None)
    assert 'data-all="on"' not in html and 'data-all="off"' not in html


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
    ring = [[85.0 + i * 1e-6, 27.0 + (i % 2) * 1e-6] for i in range(30000)]
    ring.append(ring[0])
    big = json.dumps({"type": "Polygon", "coordinates": [ring]})
    assert len(big) > _AOI_INLINE_LIMIT_BYTES
    thinned = _display_boundary(big)
    assert len(thinned) <= _AOI_INLINE_LIMIT_BYTES
    assert json.loads(thinned)["type"] in ("Polygon", "MultiPolygon")


def test_feature_attributes_reach_the_popup_as_text_not_markup() -> None:
    """Anyone can set an OSM tag to `<img onerror=...>`, and the page is published publicly."""
    html = render_map([_entry()], None)
    assert ".setDOMContent(content)" in html
    assert "row.insertCell().textContent = value;" in html
    assert ".setHTML(" not in html
    assert "innerHTML" not in html


def test_every_layers_points_and_lines_draw_above_every_layers_polygons() -> None:
    """A campus polygon from one layer must not hide the roads and points of another."""
    script = _script(render_map(_entries(), None))
    order = [match.group(1) for match in re.finditer(r'id: "[^"]+-(fill|line|point)"', script)]
    assert order == ["fill", "fill", "line", "line", "point", "point"]


def test_a_long_legend_starts_closed_on_phones_and_a_short_one_stays_open() -> None:
    long_legend = render_map([_entry(name) for name in "abcd"], None)
    short_legend = render_map([_entry(name) for name in "abc"], None)
    assert "matchMedia('(max-width: 480px)')" in long_legend
    assert "(max-width: 480px)" not in short_legend


def test_smaller_point_layers_draw_above_larger_ones_so_they_stay_clickable() -> None:
    """Overture puts every health facility in its points of interest too, at the same spot."""
    entries = [
        MapEntry("health", "Health", "#111111", "https://x/a.pmtiles", "a", feature_count=24),
        MapEntry("poi", "POI", "#222222", "https://x/a.pmtiles", "a", feature_count=578),
    ]
    script = _script(render_map(entries, None))
    points = re.findall(r'id: "([^"]+)-point"', script)
    assert points == ["poi", "health"]


def test_one_zoom_button_frames_the_ticked_layers_or_the_area() -> None:
    entry = MapEntry("a", "A", "#111111", "https://x/a.pmtiles", "a", bbox=(85.0, 27.0, 85.5, 27.5))
    html = render_map([entry], (84.0, 26.0, 86.0, 28.0))
    assert html.count("data-zoom title") == 1
    assert "const areaBounds = [84.0, 26.0, 86.0, 28.0];" in html
    assert 'const boundsOfKey = {"a": [85.0, 27.0, 85.5, 27.5]};' in html


def test_without_a_boundary_or_layer_extents_there_is_nothing_to_zoom_to() -> None:
    html = render_map([_entry("a")], None)
    assert "data-zoom title" not in html


def test_a_popup_takes_the_colour_of_its_layer() -> None:
    """The page accent would paint every layer's popup the same colour."""
    html = render_map([MapEntry("a", "A", "#ffd8b1", "https://x/a.pmtiles", "a")], None)
    assert '"a-point": "#ffd8b1"' in html


def test_lines_get_a_dark_edge_that_toggles_with_their_layer() -> None:
    """Pale palette colours would otherwise vanish on the light basemap."""
    script = _script(render_map([_entry("a")], None))
    assert "id: \"a-casing\", type: 'line'" in script
    assert "const suffixes = ['fill', 'casing', 'line', 'point'];" in script


@pytest.mark.parametrize(("count", "shown"), [(999, "999"), (3856, "3.9k"), (52113, "52k")])
def test_legend_counts_keep_a_decimal_below_ten_thousand(count, shown) -> None:
    html = render_map(
        [MapEntry("a", "A", "#111", "https://x/a.pmtiles", "a", feature_count=count)], None
    )
    assert f'<span class="legend-count">{shown}</span>' in html


def test_buildings_draw_above_the_residential_areas_they_sit_in() -> None:
    entries = [
        MapEntry("buildings", "B", "#111111", "https://x/a.pmtiles", "a", feature_count=52113),
        MapEntry("residential", "R", "#222222", "https://x/a.pmtiles", "a", feature_count=1473),
    ]
    fills = re.findall(r'id: "([^"]+)-fill"', _script(render_map(entries, None)))
    assert fills == ["residential", "buildings"]


def test_a_tileset_credits_the_sources_drawn_from_it() -> None:
    """Overture asks to be credited wherever its data is shown."""
    entry = MapEntry(
        "a", "A", "#111111", "https://x/a.pmtiles", "a", attribution="Overture Maps Foundation"
    )
    assert 'attribution: "Overture Maps Foundation"' in render_map([entry], None)
