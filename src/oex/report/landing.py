"""Combined-dataset landing page: a layers-and-data-quality table, then a map.

Rendered as the HDX custom viz when several categories are published onto one
dataset. The table carries one row per layer (source colour swatch, feature
count, named coverage, geometry mix, top categories); the interactive map comes
last and is coloured to match the swatches. The map is shown only when a
combined PMTiles resource exists.
"""

from dataclasses import dataclass
from html import escape

from oex.config.schema import MapAssetsConfig
from oex.metadata import ColumnReport, MetadataReport
from oex.palette import DEFAULT_PALETTE
from oex.report.html import _CSS, SourceMetadata, _fmt_int
from oex.report.map_block import MAP_CSS, MapEntry, head_scripts, render_map
from oex.report.quality import LayerQuality, layer_quality

_LANDING_CSS = """
.landing-head h1 { font-size: 22px; margin: 0 0 6px; }
.landing-sub { color: var(--muted); font-size: 13px; margin: 0 0 8px; }
table.layers td, table.layers th { vertical-align: top; }
td.layer-name { font-weight: 600; white-space: nowrap; }
td.geom { font-size: 12px; color: var(--muted); }
td.cov { font-size: 12px; color: var(--muted); white-space: nowrap; }
.cov-bar { display: inline-flex; width: 84px; height: 8px; border-radius: 2px;
           overflow: hidden; background: var(--bar-bg); vertical-align: middle;
           margin-right: 7px; }
.cov-bar > i { display: block; height: 100%; }
i.cov-well { background: #22c55e; }
i.cov-partial { background: #eab308; }
i.cov-rare { background: #ef4444; }
.cov-note { font-size: 11px; color: var(--muted); margin: 10px 0 0; }
tr.layer-row { cursor: pointer; }
tr.layer-row .caret { display: inline-block; width: 9px; color: var(--muted);
                      transition: transform 0.15s; }
tr.layer-row.open .caret { transform: rotate(90deg); }
tr.attr-row > td { background: #fafafa; padding: 4px 10px 10px 26px; }
table.attr-detail { border-collapse: collapse; font-size: 11px; width: 100%; }
table.attr-detail th { text-align: left; color: var(--muted); font-weight: 400;
                       padding: 2px 12px 4px 0; }
table.attr-detail td { padding: 2px 12px 2px 0; vertical-align: top; }
table.attr-detail td.k { font-family: var(--mono); white-space: nowrap; }
table.attr-detail td.f { font-family: var(--mono); color: var(--muted); white-space: nowrap; }
table.attr-detail code { font-family: var(--mono); background: var(--bg);
                         border: 1px solid var(--line); border-radius: 2px; padding: 0 3px; }
table.attr-detail .count { color: var(--muted); }
.attr-list { display: flex; flex-wrap: wrap; gap: 6px; }
.attr-list .attr { font-family: var(--mono); font-size: 11px; background: var(--bg);
                   border: 1px solid var(--line); border-radius: 3px; padding: 2px 7px; }
.attr-list .attr i { color: var(--muted); font-style: normal; margin-left: 6px; }
"""

_ATTR_COLSPAN = 6


@dataclass(frozen=True)
class CategoryPanel:
    slug: str
    label: str
    sources: list[SourceMetadata]


@dataclass(frozen=True)
class _Row:
    slug: str
    label: str
    color: str
    source: SourceMetadata


def render_landing(
    *,
    title: str,
    subtitle: str,
    panels: list[CategoryPanel],
    pmtiles_url: str | None,
    pmtiles_layer: str | None,
    boundary_bbox: tuple[float, float, float, float] | None = None,
    boundary_geojson: str | None = None,
    palette: list[str] | None = None,
    map_assets: MapAssetsConfig | None = None,
) -> str:
    """Render the combined landing page. `panels` must be in the intended display order."""
    if not panels:
        raise ValueError("render_landing needs at least one category panel")
    colors = palette or DEFAULT_PALETTE

    # One colour per (category, source) row, so a merged single-layer tileset can
    # still be read per category and per source on the map and in the swatches.
    flat = [(p, s) for p in panels for s in p.sources]
    rows = [
        _Row(slug=p.slug, label=p.label, color=colors[i % len(colors)], source=s)
        for i, (p, s) in enumerate(flat)
    ]

    table = _render_table(rows)
    entries = _map_entries(rows, pmtiles_url, pmtiles_layer)
    map_block = render_map(
        entries,
        boundary_bbox or _overall_bounds(rows),
        map_assets,
        boundary_geojson=boundary_geojson,
    )
    scripts = head_scripts(map_assets) if entries else ""

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        # Without this the browser requests /favicon.ico from whatever host serves
        # the page and logs a 404 when the bucket has none.
        '<link rel="icon" href="data:,">\n'
        f"<title>{escape(title)}</title>\n"
        f"{scripts}"
        f"<style>{_CSS}{MAP_CSS}{_LANDING_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="landing-head">'
        f'<div class="landing-sub">{escape(subtitle)}</div>'
        "</div>\n"
        "<h2>Layers and data quality</h2>\n"
        f"{table}\n"
        f"{map_block}"
        f"{_ATTR_TOGGLE_JS}"
        "</body>\n"
        "</html>\n"
    )


_ATTR_TOGGLE_JS = """<script>
document.querySelectorAll('tr.layer-row').forEach((row) => {
  row.addEventListener('click', () => {
    const detail = row.nextElementSibling;
    if (!detail || !detail.classList.contains('attr-row')) return;
    if (detail.hasAttribute('hidden')) { detail.removeAttribute('hidden'); row.classList.add('open'); }
    else { detail.setAttribute('hidden', ''); row.classList.remove('open'); }
  });
});
</script>
"""


def _render_table(rows: list[_Row]) -> str:
    head = (
        "<thead><tr>"
        "<th>Layer</th><th>License</th>"
        '<th class="num">Features</th><th class="num">Named</th>'
        "<th>Attribute coverage</th><th>Geometry</th>"
        "</tr></thead>"
    )
    body = "".join(_render_row(r) for r in rows)
    legend = (
        '<p class="cov-note">Attribute coverage: how many of the layer\'s columns carry data. '
        "Green is well-populated (50% of rows or more), amber partial, red rare (under 25%). "
        "The per-layer report breaks the same measure down column by column.</p>"
    )
    return f'<table class="layers">{head}<tbody>{body}</tbody></table>{legend}'


def _row_label(row: _Row) -> str:
    return f"{row.label} ({source_label(row.source.source_name)})"


def _render_row(row: _Row) -> str:
    meta = row.source.metadata
    quality = layer_quality(meta)
    named_cell = "n/a" if quality.named_percent is None else f"{quality.named_percent:.0f}%"
    license_text = escape(row.source.license_label or "n/a")
    if row.source.license_url:
        license_text = f'<a href="{escape(row.source.license_url)}">{license_text}</a>'
    caret = '<span class="caret">&#9656;</span> ' if meta.columns else ""
    main = (
        '<tr class="layer-row">'
        f'<td class="layer-name">{caret}<span class="sw" style="background:{row.color}"></span>'
        f"{escape(_row_label(row))}</td>"
        f"<td>{license_text}</td>"
        f'<td class="num">{_fmt_int(meta.feature_count)}</td>'
        f'<td class="num">{named_cell}</td>'
        f'<td class="cov">{_coverage_bar(quality)}</td>'
        f'<td class="geom">{_geometry_summary(meta)}</td>'
        "</tr>"
    )
    return main + _render_attr_row(meta)


def _render_attr_row(meta: MetadataReport) -> str:
    """A hidden detail row: every attribute column, how full it is and what is in it.

    The commonest values are the quickest way to judge whether a column is usable,
    so they belong next to the coverage figure rather than only in the per-layer report.
    """
    if not meta.columns:
        return ""
    rows = "".join(
        "<tr>"
        f'<td class="k">{escape(col.name)}</td>'
        f'<td class="f">{max(0.0, 100.0 - col.null_percent):.0f}%</td>'
        f'<td class="f">{_fmt_int(col.distinct_count)}</td>'
        f"<td>{_top_values(col)}</td>"
        "</tr>"
        for col in meta.columns
    )
    head = (
        "<thead><tr><th>Column</th><th>Filled</th><th>Distinct</th>"
        "<th>Most common values</th></tr></thead>"
    )
    return (
        '<tr class="attr-row" hidden>'
        f'<td colspan="{_ATTR_COLSPAN}">'
        f'<table class="attr-detail">{head}<tbody>{rows}</tbody></table>'
        "</td></tr>"
    )


def _top_values(col: ColumnReport) -> str:
    if not col.top_values:
        if col.distinct_count == 0:
            return "all null"
        return "n/a"
    parts = []
    for entry in col.top_values:
        value = entry.get("value")
        shown = "(blank)" if value is None or value == "" else str(value)
        if len(shown) > 40:
            shown = shown[:39] + "\u2026"
        count = _fmt_int(int(entry.get("count", 0)))
        parts.append(f'<code>{escape(shown)}</code> <span class="count">{count}</span>')
    return " &middot; ".join(parts)


def _coverage_bar(quality: LayerQuality) -> str:
    total = quality.total_columns
    if total == 0:
        return "n/a"
    segments = "".join(
        f'<i class="cov-{name}" style="width:{count / total * 100:.1f}%"></i>'
        for name, count in (
            ("well", quality.well),
            ("partial", quality.partial),
            ("rare", quality.rare),
        )
        if count
    )
    return f'<span class="cov-bar">{segments}</span>{quality.well}/{total} populated'


def _geometry_summary(meta: MetadataReport) -> str:
    if not meta.geometry_types:
        return "n/a"
    items = sorted(meta.geometry_types.items(), key=lambda kv: kv[1], reverse=True)
    return ", ".join(f"{_pretty_geom(t)} {_fmt_int(c)}" for t, c in items)


def _pretty_geom(name: str) -> str:
    return name.replace("ST_", "").replace("_", " ").capitalize()


def _map_entries(
    rows: list["_Row"],
    pmtiles_url: str | None,
    pmtiles_layer: str | None,
) -> list[MapEntry]:
    """One entry per layer, all pointing at the single merged tileset.

    The merged tileset holds every layer, so each entry restricts itself to its
    own features with a `category|source` match.
    """
    if not (pmtiles_url and pmtiles_layer):
        return []
    return [
        MapEntry(
            key=f"{r.slug}-{r.source.source_name}",
            label=_row_label(r),
            color=r.color,
            tileset_url=pmtiles_url,
            source_layer=pmtiles_layer,
            match_key=f"{r.slug}|{r.source.source_name}",
            feature_count=r.source.metadata.feature_count,
        )
        for r in rows
    ]


def _overall_bounds(
    rows: list["_Row"],
) -> tuple[float, float, float, float] | None:
    boxes = [r.source.metadata.bbox for r in rows if r.source.metadata.bbox is not None]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def source_label(name: str) -> str:
    if name == "osm":
        return "OSM"
    if name == "overture":
        return "Overture"
    return name.title()
