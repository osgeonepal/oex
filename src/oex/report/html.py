"""Multi-source HTML report renderer.

Pure stdlib (no Jinja, no Plotly, no JS, no external CDN). When more
than one source is supplied, the result has CSS-only tabs (hidden radios
+ sibling selectors) so users can switch between Overture and OSM views
inside the HDX customviz iframe.
"""

from dataclasses import dataclass
from html import escape
from typing import Any

from oex.metadata import ColumnReport, MetadataReport

_PCODE_PREFIX = "adm"
_PCODE_SUFFIX = "_pcode"
_SOURCE_COL = "source"
# Coverage buckets (filled-percent thresholds). High = green, mid = amber, low = red.
_COVERAGE_HIGH = 80.0
_COVERAGE_MID = 50.0


@dataclass(frozen=True)
class SourceMetadata:
    """One source's payload for the report (and the metadata.json resource).

    `metadata` is the runtime `MetadataReport` for the rendering path; the
    JSON serialisation lives in `to_payload` / `from_payload`.
    """

    source_name: str
    snapshot_label: str
    dataset_source: str
    generated_utc: str
    oex_version: str
    license_label: str
    license_url: str | None
    pcode_source_date: str | None
    metadata: MetadataReport

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "snapshot_label": self.snapshot_label,
            "dataset_source": self.dataset_source,
            "generated_utc": self.generated_utc,
            "oex_version": self.oex_version,
            "license_label": self.license_label,
            "license_url": self.license_url,
            "pcode_source_date": self.pcode_source_date,
            "metadata": self.metadata.to_dict(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SourceMetadata":
        meta = payload["metadata"]
        bbox_raw = meta.get("bbox")
        bbox = tuple(bbox_raw) if bbox_raw is not None else None
        columns = [ColumnReport(**c) for c in meta["columns"]]
        report = MetadataReport(
            feature_count=meta["feature_count"],
            geometry_types=dict(meta["geometry_types"]),
            bbox=bbox,  # type: ignore[arg-type]
            columns=columns,
            summary=meta["summary"],
        )
        return cls(
            source_name=payload["source_name"],
            snapshot_label=payload["snapshot_label"],
            dataset_source=payload["dataset_source"],
            generated_utc=payload["generated_utc"],
            oex_version=payload["oex_version"],
            license_label=payload["license_label"],
            license_url=payload.get("license_url"),
            pcode_source_date=payload.get("pcode_source_date"),
            metadata=report,
        )


_CSS = """
:root {
  --fg: #1a1a1a;
  --muted: #6b6b6b;
  --line: #e4e4e4;
  --bg: #ffffff;
  --bar: #2563eb;
  --bar-bg: #f1f5f9;
  --tab-active: #1a1a1a;
  --tab-idle: #6b6b6b;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); }
body {
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  max-width: 980px;
  margin: 0 auto;
  padding: 28px 24px 64px;
}
.tab-input { position: absolute; opacity: 0; pointer-events: none; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line);
        margin: 0 0 24px; }
.tabs label { padding: 10px 18px; cursor: pointer; color: var(--tab-idle);
              font-weight: 600; font-size: 13px; letter-spacing: 0.02em;
              border-bottom: 2px solid transparent; margin-bottom: -1px; }
.tabs label:hover { color: var(--fg); }
.panel { display: none; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em;
     color: var(--muted); margin: 32px 0 10px; font-weight: 600; }
.panel > h2:first-child { margin-top: 0; }
.panel > .quality:first-child + h2 { margin-top: 24px; }
.quality { padding: 14px 16px; background: #fafafa; border-left: 3px solid var(--bar);
           border-radius: 2px; margin: 0; }
.quality-strip { display: flex; gap: 2px; margin: 0 0 10px; }
.qc { flex: 1; height: 24px; min-width: 6px; border-radius: 2px; display: inline-block; }
.qc-high { background: #22c55e; }
.qc-mid  { background: #eab308; }
.qc-low  { background: #ef4444; }
.quality-caption { font-size: 13px; color: var(--fg); }
.quality-hint { color: var(--muted); font-size: 12px; }
.quality-legend { font-size: 11px; color: var(--muted); margin-top: 8px;
                  display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.quality-legend .qc { width: 12px; height: 12px; flex: none; min-width: 12px;
                      vertical-align: middle; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
        background: var(--line); margin: 18px 0 0; border: 1px solid var(--line); }
.kpi { background: var(--bg); padding: 14px 16px; }
.kpi-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
             color: var(--muted); }
.kpi-value { font: 600 20px/1.2 var(--mono); margin-top: 4px; }
.kpi-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.04em; }
td.num, th.num { text-align: right; font-family: var(--mono); }
td.col-name { font-family: var(--mono); }
td.col-type { color: var(--muted); font-family: var(--mono); font-size: 12px; }
.bar { background: var(--bar-bg); border-radius: 2px; height: 6px;
       position: relative; overflow: hidden; min-width: 80px; }
.bar > i { display: block; height: 100%; background: var(--bar); }
td.coverage { text-align: right; min-width: 110px; }
td.coverage .coverage-pct { font-family: var(--mono); font-size: 13px; }
td.coverage .bar { margin-top: 4px; min-width: 0; height: 4px; }
.top-values { color: var(--muted); font-size: 12px; }
.top-values code { font-family: var(--mono); color: var(--fg);
                   background: #f4f4f5; padding: 1px 5px; border-radius: 3px; }
.top-values .count { color: var(--muted); }
.bbox { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.tab-footer { margin-top: 36px; padding-top: 14px; border-top: 1px solid var(--line);
              color: var(--muted); font-size: 12px; line-height: 1.55; }
.tab-footer a { color: inherit; }
"""


def render_report(sources: dict[str, SourceMetadata]) -> str:
    if not sources:
        raise ValueError("render_report needs at least one source")

    ordered = _ordered_source_names(sources)
    default = _default_source_name(sources, ordered)

    radios = "".join(
        '<input type="radio" name="src" '
        f'id="tab-{escape(name)}" class="tab-input"'
        f"{' checked' if name == default else ''}>"
        for name in ordered
    )
    nav = (
        '<div class="tabs">'
        + "".join(
            f'<label for="tab-{escape(name)}">{escape(_pretty_source_name(name))}</label>'
            for name in ordered
        )
        + "</div>"
    )
    panels = "".join(_render_panel(name, sources[name]) for name in ordered)
    show_rules = " ".join(
        f"#tab-{name}:checked ~ .panels .panel-{name} {{ display: block; }}" for name in ordered
    )
    active_label_rules = ", ".join(
        f'#tab-{name}:checked ~ .tabs label[for="tab-{name}"]' for name in ordered
    )
    dynamic_css = (
        f"{active_label_rules} "
        "{ color: var(--tab-active); border-bottom-color: var(--bar); } "
        f"{show_rules}"
    )

    body = radios + (nav if len(ordered) > 1 else "") + f'<div class="panels">{panels}</div>'

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>oex report</title>\n"
        f"<style>{_CSS}{dynamic_css}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _render_panel(name: str, source: SourceMetadata) -> str:
    metadata = source.metadata
    sections = [
        _render_quality(metadata),
        _render_kpis(metadata),
        _render_geometry_types(metadata),
        _render_source_mix(metadata),
        _render_pcode_coverage(metadata),
        _render_attribute_table(metadata),
        _render_footer(source),
    ]
    inner = "\n".join(s for s in sections if s)
    return f'<section class="panel panel-{escape(name)}">{inner}</section>'


def _render_quality(metadata: MetadataReport) -> str:
    if not metadata.columns:
        return ""
    cells = []
    for col in metadata.columns:
        coverage = max(0.0, min(100.0, 100.0 - col.null_percent))
        bucket = _coverage_bucket(coverage)
        title = f"{col.name} ({coverage:.2f}% filled)"
        cells.append(f'<span class="qc qc-{bucket}" title="{escape(title)}"></span>')
    sparse_count = sum(1 for c in metadata.columns if c.null_percent >= _COVERAGE_MID)
    total = len(metadata.columns)
    if sparse_count == 0:
        caption = f"All {total} attribute columns are at least 50% filled."
    else:
        caption = f"{sparse_count} of {total} attribute columns are over 50% null."
    return (
        '<div class="quality">'
        f'<div class="quality-strip">{"".join(cells)}</div>'
        f'<div class="quality-caption">{caption} '
        '<span class="quality-hint">Hover a cell to see the column.</span>'
        "</div>"
        '<div class="quality-legend">'
        '<span class="qc qc-high"></span>over 80% filled '
        '<span class="qc qc-mid"></span>50 to 80% '
        '<span class="qc qc-low"></span>under 50%'
        "</div>"
        "</div>"
    )


def _coverage_bucket(coverage: float) -> str:
    if coverage >= _COVERAGE_HIGH:
        return "high"
    if coverage >= _COVERAGE_MID:
        return "mid"
    return "low"


def _render_kpis(metadata: MetadataReport) -> str:
    geom_label = _geometry_label(metadata.geometry_types)
    pcode_kpi = _admin_coverage_kpi(metadata)
    bbox_kpi = _bbox_kpi(metadata)
    columns_count = len(metadata.columns)
    return (
        '<div class="kpis">'
        f'<div class="kpi"><div class="kpi-label">Features</div>'
        f'<div class="kpi-value">{_fmt_int(metadata.feature_count)}</div>'
        f'<div class="kpi-sub">{escape(geom_label)}</div></div>'
        f'<div class="kpi"><div class="kpi-label">Attribute columns</div>'
        f'<div class="kpi-value">{columns_count}</div>'
        f'<div class="kpi-sub">+ geometry</div></div>'
        f"{pcode_kpi}"
        f"{bbox_kpi}"
        "</div>"
    )


def _admin_coverage_kpi(metadata: MetadataReport) -> str:
    deepest = _deepest_pcode_column(metadata)
    if deepest is None:
        return (
            '<div class="kpi"><div class="kpi-label">Admin tagging</div>'
            '<div class="kpi-value" style="font-size:14px">off</div>'
            '<div class="kpi-sub">no adm{N}_pcode columns</div></div>'
        )
    coverage = 100.0 - deepest.null_percent
    level = deepest.name[len(_PCODE_PREFIX) : -len(_PCODE_SUFFIX)]
    return (
        '<div class="kpi"><div class="kpi-label">Admin tagging</div>'
        f'<div class="kpi-value">{coverage:.2f}%</div>'
        f'<div class="kpi-sub">have adm{escape(level)} pcode</div></div>'
    )


def _bbox_kpi(metadata: MetadataReport) -> str:
    if metadata.bbox is None:
        return (
            '<div class="kpi"><div class="kpi-label">Bounding box</div>'
            '<div class="kpi-value" style="font-size:14px">n/a</div>'
            '<div class="kpi-sub">empty dataset</div></div>'
        )
    minx, miny, maxx, maxy = metadata.bbox
    return (
        '<div class="kpi"><div class="kpi-label">Bounding box</div>'
        '<div class="kpi-value" style="font-size:14px">EPSG:4326</div>'
        f'<div class="bbox">{minx:.2f}, {miny:.2f} to {maxx:.2f}, {maxy:.2f}</div>'
        "</div>"
    )


def _render_geometry_types(metadata: MetadataReport) -> str:
    if not metadata.geometry_types:
        return ""
    total = sum(metadata.geometry_types.values()) or 1
    rows = []
    for gtype, count in metadata.geometry_types.items():
        share = count / total * 100.0
        rows.append(_proportion_row(escape(gtype), count, share))
    return _table("Geometry types", ["Type", "Count", "Share", ""], rows)


def _render_source_mix(metadata: MetadataReport) -> str:
    source_col = next((c for c in metadata.columns if c.name == _SOURCE_COL), None)
    if source_col is None or not source_col.top_values:
        return ""
    if len(source_col.top_values) <= 1:
        return ""
    total = metadata.feature_count or 1
    rows = []
    for entry in source_col.top_values:
        value = _stringify_value(entry.get("value"))
        count = int(entry.get("count", 0))
        share = count / total * 100.0
        rows.append(_proportion_row(escape(value), count, share))
    return _table("Source mix", ["Source", "Count", "Share", ""], rows)


def _render_pcode_coverage(metadata: MetadataReport) -> str:
    pcode_cols = [c for c in metadata.columns if _is_pcode_column(c.name)]
    if not pcode_cols:
        return ""
    total = metadata.feature_count or 1
    pcode_cols.sort(key=lambda c: c.name)
    rows = []
    for col in pcode_cols:
        tagged = total - col.null_count
        coverage = 100.0 - col.null_percent
        rows.append(_proportion_row(escape(col.name), tagged, coverage))
    return _table(
        "Pcode coverage by admin level",
        ["Level", "Tagged", "Coverage", ""],
        rows,
    )


def _render_attribute_table(metadata: MetadataReport) -> str:
    if not metadata.columns:
        return ""
    rows = [_attribute_row(col) for col in metadata.columns]
    return _table(
        "Attribute columns",
        ["Column", "Type", "Coverage", "Distinct", "Top values"],
        rows,
    )


def _render_footer(source: SourceMetadata) -> str:
    parts = [
        f"Source: {escape(source.dataset_source)}",
        f"snapshot {escape(source.snapshot_label)}",
        f"generated {escape(source.generated_utc)}",
    ]
    if source.pcode_source_date:
        parts.append(
            "p-codes from fieldmaps.io edge-matched humanitarian "
            f"({escape(source.pcode_source_date)})"
        )
    license_text = escape(source.license_label)
    if source.license_url:
        license_text = f'<a href="{escape(source.license_url)}">{license_text}</a>'
    parts.append(f"licensed under {license_text}")
    parts.append(f"oex {escape(source.oex_version)}")
    return f'<div class="tab-footer">{". ".join(parts)}.</div>'


def _attribute_row(col: ColumnReport) -> str:
    coverage = max(0.0, min(100.0, 100.0 - col.null_percent))
    return (
        "<tr>"
        f'<td class="col-name">{escape(col.name)}</td>'
        f'<td class="col-type">{escape(col.type)}</td>'
        f'<td class="coverage">'
        f'<div class="coverage-pct">{coverage:.2f}%</div>'
        f'<div class="bar"><i style="width:{coverage:.2f}%"></i></div>'
        f"</td>"
        f'<td class="num">{_fmt_int(col.distinct_count)}</td>'
        f'<td class="top-values">{_top_values_html(col)}</td>'
        "</tr>"
    )


def _top_values_html(col: ColumnReport) -> str:
    if not col.top_values:
        if col.distinct_count == 0:
            return "all null"
        if col.type.upper().startswith(("INT", "DOUBLE", "FLOAT", "BIGINT", "DECIMAL")):
            return "numeric"
        return "n/a"
    parts = []
    for entry in col.top_values:
        value = _stringify_value(entry.get("value"))
        count = int(entry.get("count", 0))
        parts.append(f'<code>{escape(value)}</code> <span class="count">{_fmt_int(count)}</span>')
    return " &middot; ".join(parts)


def _proportion_row(label_html: str, count: int, share: float) -> str:
    width = max(0.0, min(100.0, share))
    return (
        "<tr>"
        f'<td class="col-name">{label_html}</td>'
        f'<td class="num">{_fmt_int(count)}</td>'
        f'<td class="num">{share:.2f}%</td>'
        f'<td><div class="bar"><i style="width:{width:.2f}%"></i></div></td>'
        "</tr>"
    )


def _table(heading: str, headers: list[str], rows: list[str]) -> str:
    head_cells = "".join(
        f'<th class="num">{escape(h)}</th>'
        if h in {"Count", "Share", "Coverage", "Tagged", "Distinct"}
        else f"<th>{escape(h)}</th>"
        for h in headers
    )
    return (
        f"<h2>{escape(heading)}</h2>"
        "<table>"
        f"<thead><tr>{head_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _ordered_source_names(sources: dict[str, SourceMetadata]) -> list[str]:
    return sorted(sources.keys())


def _default_source_name(sources: dict[str, SourceMetadata], ordered: list[str]) -> str:
    # Most recently generated source is the default tab so a fresh push
    # always lands on the data the user just produced.
    return max(ordered, key=lambda name: sources[name].generated_utc)


def _pretty_source_name(name: str) -> str:
    if name == "osm":
        return "OpenStreetMap"
    if name == "overture":
        return "Overture"
    return name.title()


def _geometry_label(geom_types: dict[str, int]) -> str:
    if not geom_types:
        return "n/a"
    if len(geom_types) == 1:
        return next(iter(geom_types)).title() + "s"
    return "mixed"


def _is_pcode_column(name: str) -> bool:
    return name.startswith(_PCODE_PREFIX) and name.endswith(_PCODE_SUFFIX)


def _deepest_pcode_column(metadata: MetadataReport) -> ColumnReport | None:
    pcode_cols = [c for c in metadata.columns if _is_pcode_column(c.name)]
    populated = [c for c in pcode_cols if c.null_percent < 100.0]
    if not populated:
        return None
    return max(populated, key=lambda c: c.name)


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _stringify_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
