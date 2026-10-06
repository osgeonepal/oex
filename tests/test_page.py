"""The dataset page is what a reader actually opens, so it is tested as markup."""

import re
from dataclasses import replace
from html.parser import HTMLParser

import pytest

from oex.config.schema import ThemeConfig
from oex.metadata import ColumnReport, MetadataReport
from oex.report import SourceMetadata
from oex.report.page import (
    Download,
    PageLayer,
    downloads_from_assets,
    render_dataset_page,
)

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
NPL_BBOX = (84.55, 27.79, 85.38, 28.28)


class _Nesting(HTMLParser):
    """Every unclosed tag is a section that swallows the rest of the page."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.errors.append(f"stray </{tag}>")
        elif self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


def _layer(name: str = "buildings", *, count: int = 20403, downloads: bool = True) -> PageLayer:
    report = MetadataReport(
        feature_count=count,
        geometry_types={"POLYGON": count},
        bbox=NPL_BBOX,
        columns=[ColumnReport("name", "VARCHAR", 10, 5.0, 40, [{"value": "x", "count": 9}])],
        summary=f"{count} features.",
    )
    source = SourceMetadata(
        source_name="osm",
        snapshot_label="2026-09-18T03:55:55Z",
        dataset_source="OpenStreetMap",
        generated_utc="2026-09-18T04:00:34Z",
        oex_version="0.4.16",
        license_label="Open Database License (ODC-ODbL)",
        license_url="https://opendatacommons.org/licenses/odbl/1-0/",
        pcode_source_date=None,
        boundary="user geom",
        metadata=report,
    )
    return PageLayer(
        key=f"{name}-osm",
        label=name.replace("_", " ").title(),
        color="#1f77b4",
        source=source,
        downloads=[
            Download("gpkg", f"{name}_osm_gpkg.zip"),
            Download("geoparquet", f"{name}.parquet"),
        ]
        if downloads
        else [],
        match_key=f"{name}|osm",
    )


def _opened_layers(html: str) -> list[str]:
    return re.findall(
        r'<input type="radio" name="layer" id="([\w-]+)"[^>]* checked(?: disabled)?>', html
    )


def _render(*layers: PageLayer, **kwargs) -> str:
    if kwargs.pop("with_tiles", False):
        layers = tuple(replace(layer, tileset=("t.pmtiles", "t")) for layer in layers or [_layer()])
    return render_dataset_page(
        title=kwargs.pop("title", "Nepal flood affected area data"),
        layers=list(layers) or [_layer()],
        **kwargs,
    )


def test_the_page_is_well_formed_markup():
    """A page is published unattended, so an unclosed tag reaches every reader."""
    checker = _Nesting()
    checker.feed(_render(_layer(), _layer("roads", count=812), with_tiles=True))
    assert checker.errors == []
    assert checker.stack == []


def test_every_layer_is_listed_and_only_the_first_is_open():
    """The list is the navigation; a layer missing from it cannot be reached."""
    html = _render(_layer(), _layer("roads"), _layer("waterways"))
    assert html.count('name="layer"') == 3
    # Exactly one layer opens, and it is the first; none open means a blank detail pane.
    assert _opened_layers(html) == ["layer-buildings-osm"]
    for label in ("Buildings", "Roads", "Waterways"):
        assert f">{label}<" in html


def test_the_theme_reaches_the_page_as_css_tokens():
    """Every rule reads these, so one wrong token restyles the whole page."""
    theme = ThemeConfig(
        accent_color="#cc2936", map_popup_color="#aa1111", boundary_outline_color="#1d4ed8"
    )
    html = _render(_layer(), theme=theme)
    assert "--accent: #cc2936;" in html
    assert "--popup: #aa1111;" in html
    assert "--boundary: #1d4ed8;" in html


def test_the_about_grid_lets_wide_code_scroll_inside_its_column():
    """A plain `1fr` column grows to fit the unwrapped DuckDB query and widens the page on phones."""
    html = _render(_layer())
    columns = re.findall(r"\.about \{[^}]*grid-template-columns: ([^;]+);", html)
    assert columns
    for tracks in columns:
        assert re.fullmatch(r"minmax\(0, 1fr\)( minmax\(0, 1fr\))*", tracks)


def test_the_download_button_offers_every_format_the_layer_has():
    html = _render(_layer())
    assert '<option value="buildings_osm_gpkg.zip">GeoPackage</option>' in html
    assert '<option value="buildings.parquet">GeoParquet</option>' in html
    assert '<a class="dl-btn" href="buildings_osm_gpkg.zip" download>' in html


def test_a_layer_with_nothing_published_shows_no_download_control():
    """A dead control is worse than none; local runs without S3 have no hrefs."""
    html = _render(_layer(downloads=False))
    assert '<a class="dl-btn"' not in html
    assert '<select class="fmt"' not in html
    assert 'class="layer-view lv-buildings-osm"' in html


def test_without_a_tileset_there_is_no_map_view():
    """The switcher must not offer a view that would open an empty box."""
    html = _render(_layer())
    assert 'id="view-map"' not in html
    assert '<div class="switcher">' not in html
    assert "maplibregl.Map(" not in html


def test_with_a_tileset_the_map_is_a_peer_view():
    html = _render(_layer(), with_tiles=True)
    assert 'id="view-map"' in html
    assert ">Data<" in html and ">Map<" in html
    assert "pmtiles://t.pmtiles" in html


def test_the_map_is_resized_when_its_view_is_opened():
    """It is laid out while hidden, so it comes up zero-sized without this."""
    html = _render(_layer(), with_tiles=True)
    assert "mapTab.addEventListener('change', () => map.resize())" in html


def test_the_page_carries_the_small_screen_layout():
    """The dataset page is opened on phones in the field, not only on a laptop."""
    html = _render(_layer())
    assert "@media (max-width: 860px)" in html
    assert "@media (max-width: 480px)" in html
    assert 'name="viewport"' in html


def test_a_single_layer_dataset_still_reads_as_a_page():
    """A country export publishes one layer, and it must not look like a stub."""
    html = _render(_layer())
    assert _opened_layers(html) == ["layer-buildings-osm"]
    # The title already names the layer, so a one-entry list would only repeat it.
    assert '<div class="layer-list">' not in html
    assert "<h3>" not in html
    assert '<p class="ds-meta"><b>20,403</b> features' in html


def test_the_headline_counts_every_layer():
    html = _render(_layer(count=100), _layer("roads", count=23))
    assert "<b>2</b> layers" in html
    assert "<b>123</b> features" in html


def test_a_page_with_no_layers_is_refused():
    """An empty page would publish as a working link to nothing."""
    with pytest.raises(ValueError, match="at least one layer"):
        render_dataset_page(title="t", layers=[])


def test_a_format_split_by_geometry_reads_as_a_format_and_a_partition():
    """`polygons_shp` on its own tells a reader nothing about which file they get."""
    assert Download("polygons_shp", "x.zip").label == "Shapefile (polygons)"
    assert Download("gpkg", "x.zip").label == "GeoPackage"


def test_downloads_come_off_the_stac_assets_in_a_stable_order():
    """The published page reads the Item, so the order must not follow dict insertion."""
    assets = {
        "geoparquet": {"href": "b.parquet"},
        "shp": {"href": "b_shp.zip"},
        "gpkg": {"href": "b_gpkg.zip"},
        "nothing": {},
    }
    formats = [d.format for d in downloads_from_assets(assets)]
    # GeoPackage first because that is what most people want, GeoParquet last.
    assert formats == ["gpkg", "shp", "geoparquet"]
    # An asset with no href would render a dead button.
    assert "nothing" not in formats


def test_each_layer_can_carry_its_own_tileset():
    """A per-category dataset gives each source its own tiles, not one shared archive."""
    osm = replace(_layer("buildings"), tileset=("osm.pmtiles", "a"))
    overture = replace(_layer("roads"), key="roads-ovt", tileset=("ovt.pmtiles", "b"))
    html = _render(osm, overture)
    assert "pmtiles://osm.pmtiles" in html
    assert "pmtiles://ovt.pmtiles" in html


def test_a_layer_without_tiles_is_left_off_the_map():
    """Its entry would be a legend row that toggles nothing."""
    tiled = replace(_layer("buildings"), tileset=("t.pmtiles", "t"))
    html = _render(tiled, _layer("roads"))
    legend = html[html.index('<details class="legend"') : html.index("</details>")]
    assert "Buildings" in legend
    assert "Roads" not in legend


def test_each_download_names_its_size_when_it_is_known():
    assert Download("gpkg", "x.zip", size=835_146_954).label == "GeoPackage (835.1 MB)"
    assert Download("polygons_shp", "x.zip", size=2_048).label == "Shapefile (polygons, 2.0 kB)"
    assert downloads_from_assets({"gpkg": {"href": "x.zip", "file:size": 5}})[0].size == 5


def test_use_in_code_lists_only_the_links_the_layer_has():
    layer = replace(
        _layer(), metadata_href="https://x/item.json", tileset=("https://x/t.pmtiles", "t")
    )
    html = _render(layer)
    code = html.split("Use in code", 1)[1]
    assert (
        '<a href="https://x/item.json" target="_blank" rel="noopener"><code>https://x/item.json</code></a>'
    ) in code
    assert "<code>buildings.parquet</code>" in code
    assert "<code>https://x/t.pmtiles</code>" in code
    assert "Use in code" not in _render(_layer(downloads=False))


def test_layers_under_different_licences_name_each_in_the_header():
    cdla = replace(
        _layer("places"),
        source=replace(
            _layer().source,
            license_label="CDLA Permissive 2.0",
            license_url="https://cdla.dev/permissive-2-0/",
        ),
    )
    meta = _render(_layer(), cdla).split('<p class="ds-meta">', 1)[1].split("</p>", 1)[0]
    assert ">Open Database License (ODC-ODbL)</a> &middot; <a" in meta
    assert ">CDLA Permissive 2.0</a>" in meta


def test_a_lone_choice_is_kept_out_of_the_tab_order():
    """With no list or switcher there is no label to reach the radio by."""
    html = _render(_layer())
    assert 'id="layer-buildings-osm" class="tab-input" checked disabled>' in html
    assert 'id="view-data" class="tab-input" checked disabled>' in html
    several = _render(_layer(), _layer("roads"))
    assert 'id="layer-buildings-osm" class="tab-input" checked>' in several
    # A disabled radio ignores its label, so with a map both views must stay enabled.
    with_map = _render(_layer(), with_tiles=True)
    assert 'id="view-data" class="tab-input" checked>' in with_map
    assert 'id="view-map" class="tab-input">' in with_map


def test_a_single_layer_without_downloads_renders_no_empty_heading_box():
    assert '<div class="detail-head">' not in _render(_layer(downloads=False))


def test_related_datasets_and_the_hdx_page_are_linked_from_the_page():
    html = render_dataset_page(
        title="Buildings of Nepal",
        layers=[_layer()],
        dataset_url="https://data.humdata.org/dataset/hotosm_npl_buildings",
        related=[("Roads", "https://data.humdata.org/dataset/hotosm_npl_roads"), ("A&B", "x")],
    )
    # Opened from inside HDX's preview frame, a link in place would replace the page.
    assert (
        '<a href="https://data.humdata.org/dataset/hotosm_npl_roads" target="_blank" '
        'rel="noopener">Roads</a>'
    ) in html
    assert ">A&amp;B</a>" in html
    assert (
        '<dt>HDX dataset</dt><dd><a href="https://data.humdata.org/dataset/hotosm_npl_buildings" '
        'target="_blank" rel="noopener">'
    ) in html
    licence = '<a href="https://opendatacommons.org/licenses/odbl/1-0/" target="_blank"'
    assert licence in html.split('<p class="ds-meta">', 1)[1].split("</p>", 1)[0]
    plain = _render(_layer())
    assert 'class="related"' not in plain
    assert "<dt>HDX dataset</dt>" not in plain


def test_use_in_code_offers_a_duckdb_query_on_the_geoparquet():
    """Filtering on the bbox column is what lets DuckDB read a fraction of the file."""
    html = _render(_layer())
    assert "SELECT * FROM &#x27;buildings.parquet&#x27;" in html
    assert "WHERE bbox.xmin &gt;= 84.55 AND bbox.xmax &lt;= 85.38" in html
    assert "DuckDB" not in _render(_layer(downloads=False))
