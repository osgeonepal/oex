"""The page a dataset publishes: its layers, their attributes, and a map of them."""

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any

from oex.config.schema import MapAssetsConfig, ThemeConfig
from oex.naming import ASSET_TITLES
from oex.report.html import (
    BASE_CSS,
    SourceMetadata,
    format_int,
    outbound_link,
    panel_sections,
    theme_tokens,
)
from oex.report.map_block import MAP_CSS, MapEntry, head_scripts, render_map
from oex.sources.labels import long_label

_SHELL_CSS = """
body { max-width: 1180px; }
.ds-head { margin: 0 0 16px; }
.ds-head h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: -0.01em; }
.ds-meta { color: var(--muted); font-size: 13px; margin: 0; }
.ds-meta b { color: var(--fg); font-weight: 600; }
.ds-meta a { color: inherit; }
.related { font-size: 13px; color: var(--muted); margin: -8px 0 16px; line-height: 1.7; }
.related a { color: var(--fg); }
.switcher { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin: 0 0 20px; }
.switcher label { padding: 9px 16px; cursor: pointer; color: var(--tab-idle); font-weight: 600;
                  font-size: 13px; border-bottom: 2px solid transparent; margin-bottom: -1px; }
.switcher label:hover { color: var(--fg); }
.view { display: none; }
.split { display: grid; grid-template-columns: 252px 1fr; gap: 24px; align-items: start; }
.split.single { grid-template-columns: minmax(0, 1fr); }
.layer-views { min-width: 0; }
.layer-list { border: 1px solid var(--line); border-radius: 3px; overflow: hidden;
              position: sticky; top: 14px; }
.layer-list label { display: flex; align-items: center; gap: 8px; padding: 9px 12px;
                    cursor: pointer; border-bottom: 1px solid var(--line); font-size: 13px; }
.layer-list label:last-child { border-bottom: 0; }
.layer-list label:hover { background: #fafafa; }
.layer-list .lc { margin-left: auto; font-variant-numeric: tabular-nums; font-size: 11px;
                  color: var(--muted); }
.layer-view { display: none; }
.detail-head { display: flex; align-items: center; justify-content: space-between; gap: 16px;
               flex-wrap: wrap; padding: 0 0 13px; border-bottom: 1px solid var(--line);
               margin: 0 0 18px; }
.detail-head h3 { margin: 0; font-size: 16px; }
select.fmt { font: inherit; font-size: 12px; padding: 5px 7px; border: 1px solid var(--line);
             border-radius: 3px; background: var(--bg); color: var(--fg); max-width: 240px; }
a.dl-btn { display: inline-block; margin-left: 6px; padding: 6px 13px; font-size: 12px;
           font-weight: 600; color: #fff; background: var(--accent); border-radius: 3px;
           text-decoration: none; }
a.dl-btn:hover { filter: brightness(0.9); }
a.dl-btn:focus-visible, select.fmt:focus-visible { outline: 2px solid var(--accent);
           outline-offset: 2px; }
.table-scroll { overflow-x: auto; }
@media (max-width: 860px) {
  body { padding: 18px 14px 44px; }
  .kpis { grid-template-columns: repeat(2, 1fr); }
  .split { grid-template-columns: minmax(0, 1fr); gap: 16px; }
  .about { grid-template-columns: minmax(0, 1fr); }
  .layer-list { position: static; max-height: 210px; overflow-y: auto; }
  #map { height: 62vh; min-height: 320px; }
  .switcher label { flex: 1; text-align: center; padding: 10px 8px; }
}
@media (max-width: 480px) {
  .kpis { grid-template-columns: 1fr; }
  .ds-head h1 { font-size: 18px; }
  .legend { left: 8px; right: 8px; bottom: 8px; max-height: 45%; }
  .legend li { white-space: normal; }
  a.dl-btn, select.fmt { font-size: 13px; padding: 8px 12px; }
  select.fmt { max-width: none; flex: 1; }
  .detail-head .detail-dl { display: flex; width: 100%; gap: 6px; }
  a.dl-btn { margin-left: 0; }
}
"""

_SCRIPT = """<script>
document.querySelectorAll('select.fmt').forEach((select) => {
  select.addEventListener('change', () => {
    select.nextElementSibling.setAttribute('href', select.value);
  });
});
const mapTab = document.getElementById('view-map');
// The map is laid out while its view is hidden, so it needs its size back when shown.
if (mapTab) mapTab.addEventListener('change', () => map.resize());
</script>
"""


@dataclass(frozen=True)
class Download:
    format: str
    href: str
    size: int | None = None

    @property
    def label(self) -> str:
        """`polygons_shp` reads "Shapefile (polygons)", with the size when it is known."""
        partition, _, fmt = self.format.rpartition("_")
        title = ASSET_TITLES.get(fmt, fmt.upper())
        details = [part for part in (partition, _file_size(self.size)) if part]
        return f"{title} ({', '.join(details)})" if details else title


def _file_size(size: int | None) -> str:
    if size is None:
        return ""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def downloads_from_assets(assets: Mapping[str, Mapping[str, Any]]) -> list[Download]:
    """A layer's STAC assets as the formats a reader is offered, in a stable order."""
    order = list(ASSET_TITLES)

    def rank(key: str) -> tuple[int, str]:
        fmt = key.rpartition("_")[2]
        return (order.index(fmt) if fmt in order else len(order), key)

    return [
        Download(format=key, href=asset["href"], size=asset.get("file:size"))
        for key, asset in sorted(assets.items(), key=lambda kv: rank(kv[0]))
        if asset.get("href")
    ]


@dataclass(frozen=True)
class PageLayer:
    key: str
    label: str
    color: str
    source: SourceMetadata
    downloads: list[Download]
    # (url, source layer). Shared across a combined dataset, one per source otherwise.
    tileset: tuple[str, str] | None = None
    # Restricts this layer to part of a shared tileset, as `category|source`.
    match_key: str | None = None
    metadata_href: str | None = None


def render_dataset_page(
    *,
    title: str,
    layers: list[PageLayer],
    theme: ThemeConfig | None = None,
    boundary_bbox: tuple[float, float, float, float] | None = None,
    boundary_geojson: str | None = None,
    map_assets: MapAssetsConfig | None = None,
    dataset_url: str | None = None,
    related: list[tuple[str, str]] | None = None,
) -> str:
    """One dataset as one page, whether it carries one layer or twenty. `related` links the
    area's other datasets as (label, href)."""
    if not layers:
        raise ValueError("render_dataset_page needs at least one layer")
    theme = theme or ThemeConfig()
    map_html = _map(layers, boundary_bbox, boundary_geojson, theme, map_assets)
    views = [("data", "Data"), ("map", "Map")] if map_html else [("data", "Data")]

    body = (
        _view_inputs(views)
        + _view_bar(views)
        + '<div class="views">'
        + f'<section class="view view-data">{_data_view(layers, dataset_url)}</section>'
        + (f'<section class="view view-map">{map_html}</section>' if map_html else "")
        + "</div>"
    )
    scripts = head_scripts(map_assets) if map_html else ""
    styles = f"{BASE_CSS}{MAP_CSS}{_SHELL_CSS}{theme_tokens(theme)} {_view_css(views)} {_layer_css(layers)}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        # Without this the browser requests /favicon.ico and logs a 404.
        '<link rel="icon" href="data:,">\n'
        f"<title>{escape(title)}</title>\n"
        f"{scripts}"
        f"<style>{styles}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{_header(title, layers)}{_related_line(related or [])}"
        f"{body}\n"
        f"{_SCRIPT}"
        "</body>\n"
        "</html>\n"
    )


def _header(title: str, layers: list[PageLayer]) -> str:
    """Counts, then each source with its snapshot, then each licence once."""
    total = sum(layer.source.metadata.feature_count for layer in layers)
    count = f"<b>{len(layers)}</b> layers &middot; " if len(layers) > 1 else ""
    snapshots = dict.fromkeys(
        f"{long_label(layer.source.source_name)} {layer.source.snapshot_label}" for layer in layers
    )
    licences = dict.fromkeys(
        (layer.source.license_label, layer.source.license_url) for layer in layers
    )
    licence_html = " &middot; ".join(
        outbound_link(url, escape(label)) if url else escape(label) for label, url in licences
    )
    meta = [f"{count}<b>{format_int(total)}</b> features", *map(escape, snapshots), licence_html]
    return (
        '<div class="ds-head">'
        f"<h1>{escape(title)}</h1>"
        f'<p class="ds-meta">{" &middot; ".join(meta)}</p>'
        "</div>\n"
    )


def _view_inputs(views: list[tuple[str, str]]) -> str:
    return "".join(
        _radio("view", f"view-{key}", checked=index == 0, alone=len(views) == 1)
        for index, (key, _label) in enumerate(views)
    )


def _radio(group: str, input_id: str, *, checked: bool, alone: bool) -> str:
    """A lone choice has no label to reach it by, so it is disabled to keep it out of the
    tab order; a disabled radio still matches `:checked`."""
    flags = (" checked" if checked else "") + (" disabled" if alone else "")
    return f'<input type="radio" name="{group}" id="{escape(input_id)}" class="tab-input"{flags}>'


def _view_bar(views: list[tuple[str, str]]) -> str:
    if len(views) == 1:
        return ""
    return (
        '<div class="switcher">'
        + "".join(f'<label for="view-{key}">{escape(label)}</label>' for key, label in views)
        + "</div>"
    )


def _view_css(views: list[tuple[str, str]]) -> str:
    active = ", ".join(
        f'#view-{key}:checked ~ .switcher label[for="view-{key}"]' for key, _ in views
    )
    shown = " ".join(
        f"#view-{key}:checked ~ .views .view-{key} {{ display: block; }}" for key, _ in views
    )
    focused = ", ".join(
        f'#view-{key}:focus-visible ~ .switcher label[for="view-{key}"]' for key, _ in views
    )
    return (
        f"{active} {{ color: var(--tab-active); border-bottom-color: var(--accent); }} "
        f"{focused} {{ outline: 2px solid var(--accent); outline-offset: 2px; }} {shown}"
    )


def _related_line(related: list[tuple[str, str]]) -> str:
    if not related:
        return ""
    links = " &middot; ".join(outbound_link(href, escape(label)) for label, href in related)
    return f'<p class="related">More for this area: {links}</p>\n'


def _data_view(layers: list[PageLayer], dataset_url: str | None) -> str:
    single = len(layers) == 1
    inputs = "".join(
        _radio("layer", f"layer-{layer.key}", checked=index == 0, alone=single)
        for index, layer in enumerate(layers)
    )
    listing = (
        ""
        if single
        else (
            '<div class="layer-list">'
            + "".join(
                f'<label for="layer-{escape(layer.key)}">'
                f'<span class="sw" style="background:{escape(layer.color)}"></span>{escape(layer.label)}'
                f'<span class="lc">{format_int(layer.source.metadata.feature_count)}</span></label>'
                for layer in layers
            )
            + "</div>"
        )
    )
    panels = "".join(
        f'<div class="layer-view lv-{escape(layer.key)}">'
        f"{_detail_head(layer, named=not single)}"
        f"{panel_sections(layer.source, _code_links(layer), dataset_url)}</div>"
        for layer in layers
    )
    split = "split single" if single else "split"
    return f'{inputs}<div class="{split}">{listing}<div class="layer-views">{panels}</div></div>'


def _detail_head(layer: PageLayer, *, named: bool) -> str:
    """A single-layer page is titled by the layer already, so only the download shows."""
    heading = f"<h3>{escape(layer.label)}</h3>" if named else ""
    downloads = _download_controls(layer)
    if not heading and not downloads:
        return ""
    return f'<div class="detail-head">{heading}<div class="detail-dl">{downloads}</div></div>'


def _code_links(layer: PageLayer) -> list[tuple[str, str]]:
    links = [("STAC Item", layer.metadata_href)]
    links += [("GeoParquet", d.href) for d in layer.downloads if d.format == "geoparquet"]
    links.append(("Vector tiles (PMTiles)", layer.tileset[0] if layer.tileset else None))
    return [(label, href) for label, href in links if href]


def _download_controls(layer: PageLayer) -> str:
    if not layer.downloads:
        return ""
    options = "".join(
        f'<option value="{escape(d.href)}">{escape(d.label)}</option>' for d in layer.downloads
    )
    return (
        f'<select class="fmt" aria-label="Download format for {escape(layer.label)}">'
        f"{options}</select>"
        f'<a class="dl-btn" href="{escape(layer.downloads[0].href)}" download>Download</a>'
    )


def _layer_css(layers: list[PageLayer]) -> str:
    shown = " ".join(
        f"#layer-{layer.key}:checked ~ .split .lv-{layer.key} {{ display: block; }}"
        for layer in layers
    )
    selected = ", ".join(
        f'#layer-{layer.key}:checked ~ .split .layer-list label[for="layer-{layer.key}"]'
        for layer in layers
    )
    focused = ", ".join(
        f'#layer-{layer.key}:focus-visible ~ .split .layer-list label[for="layer-{layer.key}"]'
        for layer in layers
    )
    return (
        f"{shown} {selected} {{ background: color-mix(in srgb, var(--accent) 10%, white);"
        " font-weight: 600; box-shadow: inset 3px 0 0 var(--accent); } "
        f"{focused} {{ outline: 2px solid var(--accent); outline-offset: -2px; }}"
    )


def _map(
    layers: list[PageLayer],
    boundary_bbox: tuple[float, float, float, float] | None,
    boundary_geojson: str | None,
    theme: ThemeConfig,
    map_assets: MapAssetsConfig | None,
) -> str:
    entries = [
        MapEntry(
            key=layer.key,
            label=layer.label,
            color=layer.color,
            tileset_url=layer.tileset[0],
            source_layer=layer.tileset[1],
            match_key=layer.match_key,
            feature_count=layer.source.metadata.feature_count,
        )
        for layer in layers
        if layer.tileset is not None
    ]
    return render_map(
        entries,
        boundary_bbox,
        map_assets,
        boundary_geojson=boundary_geojson,
        theme=theme,
    )
