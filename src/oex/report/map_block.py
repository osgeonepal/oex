"""Shared PMTiles map for the report pages.

Both report pages draw the same map. They differ only in how their tiles are
packaged: the combined landing page has one merged tileset whose features carry
`category` and `source`, while a per-category dataset has one tileset per source.
A MapEntry names its own tileset, so both shapes render through this one path.
"""

import json
from dataclasses import dataclass
from html import escape

from oex.config.schema import MapAssetsConfig

MAP_CSS = """
span.sw { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
          margin-right: 8px; vertical-align: -1px; }
#map { width: 100%; height: 520px; border: 1px solid var(--line); border-radius: 3px;
       margin: 6px 0 0; background: #eef1f4; }
.map-wrap { position: relative; }
.legend { position: absolute; left: 10px; bottom: 10px; margin: 0; padding: 8px 11px;
          list-style: none; background: rgba(255, 255, 255, 0.92);
          border: 1px solid var(--line); border-radius: 3px; font-size: 12px;
          line-height: 1.7; }
.legend li { white-space: nowrap; }
.legend label { display: flex; align-items: center; cursor: pointer; }
.legend input { margin: 0 7px 0 0; cursor: pointer; }
.legend-count { margin-left: 7px; color: var(--muted); font-family: var(--mono);
                font-size: 11px; }
table.popup { border-collapse: collapse; margin-top: 6px; font-size: 12px; }
table.popup th { text-align: left; color: var(--muted); font-weight: 400;
                 padding: 2px 10px 2px 0; white-space: nowrap; }
table.popup td { font-family: var(--mono); padding: 2px 0; }
"""


@dataclass(frozen=True)
class MapEntry:
    """One toggleable, coloured layer on the map."""

    key: str  # unique id; also the MapLibre layer-id prefix
    label: str  # legend text
    color: str
    tileset_url: str
    source_layer: str
    # Restricts this entry to part of a shared tileset, as `category|source`.
    # None when the entry owns its whole tileset.
    match_key: str | None = None
    # Shown beside the legend swatch, so the map carries the same headline count
    # the quality table does.
    feature_count: int | None = None


def head_scripts(assets: MapAssetsConfig | None = None) -> str:
    assets = assets or MapAssetsConfig()
    return (
        f'<link href="{escape(assets.maplibre_css)}" rel="stylesheet">\n'
        f'<script src="{escape(assets.maplibre_js)}"></script>\n'
        f'<script src="{escape(assets.pmtiles_js)}"></script>\n'
    )


def render_map(
    entries: list[MapEntry],
    boundary_bbox: tuple[float, float, float, float] | None,
    assets: MapAssetsConfig | None = None,
) -> str:
    """Render the map section: heading, canvas, legend with a checkbox per entry."""
    if not entries:
        return ""
    script = _map_script(entries, boundary_bbox, assets or MapAssetsConfig())
    return (
        "<h2>Map preview</h2>\n"
        '<div class="map-wrap">'
        '<div id="map"></div>'
        f"{_render_legend(entries)}"
        "</div>\n"
        f"<script>{script}</script>\n"
    )


def _map_script(
    entries: list[MapEntry],
    boundary_bbox: tuple[float, float, float, float] | None,
    assets: MapAssetsConfig,
) -> str:
    tilesets = {e.tileset_url for e in entries}
    sources = {f"src{i}": url for i, url in enumerate(sorted(tilesets))}
    source_of = {url: name for name, url in sources.items()}

    source_defs = ",\n      ".join(
        f"{name}: {{ type: 'vector', url: 'pmtiles://{url}' }}" for name, url in sources.items()
    )
    layer_defs = ",\n      ".join(_layer_defs(e, source_of[e.tileset_url]) for e in entries)
    labels = json.dumps(
        {f"{e.key}-{suffix}": e.label for e in entries for suffix in ("fill", "line", "point")}
    )

    # Frame on the export boundary: features are selected whole, so a long road or
    # river crossing the boundary would otherwise stretch the view past the export.
    fit = (
        f"map.fitBounds([[{boundary_bbox[0]},{boundary_bbox[1]}],"
        f"[{boundary_bbox[2]},{boundary_bbox[3]}]], {{padding: 24, duration: 0}});"
        if boundary_bbox
        else ""
    )

    return f"""
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', protocol.tile);
const map = new maplibregl.Map({{
  container: 'map',
  style: {{
    version: 8,
    sources: {{
      basemap: {{ type: 'raster', tiles: ['{assets.basemap_tiles}'], tileSize: 256,
                  attribution: '{assets.basemap_attribution}' }},
      {source_defs}
    }},
    layers: [
      {{ id: 'basemap', type: 'raster', source: 'basemap',
         paint: {{ 'raster-opacity': 0.55 }} }},
      {layer_defs}
    ]
  }}
}});
map.addControl(new maplibregl.NavigationControl());
map.addControl(new maplibregl.FullscreenControl());

// Each entry owns three style layers (polygon, line, point), so a checkbox is a
// visibility flip on those three.
const suffixes = ['fill', 'line', 'point'];
const toggles = document.querySelectorAll('.legend input');

function applyToggle(toggle) {{
  const visibility = toggle.checked ? 'visible' : 'none';
  for (const suffix of suffixes) {{
    map.setLayoutProperty(`${{toggle.dataset.key}}-${{suffix}}`, 'visibility', visibility);
  }}
}}
toggles.forEach((t) => t.addEventListener('change', () => applyToggle(t)));

// Inspecting a feature is how you check the attribute coverage the table reports.
const labelOfLayer = {labels};
const layerIds = Object.keys(labelOfLayer);
map.on('click', (event) => {{
  const hit = map.queryRenderedFeatures(event.point, {{ layers: layerIds }})[0];
  if (!hit) return;
  const rows = Object.entries(hit.properties)
    .filter(([, value]) => value !== null && value !== '')
    .map(([key, value]) => `<tr><th>${{key}}</th><td>${{value}}</td></tr>`)
    .join('');
  new maplibregl.Popup({{ maxWidth: '320px' }})
    .setLngLat(event.lngLat)
    .setHTML(`<b>${{labelOfLayer[hit.layer.id]}}</b><table class="popup">${{rows}}</table>`)
    .addTo(map);
}});
map.on('mouseenter', layerIds, () => {{ map.getCanvas().style.cursor = 'pointer'; }});
map.on('mouseleave', layerIds, () => {{ map.getCanvas().style.cursor = ''; }});

map.on('load', () => {{ {fit} toggles.forEach(applyToggle); }});
"""


def _layer_defs(entry: MapEntry, source: str) -> str:
    common = f"source: '{source}', 'source-layer': '{entry.source_layer}'"
    defs: list[str] = [
        f"{{ id: '{entry.key}-fill', type: 'fill', {common},"
        f" filter: {_filter(entry, 'Polygon')},"
        f" paint: {{ 'fill-color': '{entry.color}', 'fill-opacity': 0.5,"
        f" 'fill-outline-color': '{entry.color}' }} }}",
        f"{{ id: '{entry.key}-line', type: 'line', {common},"
        f" filter: {_filter(entry, 'LineString')},"
        f" paint: {{ 'line-color': '{entry.color}', 'line-width': 1.3 }} }}",
        f"{{ id: '{entry.key}-point', type: 'circle', {common},"
        f" filter: {_filter(entry, 'Point')},"
        f" paint: {{ 'circle-color': '{entry.color}', 'circle-radius': 3,"
        f" 'circle-stroke-width': 0.5, 'circle-stroke-color': '#ffffff' }} }}",
    ]
    separator: str = ",\n      "
    return separator.join(defs)


def _filter(entry: MapEntry, geometry: str) -> str:
    geometry_test = f"['==', ['geometry-type'], '{geometry}']"
    if entry.match_key is None:
        return geometry_test
    key_expr = "['concat', ['get', 'category'], '|', ['get', 'source']]"
    return f"['all', {geometry_test}, ['==', {key_expr}, '{entry.match_key}']]"


def _render_legend(entries: list[MapEntry]) -> str:
    items = "".join(
        "<li><label>"
        f'<input type="checkbox" checked data-key="{escape(e.key)}">'
        f'<span class="sw" style="background:{e.color}"></span>{escape(e.label)}'
        f"{_count_label(e.feature_count)}"
        "</label></li>"
        for e in entries
    )
    return f'<ul class="legend">{items}</ul>'


def _count_label(feature_count: int | None) -> str:
    if feature_count is None:
        return ""
    if feature_count >= 1000:
        shown = f"{feature_count / 1000:.0f}k"
    else:
        shown = str(feature_count)
    return f'<span class="legend-count">{shown}</span>'
