"""The shared map component emits the JavaScript both report pages depend on."""

import pytest

from oex.report.map_block import MapEntry, render_map


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


def test_each_entry_owns_three_toggleable_layers() -> None:
    html = render_map(_entries(), None)
    for key in ("buildings-osm", "roads-osm"):
        for suffix in ("fill", "line", "point"):
            assert f"id: '{key}-{suffix}'" in html
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
    from oex.config.schema import MapAssetsConfig
    from oex.report.map_block import head_scripts

    assets = MapAssetsConfig(
        basemap_tiles="https://tiles.internal/{z}/{x}/{y}.png",
        basemap_attribution="Internal",
        maplibre_css="https://internal/maplibre.css",
        maplibre_js="https://internal/maplibre.js",
        pmtiles_js="https://internal/pmtiles.js",
    )
    html = render_map(_entries(), None, assets)
    assert "https://tiles.internal/{z}/{x}/{y}.png" in html
    assert "attribution: 'Internal'" in html
    assert "unpkg.com" not in html

    scripts = head_scripts(assets)
    assert "https://internal/maplibre.js" in scripts
    assert "unpkg.com" not in scripts
