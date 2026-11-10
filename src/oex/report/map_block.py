"""The PMTiles map on the dataset page. A MapEntry names its own tileset, so one merged
tileset and one tileset per source render through the same path.
"""

import json
from dataclasses import dataclass
from html import escape
from typing import NamedTuple

from shapely.geometry import mapping, shape

from oex.config.schema import MapAssetsConfig, ThemeConfig

AOI_LABEL = "Area of interest"

# A full-resolution country boundary runs to megabytes. The outline only has to read
# correctly on screen, so anything larger than this is thinned before inlining.
_AOI_INLINE_LIMIT_BYTES = 200_000

MAP_CSS = """
span.sw { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
          margin-right: 8px; vertical-align: -1px; }
#map { width: 100%; height: calc(100vh - 210px); min-height: 380px;
       border: 1px solid var(--line); border-radius: 3px; background: #eef1f4; }
.map-wrap { position: relative; }
.legend { position: absolute; left: 10px; bottom: 10px; margin: 0; padding: 0;
          background: rgba(255, 255, 255, 0.95); border: 1px solid var(--line);
          border-radius: 3px; font-size: 12px; max-height: calc(100% - 20px);
          display: flex; flex-direction: column; overflow-y: auto; }
.legend summary { list-style: none; cursor: pointer; padding: 8px 11px; font-weight: 600;
                  display: flex; align-items: center; gap: 8px;
                  position: sticky; top: 0; z-index: 1; background: #fff; }
.legend summary::-webkit-details-marker { display: none; }
.legend summary::after { content: "\\25be"; margin-left: auto; color: var(--muted); }
.legend[open] summary::after { content: "\\25b4"; }
.legend-actions { display: flex; gap: 6px; padding: 0 11px 8px; }
.legend-actions button { font: inherit; font-size: 11px; padding: 4px 9px; cursor: pointer;
          border: 1px solid var(--line); border-radius: 3px; background: var(--bg);
          color: var(--fg); }
.legend-actions button:hover { border-color: var(--accent); color: var(--accent); }
.legend-actions button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.legend ul { list-style: none; margin: 0; padding: 0 11px 9px; line-height: 1.75; }
.legend li { white-space: nowrap; }
.legend label { display: flex; align-items: center; cursor: pointer; }
.legend input { margin: 0 7px 0 0; cursor: pointer; accent-color: var(--accent); }
.legend-count { margin-left: 7px; color: var(--muted); font-variant-numeric: tabular-nums;
                font-size: 11px; }
.maplibregl-popup { z-index: 3; }
.maplibregl-popup-content { border-top: 3px solid var(--popup); padding: 10px 12px; }
.maplibregl-popup-content b { color: var(--popup); }
table.popup { border-collapse: collapse; margin-top: 6px; font-size: 12px; }
table.popup th { text-align: left; color: var(--muted); font-weight: 400;
                 padding: 2px 10px 2px 0; white-space: nowrap; }
table.popup td { font-family: var(--mono); padding: 2px 0; word-break: break-word; }
.legend .sw.outline { background: transparent; border: 2px solid var(--boundary); }
.hovercard { position: absolute; pointer-events: none; z-index: 5; display: none;
             background: rgba(26, 26, 26, 0.92); color: #fff; font-size: 12px;
             padding: 5px 9px; border-radius: 3px; max-width: 240px; }
.hovercard .hc-layer { display: block; font-size: 10px; text-transform: uppercase;
             letter-spacing: 0.05em; opacity: 0.75; }
"""


@dataclass(frozen=True)
class MapEntry:
    """One toggleable, coloured layer on the map."""

    key: str  # unique id; also the MapLibre layer-id prefix
    label: str
    color: str
    tileset_url: str
    source_layer: str
    # Restricts this entry to part of a shared tileset, as `category|source`.
    match_key: str | None = None
    feature_count: int | None = None


_MAP_CONTROLS_SCRIPT = """map.addControl(new maplibregl.NavigationControl());
map.addControl(new maplibregl.FullscreenControl());

// Each entry owns three style layers (polygon, line, point), so a checkbox is a
// visibility flip on those three.
const suffixes = ['fill', 'line', 'point'];
const toggles = document.querySelectorAll('.legend input');

function applyToggle(toggle) {
  const visibility = toggle.checked ? 'visible' : 'none';
  if (toggle.dataset.key === 'aoi') {
    if (map.getLayer('aoi-line')) map.setLayoutProperty('aoi-line', 'visibility', visibility);
    return;
  }
  for (const suffix of suffixes) {
    const id = `${toggle.dataset.key}-${suffix}`;
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visibility);
  }
}
toggles.forEach((t) => t.addEventListener('change', () => applyToggle(t)));
document.querySelectorAll('.legend-actions button').forEach((button) => {
  button.addEventListener('click', () => {
    const on = button.dataset.all === 'on';
    toggles.forEach((t) => { t.checked = on; applyToggle(t); });
  });
});

"""

_FEATURE_INSPECTION_SCRIPT = """const layerIds = Object.keys(labelOfLayer);
const hovercard = document.getElementById('hovercard');

// Hovering names the feature; clicking opens everything it carries.
map.on('mousemove', layerIds, (event) => {
  const hit = event.features && event.features[0];
  if (!hit) return;
  map.getCanvas().style.cursor = 'pointer';
  const name = hit.properties.name || '(no name)';
  hovercard.textContent = '';
  const layerName = document.createElement('span');
  layerName.className = 'hc-layer';
  layerName.textContent = labelOfLayer[hit.layer.id];
  const label = document.createElement('b');
  label.textContent = name;
  hovercard.append(layerName, label);
  hovercard.style.display = 'block';
  hovercard.style.left = (event.point.x + 14) + 'px';
  hovercard.style.top = (event.point.y + 14) + 'px';
});
map.on('mouseleave', layerIds, () => {
  map.getCanvas().style.cursor = '';
  hovercard.style.display = 'none';
});

map.on('click', (event) => {
  const hit = map.queryRenderedFeatures(event.point, { layers: layerIds })[0];
  if (!hit) return;
  // Tag values are editable by anyone, so they are written as text, never as markup.
  const content = document.createElement('div');
  const title = document.createElement('b');
  title.textContent = labelOfLayer[hit.layer.id];
  const table = document.createElement('table');
  table.className = 'popup';
  // On a shared tileset, category and source name the layer the title already gives.
  const repeatsTitle = (key) => sharedLayers.has(hit.layer.id) && (key === 'category' || key === 'source');
  Object.entries(hit.properties)
    .filter(([key, value]) => value !== null && value !== '' && !repeatsTitle(key))
    .forEach(([key, value]) => {
      const row = table.insertRow();
      const header = document.createElement('th');
      header.textContent = key;
      row.append(header);
      row.insertCell().textContent = value;
    });
  if (!table.rows.length) table.insertRow().insertCell().textContent = '(no attributes)';
  content.append(title, table);
  new maplibregl.Popup({ maxWidth: '320px' })
    .setLngLat(event.lngLat)
    .setDOMContent(content)
    .addTo(map);
});

"""


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
    *,
    boundary_geojson: str | None = None,
    theme: ThemeConfig | None = None,
) -> str:
    """The canvas, a hover card, and a legend that toggles each entry."""
    if not entries:
        return ""
    outline = _display_boundary(boundary_geojson) if boundary_geojson else None
    script = _map_script(
        entries, boundary_bbox, assets or MapAssetsConfig(), theme or ThemeConfig(), outline
    )
    return (
        '<div class="map-wrap">'
        '<div id="map"></div>'
        '<div class="hovercard" id="hovercard"></div>'
        f"{_render_legend(entries, show_aoi=outline is not None)}"
        "</div>\n"
        f"<script>{script}</script>\n"
    )


def _js_literal(value: object) -> str:
    """`value` as a JavaScript literal; escaping "</" keeps "</script>" in a value from
    closing the inline script element."""
    return json.dumps(value).replace("</", "<\\/")


def _display_boundary(boundary_geojson: str) -> str:
    """The export boundary, thinned only when inlining it whole would bloat the page."""
    if len(boundary_geojson) <= _AOI_INLINE_LIMIT_BYTES:
        return boundary_geojson
    geometry = shape(json.loads(boundary_geojson))
    minx, miny, maxx, maxy = geometry.bounds
    tolerance = max(maxx - minx, maxy - miny) / 4000
    return json.dumps(mapping(geometry.simplify(tolerance, preserve_topology=True)))


def _map_script(
    entries: list[MapEntry],
    boundary_bbox: tuple[float, float, float, float] | None,
    assets: MapAssetsConfig,
    theme: ThemeConfig,
    boundary_geojson: str | None = None,
) -> str:
    source_definitions, layer_definitions = _style_definitions(entries, theme, boundary_geojson)
    labels = _js_literal(
        {layer_id: entry.label for entry in entries for layer_id in _layer_ids(entry)}
    )
    shared = _js_literal(
        [
            layer_id
            for entry in entries
            if entry.match_key is not None
            for layer_id in _layer_ids(entry)
        ]
    )
    return (
        f"""
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', protocol.tile);
const map = new maplibregl.Map({{
  container: 'map',
  style: {{
    version: 8,
    sources: {{
      basemap: {{ type: 'raster', tiles: [{_js_literal(assets.basemap_tiles)}], tileSize: 256,
                  attribution: {_js_literal(assets.basemap_attribution)} }},
      {source_definitions}
    }},
    layers: [
      {{ id: 'basemap', type: 'raster', source: 'basemap',
         paint: {{ 'raster-opacity': 0.6, 'raster-saturation': -1 }} }},
      {layer_definitions}
    ]
  }}
}});
"""
        + _MAP_CONTROLS_SCRIPT
        + f"""// Inspecting a feature is how you check the attribute coverage the table reports.
const labelOfLayer = {labels};
const sharedLayers = new Set({shared});
"""
        + _FEATURE_INSPECTION_SCRIPT
        + f"""// The map is built in a hidden tab, so it is framed the first time it has a size.
let framed = false;
function frameMap() {{
  if (framed || !map.getContainer().clientWidth) return;
  framed = true;
  {_fit_bounds_call(boundary_bbox)}
}}
map.on('load', () => {{ frameMap(); toggles.forEach(applyToggle); }});
"""
        + (_COLLAPSE_LEGEND_ON_PHONES if len(entries) > _PHONE_LEGEND_OPEN_MAX else "")
    )


# A longer legend opens over most of a phone's map, so there it starts closed.
_PHONE_LEGEND_OPEN_MAX = 3
_COLLAPSE_LEGEND_ON_PHONES = """if (window.matchMedia('(max-width: 480px)').matches) {
  document.querySelector('.legend').open = false;
}
"""


def _style_definitions(
    entries: list[MapEntry], theme: ThemeConfig, boundary_geojson: str | None
) -> tuple[str, str]:
    """The style's tileset sources and layers as JS object literals, one per line."""
    tilesets = {entry.tileset_url for entry in entries}
    sources = {f"src{index}": url for index, url in enumerate(sorted(tilesets))}
    source_of = {url: name for name, url in sources.items()}

    source_definitions = ",\n      ".join(
        f"{name}: {{ type: 'vector', url: {_js_literal(f'pmtiles://{url}')} }}"
        for name, url in sources.items()
    )
    definitions = {
        entry.key: _layer_definitions(entry, source_of[entry.tileset_url]) for entry in entries
    }
    largest_first = sorted(entries, key=lambda entry: entry.feature_count or 0, reverse=True)
    # Fills, then lines, then points largest first, so small layers stay on top to click.
    layer_definitions = ",\n      ".join(
        [definitions[entry.key].fill for entry in entries]
        + [definitions[entry.key].line for entry in entries]
        + [definitions[entry.key].point for entry in largest_first]
    )
    if boundary_geojson:
        source_definitions += f",\n      aoi: {{ type: 'geojson', data: {boundary_geojson} }}"
        # Drawn after the data layers so the outline stays readable over them.
        layer_definitions += (
            ",\n      { id: 'aoi-line', type: 'line', source: 'aoi',"
            f" paint: {{ 'line-color': {_js_literal(theme.boundary_outline_color)},"
            " 'line-width': 2 } }"
        )
    return source_definitions, layer_definitions


def _fit_bounds_call(boundary_bbox: tuple[float, float, float, float] | None) -> str:
    """Frame on the export boundary: features are selected whole, so a long road or river
    crossing the boundary would otherwise stretch the view past the export."""
    if not boundary_bbox:
        return ""
    return (
        f"map.fitBounds([[{boundary_bbox[0]},{boundary_bbox[1]}],"
        f"[{boundary_bbox[2]},{boundary_bbox[3]}]], {{padding: 24, duration: 0}});"
    )


# Thin at country zoom, readable at street zoom, where an event page is read.
_LINE_WIDTH = "['interpolate', ['linear'], ['zoom'], 6, 0.8, 10, 1.4, 14, 2.5]"
_POINT_RADIUS = "['interpolate', ['linear'], ['zoom'], 6, 3, 12, 4, 16, 6]"


class LayerDefinitions(NamedTuple):
    fill: str
    line: str
    point: str


_LAYER_KINDS = LayerDefinitions._fields


def _layer_ids(entry: MapEntry) -> list[str]:
    return [f"{entry.key}-{kind}" for kind in _LAYER_KINDS]


def _layer_definitions(entry: MapEntry, source: str) -> LayerDefinitions:
    common = f"source: '{source}', 'source-layer': {_js_literal(entry.source_layer)}"
    color = _js_literal(entry.color)
    fill_id, line_id, point_id = (_js_literal(layer_id) for layer_id in _layer_ids(entry))
    return LayerDefinitions(
        fill=f"{{ id: {fill_id}, type: 'fill', {common},"
        f" filter: {_filter(entry, 'Polygon')},"
        f" paint: {{ 'fill-color': {color}, 'fill-opacity': 0.5,"
        f" 'fill-outline-color': {color} }} }}",
        line=f"{{ id: {line_id}, type: 'line', {common},"
        f" filter: {_filter(entry, 'LineString')},"
        f" paint: {{ 'line-color': {color}, 'line-width': {_LINE_WIDTH} }} }}",
        point=f"{{ id: {point_id}, type: 'circle', {common},"
        f" filter: {_filter(entry, 'Point')},"
        f" paint: {{ 'circle-color': {color}, 'circle-radius': {_POINT_RADIUS},"
        f" 'circle-stroke-width': 1, 'circle-stroke-color': '#1a1a1a' }} }}",
    )


def _filter(entry: MapEntry, geometry: str) -> str:
    geometry_test = f"['==', ['geometry-type'], '{geometry}']"
    if entry.match_key is None:
        return geometry_test
    key_expression = "['concat', ['get', 'category'], '|', ['get', 'source']]"
    return f"['all', {geometry_test}, ['==', {key_expression}, {_js_literal(entry.match_key)}]]"


def _render_legend(entries: list[MapEntry], *, show_aoi: bool = False) -> str:
    """Collapsible, because on a phone it would otherwise cover the map it explains."""
    aoi_item = (
        "<li><label>"
        '<input type="checkbox" checked data-key="aoi">'
        f'<span class="sw outline"></span>{escape(AOI_LABEL)}'
        "</label></li>"
        if show_aoi
        else ""
    )
    items = aoi_item + "".join(
        "<li><label>"
        f'<input type="checkbox" checked data-key="{escape(entry.key)}">'
        f'<span class="sw" style="background:{escape(entry.color)}"></span>{escape(entry.label)}'
        f"{_count_label(entry.feature_count)}"
        "</label></li>"
        for entry in entries
    )
    actions = (
        '<div class="legend-actions">'
        '<button type="button" data-all="on">Select all</button>'
        '<button type="button" data-all="off">Clear all</button>'
        "</div>"
        if len(entries) > 1
        else ""
    )
    return (
        '<details class="legend" open>'
        f"<summary>Layers ({len(entries)})</summary>"
        f"{actions}"
        f"<ul>{items}</ul>"
        "</details>"
    )


def _count_label(feature_count: int | None) -> str:
    if feature_count is None:
        return ""
    if feature_count >= 1000:
        shown = f"{feature_count / 1000:.0f}k"
    else:
        shown = str(feature_count)
    return f'<span class="legend-count">{shown}</span>'
