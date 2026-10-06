"""The metadata sections a dataset page shows for one layer."""

import math
import re
from dataclasses import dataclass
from html import escape
from typing import Any

from oex.config.schema import ThemeConfig
from oex.metadata import ADMIN_COLUMN_RE, ColumnReport, MetadataReport, is_source_attribute
from oex.report.quality import coverage_bucket, layer_quality

_PCODE_SUFFIX = "_pcode"
_SOURCE_COL = "source"
# A geometry type below this share of a layer does not earn the layer a type table.
_MINOR_SHARE = 0.01
_NAME_LANG_RE = re.compile(r"^name_([a-z]{2,3})$")
_TRANSLIT_SUFFIX = "_latin"


def _license_link(item: dict[str, Any]) -> dict[str, Any]:
    """Every Item oex writes carries one, and the report reads the licence name off it."""
    for link in item["links"]:
        if link["rel"] == "license":
            return link
    raise KeyError(f"STAC Item {item['id']} carries no license link")


@dataclass(frozen=True)
class SourceMetadata:
    source_name: str
    snapshot_label: str
    dataset_source: str
    generated_utc: str
    oex_version: str
    license_label: str
    license_url: str | None
    pcode_source_date: str | None
    metadata: MetadataReport
    boundary: str | None = None

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "SourceMetadata":
        """Read back a STAC Item oex wrote; its `oex:` properties carry what STAC has no field for."""
        from oex.metadata import TemporalReport

        props = item["properties"]
        bbox = tuple(item["bbox"]) if item.get("bbox") else None
        columns = [
            ColumnReport(
                name=column["name"],
                type=column["type"],
                null_count=column["oex:null_count"],
                null_percent=column["oex:null_percent"],
                distinct_count=column["oex:distinct_count"],
                top_values=column["oex:top_values"],
            )
            for column in props["table:columns"]
        ]
        temporal_raw = props.get("oex:temporal")
        licence = _license_link(item)
        report = MetadataReport(
            feature_count=props["table:row_count"],
            geometry_types=dict(props["oex:geometry_types"] or {}),
            bbox=bbox,  # type: ignore[arg-type]
            columns=columns,
            summary=props["description"],
            temporal=TemporalReport(**temporal_raw) if temporal_raw else None,
        )
        return cls(
            source_name=props["oex:source"],
            snapshot_label=props["oex:snapshot"],
            dataset_source=props["oex:dataset_source"],
            generated_utc=props["created"],
            oex_version=props["oex:version"],
            license_label=licence["title"],
            license_url=licence["href"],
            pcode_source_date=props.get("oex:pcode_source_date"),
            boundary=props.get("oex:boundary"),
            metadata=report,
        )


BASE_CSS = """
:root {
  --fg: #1a1a1a;
  --muted: #6b6b6b;
  --line: #e4e4e4;
  --bg: #ffffff;
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
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em;
     color: var(--muted); margin: 32px 0 10px; font-weight: 600; }
.ratio-bar { display: flex; height: 6px; border-radius: 2px; overflow: hidden;
             background: var(--bar-bg); margin-top: 8px; }
.ratio-bar > i { display: block; height: 100%; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
        background: var(--line); margin: 18px 0 0; border: 1px solid var(--line); }
.kpi { background: var(--bg); padding: 14px 16px; }
.kpi-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
             color: var(--muted); }
.kpi-value { font-size: 18px; font-weight: 600; line-height: 1.2; margin-top: 4px;
             font-variant-numeric: tabular-nums; }
.kpi-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.04em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.col-name { font-family: var(--mono); }
.bar { background: var(--bar-bg); border-radius: 2px; height: 6px;
       position: relative; overflow: hidden; min-width: 80px; }
.bar > i { display: block; height: 100%; }
td.bar-cell { width: 30%; padding-top: 13px; }
.well { background: #22c55e; } .partial { background: #eab308; } .rare { background: #ef4444; }
.share { background: #9ca3af; }
.coverage-legend { font-size: 12px; color: var(--muted); display: flex; gap: 14px;
                   flex-wrap: wrap; margin: 0 0 6px; }
.coverage-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
                     margin-right: 4px; vertical-align: -1px; }
.note, .muted { color: var(--muted); } .note { font-size: 12px; margin: 8px 0 0; }
.top-values { color: var(--muted); font-size: 12px; min-width: 220px; }
.top-values code { font-family: var(--mono); color: var(--fg);
                   background: #f4f4f5; padding: 1px 5px; border-radius: 3px; }
.top-values .count { color: var(--muted); }
.about { margin-top: 40px; border-top: 1px solid var(--line); display: grid;
          grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 24px; }
.about dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; margin: 0;
            font-size: 13px; }
.about dt { color: var(--muted); } .about dd { margin: 0; overflow-wrap: anywhere; }
.code-links { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; font-size: 12px; }
.code-links span { display: block; color: var(--muted); }
.code-links code { font-family: var(--mono); font-size: 11px; word-break: break-all; }
.snippet-label { margin: 12px 0 4px; font-size: 12px; color: var(--muted); }
.snippet { margin: 0; padding: 8px 10px; background: #f4f4f5; border-radius: 3px;
           font: 11px/1.5 var(--mono); overflow-x: auto; white-space: pre; }
.temporal { padding: 6px 10px; background: #fafafa;
            border-left: 3px solid var(--line); border-radius: 2px;
            font-size: 12px; color: var(--muted); margin-top: 8px; }
.temporal-label { font-weight: 600; color: var(--fg);
                  text-transform: uppercase; letter-spacing: 0.04em; font-size: 11px; }
.temporal-range { font-variant-numeric: tabular-nums; color: var(--fg); }
.temporal-arrow { color: var(--muted); }
.temporal-meta code { font-family: var(--mono); background: #f4f4f5;
                      padding: 1px 4px; border-radius: 3px; color: var(--fg); }
"""


def panel_sections(
    source: SourceMetadata, links: list[tuple[str, str]], dataset_url: str | None = None
) -> str:
    metadata = source.metadata
    if not metadata.feature_count:
        return '<p class="note">This layer has no features.</p>' + _render_about(
            source, links, dataset_url
        )
    sections = [
        _render_kpis(metadata),
        _render_temporal(metadata),
        _render_geometry_types(metadata),
        _render_source_mix(metadata),
        _render_admin_coverage(metadata),
        _render_attribute_table(metadata),
        _render_about(source, links, dataset_url),
    ]
    return "\n".join(s for s in sections if s)


def _render_temporal(metadata: MetadataReport) -> str:
    t = metadata.temporal
    if t is None or (t.min is None and t.max is None):
        return ""
    column = escape(t.column)
    lo = escape(t.min or "n/a")
    hi = escape(t.max or "n/a")
    coverage_pct = (
        (t.non_null_count / metadata.feature_count * 100.0) if metadata.feature_count else 0.0
    )
    return (
        '<div class="temporal">'
        '<span class="temporal-label">Temporal coverage</span> '
        f'<span class="temporal-range">{lo} <span class="temporal-arrow">to</span> {hi}</span>'
        f' <span class="temporal-meta">column <code>{column}</code>, '
        f"{coverage_pct:.1f}% of {format_int(metadata.feature_count)} features"
        "</span></div>"
    )


def _render_kpis(metadata: MetadataReport) -> str:
    return (
        '<div class="kpis">'
        f"{_geometry_kpi(metadata)}{_completeness_kpi(metadata)}"
        f"{_admin_coverage_kpi(metadata)}{_languages_kpi(metadata)}"
        "</div>"
    )


def _kpi(label: str, value: str, sub: str, extra: str = "") -> str:
    return (
        f'<div class="kpi"><div class="kpi-label">{escape(label)}</div>'
        f'<div class="kpi-value">{value}</div><div class="kpi-sub">{sub}</div>{extra}</div>'
    )


def _geometry_kpi(metadata: MetadataReport) -> str:
    if not metadata.geometry_types:
        return _kpi("Geometry", "unknown", "geometry type not reported")
    main, count = max(metadata.geometry_types.items(), key=lambda kv: kv[1])
    other = metadata.feature_count - count
    sub = _plural(main) + (f", {format_int(other)} other" if other else "")
    return _kpi("Geometry", f"{count / metadata.feature_count * 100:.2f}%", escape(sub))


def _completeness_kpi(metadata: MetadataReport) -> str:
    quality = layer_quality(_data_columns(metadata))
    total = quality.total_columns
    if not total:
        return _kpi("Attributes filled", "none", "no attribute columns")
    segments = "".join(
        f'<i class="{bucket}" style="width:{count / total * 100:.2f}%"></i>'
        for bucket, count in (
            ("well", quality.well),
            ("partial", quality.partial),
            ("rare", quality.rare),
        )
    )
    return _kpi(
        "Attributes filled",
        f"{quality.well} of {total}",
        f"at least half filled; {quality.partial} partial, {quality.rare} sparse",
        f'<div class="ratio-bar">{segments}</div>',
    )


def _admin_coverage_kpi(metadata: MetadataReport) -> str:
    deepest = _deepest_pcode_column(metadata)
    if deepest is None:
        return _kpi("Admin tagging", "off", "no admin codes")
    level = deepest.name[: -len(_PCODE_SUFFIX)]
    return _kpi("Admin tagging", _filled_label(deepest, metadata), f"to {escape(level)}")


def _languages_kpi(metadata: MetadataReport) -> str:
    languages = _language_columns(metadata)
    if not languages:
        return _kpi("Languages", "none", "no name columns")
    return _kpi("Languages", str(len(languages)), escape(", ".join(languages)))


def _language_columns(metadata: MetadataReport) -> list[str]:
    """Language labels carried by the dataset, ordered as they appear."""
    labels: list[str] = []
    for col in metadata.columns:
        if col.name == "name":
            labels.append("local")
            continue
        match = _NAME_LANG_RE.match(col.name)
        if match:
            labels.append(match.group(1))
    return labels


def _render_geometry_types(metadata: MetadataReport) -> str:
    """Only worth a table when the layer is not essentially one geometry type."""
    total = sum(metadata.geometry_types.values())
    if not total or max(metadata.geometry_types.values()) / total >= 1 - _MINOR_SHARE:
        return ""
    ordered = sorted(metadata.geometry_types.items(), key=lambda kv: kv[1], reverse=True)
    rows = [_share_row(escape(_plural(g).capitalize()), n, n / total * 100.0) for g, n in ordered]
    return _table("Geometry types", ["Type", "Features", "Share", ""], rows)


def _render_source_mix(metadata: MetadataReport) -> str:
    source_col = _source_column(metadata)
    if source_col is None or len(source_col.top_values) <= 1:
        return ""
    total = metadata.feature_count or 1
    rows = []
    for entry in source_col.top_values:
        count = int(entry.get("count", 0))
        rows.append(
            _share_row(escape(_stringify_value(entry.get("value"))), count, count / total * 100.0)
        )
    return _table("Source mix", ["Source", "Features", "Share", ""], rows)


def _render_admin_coverage(metadata: MetadataReport) -> str:
    pcode_cols = sorted(
        (c for c in metadata.columns if _is_pcode_column(c.name)), key=lambda c: c.name
    )
    tagged = [c for c in pcode_cols if _has_values(c, metadata)]
    if not tagged:
        return ""
    rows = [_coverage_row(escape(c.name[: -len(_PCODE_SUFFIX)]), c, metadata, None) for c in tagged]
    empty = [c.name[: -len(_PCODE_SUFFIX)] for c in pcode_cols if not _has_values(c, metadata)]
    note = (
        f'<p class="note">No {escape(", ".join(empty))} boundaries for this area.</p>'
        if empty
        else ""
    )
    return _table("Admin coverage", ["Level", "Tagged", ""], rows) + note


def _render_attribute_table(metadata: MetadataReport) -> str:
    columns = sorted(_data_columns(metadata), key=lambda c: c.null_percent)
    if not columns:
        return ""
    legend = (
        '<div class="coverage-legend"><span><i class="well"></i>50% or more filled</span>'
        '<span><i class="partial"></i>25 to 50%</span><span><i class="rare"></i>under 25%</span></div>'
    )
    rows = [
        _coverage_row(
            f'<span class="col-name">{escape(c.name)}</span>',
            c,
            metadata,
            _common_values(c, metadata),
        )
        for c in columns
    ]
    return _table("Attributes", ["Column", "Filled", "", "Common values"], rows, legend)


def _render_about(
    source: SourceMetadata, links: list[tuple[str, str]], dataset_url: str | None
) -> str:
    facts = [("Data", escape(source.dataset_source)), ("Snapshot", escape(source.snapshot_label))]
    if source.boundary:
        facts.append(("Boundary", escape(source.boundary)))
    if source.pcode_source_date:
        facts.append(
            (
                "P-codes",
                f"fieldmaps.io edge-matched humanitarian, {escape(source.pcode_source_date)}",
            )
        )
    if any(col.name.endswith(_TRANSLIT_SUFFIX) for col in source.metadata.columns):
        facts.append(("Latin names", "transliterated with unidecode"))
    licence = escape(source.license_label)
    if source.license_url:
        licence = outbound_link(source.license_url, licence)
    facts.append(("Licence", licence))
    if dataset_url:
        facts.append(("HDX dataset", outbound_link(dataset_url, escape(dataset_url))))
    facts.append(("Built", f"{escape(source.generated_utc)}, oex {escape(source.oex_version)}"))
    about = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in facts)
    code = "".join(
        f"<li><span>{escape(label)}</span>{outbound_link(href, f'<code>{escape(href)}</code>')}</li>"
        for label, href in links
    )
    parquet = next((href for label, href in links if label == "GeoParquet"), None)
    snippet = _duckdb_snippet(parquet, source.metadata.bbox) if parquet else ""
    code_section = (
        f'<section><h2>Use in code</h2><ul class="code-links">{code}</ul>{snippet}</section>'
        if code
        else ""
    )
    return (
        f'<div class="about"><section><h2>About this data</h2><dl>{about}</dl></section>'
        f"{code_section}</div>"
    )


def _duckdb_snippet(url: str, bbox: tuple[float, float, float, float] | None) -> str:
    """Filtering on the `bbox` column lets DuckDB skip row groups outside the area, so a
    smaller area reads a fraction of the file over HTTP."""
    if bbox is None:
        return ""
    west, south = (math.floor(round(v * 100, 6)) / 100 for v in bbox[:2])
    east, north = (math.ceil(round(v * 100, 6)) / 100 for v in bbox[2:])
    query = (
        f"SELECT * FROM '{url}'\n"
        f"WHERE bbox.xmin >= {west:.2f} AND bbox.xmax <= {east:.2f}\n"
        f"  AND bbox.ymin >= {south:.2f} AND bbox.ymax <= {north:.2f}\n"
        "LIMIT 10;"
    )
    return f'<p class="snippet-label">DuckDB</p><pre class="snippet"><code>{escape(query)}</code></pre>'


def _data_columns(metadata: MetadataReport) -> list[ColumnReport]:
    """The source's own attributes. Admin codes and the source mix have their own tables, and
    `id` is always complete, so counting any of them would overstate how filled the data is."""
    mixed_out = _source_mix_is_complete(metadata)
    return [
        c
        for c in metadata.columns
        if is_source_attribute(c.name) and not (mixed_out and c.name == _SOURCE_COL)
    ]


def _has_values(col: ColumnReport, metadata: MetadataReport) -> bool:
    """Read off the counts, since `null_percent` rounds one value in a million to 100."""
    return col.null_count < metadata.feature_count


def _common_values(col: ColumnReport, metadata: MetadataReport) -> str:
    if not _has_values(col, metadata):
        return '<span class="muted">empty</span>'
    if col.top_values:
        return " &middot; ".join(
            f"<code>{escape(_stringify_value(entry.get('value')))}</code> "
            f'<span class="count">{format_int(int(entry.get("count", 0)))}</span>'
            for entry in col.top_values
        )
    if col.distinct_count == metadata.feature_count - col.null_count:
        return '<span class="muted">unique per feature</span>'
    return f'<span class="muted">{format_int(col.distinct_count)} distinct values</span>'


def _coverage_row(
    label_html: str, col: ColumnReport, metadata: MetadataReport, detail: str | None
) -> str:
    filled = _filled_percent(col, metadata)
    detail_cell = "" if detail is None else f'<td class="top-values">{detail}</td>'
    return (
        f'<tr><td>{label_html}</td><td class="num">{_filled_label(col, metadata)}</td>'
        f'<td class="bar-cell"><div class="bar"><i class="{coverage_bucket(filled)}" '
        f'style="width:{filled:.2f}%"></i></div></td>{detail_cell}</tr>'
    )


def _filled_percent(col: ColumnReport, metadata: MetadataReport) -> float:
    if not metadata.feature_count:
        return 0.0
    return (metadata.feature_count - col.null_count) / metadata.feature_count * 100.0


def _filled_label(col: ColumnReport, metadata: MetadataReport) -> str:
    """A column with any value never reads 0.00%, which the reader would take for empty."""
    filled = _filled_percent(col, metadata)
    return "&lt;0.01%" if 0 < filled < 0.005 else f"{filled:.2f}%"


def _source_column(metadata: MetadataReport) -> ColumnReport | None:
    return next((c for c in metadata.columns if c.name == _SOURCE_COL), None)


def _source_mix_is_complete(metadata: MetadataReport) -> bool:
    """Only then does the Source mix table say everything the column's own row would."""
    col = _source_column(metadata)
    return (
        col is not None
        and len(col.top_values) > 1
        and col.null_count == 0
        and col.distinct_count <= len(col.top_values)
    )


def _share_row(label_html: str, count: int, share: float) -> str:
    width = max(0.0, min(100.0, share))
    return (
        f'<tr><td>{label_html}</td><td class="num">{format_int(count)}</td>'
        f'<td class="num">{share:.2f}%</td>'
        f'<td class="bar-cell"><div class="bar"><i class="share" style="width:{width:.2f}%"></i></div></td></tr>'
    )


def _table(heading: str, headers: list[str], rows: list[str], intro: str = "") -> str:
    head_cells = "".join(
        f'<th class="num">{escape(h)}</th>'
        if h in {"Features", "Share", "Tagged", "Filled"}
        else f"<th>{escape(h)}</th>"
        for h in headers
    )
    return (
        f"<h2>{escape(heading)}</h2>{intro}"
        f'<div class="table-scroll"><table><thead><tr>{head_cells}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _plural(geometry_type: str) -> str:
    return geometry_type.replace("ST_", "").lower() + "s"


def _is_pcode_column(name: str) -> bool:
    match = ADMIN_COLUMN_RE.match(name)
    return match is not None and match.group(2) == "pcode"


def _deepest_pcode_column(metadata: MetadataReport) -> ColumnReport | None:
    pcode_cols = [c for c in metadata.columns if _is_pcode_column(c.name)]
    populated = [c for c in pcode_cols if _has_values(c, metadata)]
    if not populated:
        return None
    return max(populated, key=lambda c: c.name)


def outbound_link(href: str, text_html: str) -> str:
    """A link off the page opens beside it: the page usually sits inside HDX's preview frame."""
    return f'<a href="{escape(href)}" target="_blank" rel="noopener">{text_html}</a>'


def format_int(value: int) -> str:
    return f"{value:,}"


def _stringify_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def theme_tokens(theme: ThemeConfig) -> str:
    """The theme as CSS custom properties, which every rule on the page reads."""
    return (
        ":root {"
        f" --accent: {theme.accent_color};"
        f" --popup: {theme.map_popup_color};"
        f" --boundary: {theme.boundary_outline_color};"
        " }"
    )
