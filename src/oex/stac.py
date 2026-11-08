"""STAC Items and Collections for the layers oex exports. Hrefs come from the S3 key helpers,
so a Collection is built before anything is uploaded.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import antimeridian
import pystac
from pystac import ItemAssetDefinition
from pystac.extensions.file import FileExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.table import Column, TableExtension

from oex.catalog import layout as catalog_layout
from oex.config.schema import CategoryConfig, RootConfig, license_label
from oex.logging_setup import get_logger
from oex.metadata import GEOMETRY_COLUMN, MetadataReport
from oex.naming import ASSET_TITLES, category_label, slugify, split_artifact_filename
from oex.report import SourceMetadata
from oex.s3 import category_artifact_key, public_url
from oex.s3 import resolve as s3_resolve
from oex.timestamps import parse_utc
from oex.writers import GEOMETRY_LABELS

CRS = "EPSG:4326"
JSON_MEDIA_TYPE = "application/json"

_MEDIA_TYPES = {
    "fgb": pystac.MediaType.FLATGEOBUF,
    "geojson": pystac.MediaType.GEOJSON,
    "geoparquet": pystac.MediaType.VND_APACHE_PARQUET,
    "gpkg": pystac.MediaType.GEOPACKAGE,
    "kml": pystac.MediaType.KML,
    "pmtiles": pystac.MediaType.VND_PMTILES,
    "zip": "application/zip",
}
_SPDX = {
    "hdx-odc-odbl": "ODbL-1.0",
    "hdx-odc-by": "ODC-By-1.0",
    "hdx-pddl": "PDDL-1.0",
    "cc-by": "CC-BY-4.0",
    "cc-by-sa": "CC-BY-SA-4.0",
    "cc-by-igo": "CC-BY-3.0-IGO",
    "other-pd-nr": "CC0-1.0",
}
logger = get_logger(__name__)

_NO_SPDX = "other"
_TILE_ROLES = ["visual", "overview"]
_DATA_ROLES = ["data"]


@dataclass
class LayerAssets:
    """What one layer contributes to its Item and to the dataset Collection."""

    category: CategoryConfig
    source_metadata: SourceMetadata
    # Per-category even on a combined dataset, so an Item keeps its layer's own name.
    dataset_name: str
    artifacts: list[Path] = field(default_factory=list)
    # Only for artifacts published elsewhere; otherwise hrefs follow the S3 key layout.
    artifact_urls: dict[str, str] = field(default_factory=dict)
    geoparquet_url: str | None = None
    geoparquet_path: Path | None = None
    pmtiles_url: str | None = None
    pmtiles_path: Path | None = None
    # Names the place too, since an Item turns up in searches away from its Collection.
    title: str | None = None
    # Wall time to build the layer's files, reading the source included and uploads excluded.
    export_seconds: float | None = None
    # The export boundary as GeoJSON; the Item's footprint when set, else the data's bbox.
    footprint: str | None = None
    # Fingerprint of the features and the settings that shaped them, for skip_unchanged.
    content_hash: str | None = None


def spdx_license(hdx_license: str) -> str:
    """`other` is the spec's answer for a licence with no SPDX identifier."""
    return _SPDX.get(hdx_license, _NO_SPDX)


def _layer_licenses(layers: list[LayerAssets]) -> list[str]:
    """Distinct SPDX ids across a dataset's layers; Overture themes carry different ones."""
    return sorted({spdx_license(layer.category.hdx.license) for layer in layers})


def license_href(hdx_license: str, license_url: str | None) -> str:
    """The licence text a `rel=license` link points at. The spec requires the link for a
    non-SPDX licence, and without either the data grants consumers no right to use it."""
    if license_url:
        return license_url
    spdx = spdx_license(hdx_license)
    if spdx == _NO_SPDX:
        raise ValueError(
            f"licence {hdx_license!r} has no SPDX identifier, so hdx.license_url must "
            "give the licence text; without it the published data grants no usage rights"
        )
    return f"https://spdx.org/licenses/{spdx}.html"


def item_id(dataset_name: str, source_name: str) -> str:
    """Stable across runs, so two datasets diff layer by layer."""
    return f"{dataset_name}-{source_name}"


def item_filename(dataset_name: str, source_name: str) -> str:
    return f"{dataset_name}_{source_name}_item.json"


def collection_filename(dataset_name: str, source_name: str) -> str:
    return f"{dataset_name}_{source_name}_metadata.json"


def partition_of(filename: str, category_slug: str) -> str:
    """The points/lines/polygons segment split_by_geometry adds, or empty."""
    stem = filename.split(".", 1)[0]
    _, _, tail = stem.partition(f"_{category_slug}_")
    head = tail.split("_")[0] if tail else ""
    return head if head in GEOMETRY_LABELS else ""


def asset_key(filename: str, category_slug: str) -> str:
    """`gpkg`, or `polygons_gpkg` when split_by_geometry gives a format several files."""
    _, _, format_name = split_artifact_filename(filename)
    format_name = "geoparquet" if format_name == "parquet" else format_name
    geometry = partition_of(filename, category_slug)
    return f"{geometry}_{format_name}" if geometry else format_name


def asset_href(
    cfg: RootConfig, category_slug: str, filename: str, *, geometry: str = ""
) -> str | None:
    """Where an artifact will be published, matching the key the publisher uploads to."""
    if not cfg.output.s3.enabled:
        return None
    bucket, prefix, region, endpoint_url, _ = s3_resolve(cfg.output.s3)
    if not bucket:
        return None
    key = category_artifact_key(
        cfg.output.s3, prefix, cfg.iso3, category_slug, filename, geometry=geometry
    )
    return public_url(bucket=bucket, key=key, region=region, endpoint_url=endpoint_url)


def href_or_filename(
    cfg: RootConfig, category_slug: str, filename: str, *, geometry: str = ""
) -> str:
    """The published URL, or the bare filename so a local run stays a relative catalog."""
    return asset_href(cfg, category_slug, filename, geometry=geometry) or filename


def item_href(cfg: RootConfig, layer: LayerAssets) -> str:
    slug = slugify(layer.category.name)
    return href_or_filename(
        cfg, slug, item_filename(layer.dataset_name, layer.source_metadata.source_name)
    )


def build_item(
    layer: LayerAssets,
    cfg: RootConfig,
    *,
    self_href: str | None = None,
    collection_id: str | None = None,
    collection_href: str | None = None,
    hdx_url: str | None = None,
    page_url: str | None = None,
) -> pystac.Item:
    source_metadata = layer.source_metadata
    metadata_report = source_metadata.metadata
    category = layer.category
    slug = slugify(category.name)
    geometry, bbox = _footprint(layer)

    item = pystac.Item(
        id=item_id(layer.dataset_name, source_metadata.source_name),
        geometry=geometry,
        bbox=bbox,
        datetime=_moment(source_metadata.snapshot_label, source_metadata.generated_utc),
        properties=_present(
            {
                "oex:category": slug,
                "oex:source": source_metadata.source_name,
                "oex:dataset_source": source_metadata.dataset_source,
                "oex:snapshot": source_metadata.snapshot_label,
                "oex:boundary": source_metadata.boundary,
                "oex:version": source_metadata.oex_version,
                "oex:pcode_source_date": source_metadata.pcode_source_date,
                "oex:geometry_types": metadata_report.geometry_types,
                # The column name and its coverage have no STAC field of their own.
                "oex:temporal": asdict(metadata_report.temporal)
                if metadata_report.temporal
                else None,
                "oex:export_seconds": (
                    round(layer.export_seconds, 1) if layer.export_seconds is not None else None
                ),
                "oex:content_hash": layer.content_hash,
            }
        ),
        collection=collection_id,
    )
    common = item.common_metadata
    common.title = layer.title or category_label(category)
    common.description = metadata_report.summary
    common.license = spdx_license(category.hdx.license)
    common.created = _moment(source_metadata.generated_utc, source_metadata.generated_utc)
    common.keywords = list(category.hdx.tags)
    common.providers = _providers(source_metadata)
    start, end = _temporal_range(metadata_report)
    if start and end:
        common.start_datetime = start
        common.end_datetime = end

    item.add_link(
        pystac.Link(
            rel="license",
            target=license_href(category.hdx.license, category.hdx.license_url),
            title=license_label(category.hdx.license),
        )
    )
    catalog = catalog_layout(cfg)
    _address(item, self_href, catalog.root.url if catalog else collection_href)
    if collection_href:
        for rel in ("collection", "parent"):
            item.add_link(pystac.Link(rel=rel, target=collection_href, media_type=JSON_MEDIA_TYPE))
    if hdx_url:
        item.add_link(pystac.Link(rel="via", target=hdx_url, title="HDX dataset"))
    _link_page(item, page_url)

    ProjectionExtension.ext(item, add_if_missing=True).code = CRS
    _describe_table(item, metadata_report)
    for key, href, path in _artifact_assets(layer, cfg, slug):
        _add_asset(item, key, href, path=path)
    if layer.geoparquet_url:
        _add_asset(item, "geoparquet", layer.geoparquet_url, path=layer.geoparquet_path)
    if layer.pmtiles_url:
        _add_asset(item, "pmtiles", layer.pmtiles_url, path=layer.pmtiles_path)
    return item


def build_collection(
    *,
    dataset_name: str,
    title: str,
    description: str,
    layers: list[LayerAssets],
    cfg: RootConfig,
    keywords: list[str] | None = None,
    pmtiles_url: str | None = None,
    pmtiles_path: Path | None = None,
    self_href: str | None = None,
    hdx_url: str | None = None,
    related: list[tuple[str, str]] | None = None,
    page_url: str | None = None,
) -> pystac.Collection:
    """Every layer of one dataset, linking out to the Item file each layer writes. `related`
    names the area's other dataset Collections as (title, href)."""
    if not layers:
        raise ValueError("build_collection needs at least one layer")
    items = [
        build_item(layer, cfg, collection_id=dataset_name, collection_href=self_href)
        for layer in layers
    ]
    licenses = _layer_licenses(layers)
    collection = pystac.Collection(
        id=dataset_name,
        title=title,
        description=description,
        extent=_extent(items),
        license=licenses[0] if len(licenses) == 1 else _NO_SPDX,
        keywords=keywords or sorted({tag for layer in layers for tag in layer.category.hdx.tags}),
        providers=_providers(layers[0].source_metadata),
    )
    collection.summaries = pystac.Summaries(
        {
            "oex:category": sorted({slugify(layer.category.name) for layer in layers}),
            "oex:source": sorted({layer.source_metadata.source_name for layer in layers}),
        }
    )
    collection.item_assets.update(_item_assets(items))
    distinct = {(layer.category.hdx.license, layer.category.hdx.license_url) for layer in layers}
    for hdx_license, license_url in sorted(distinct, key=lambda pair: (pair[0], pair[1] or "")):
        collection.add_link(
            pystac.Link(
                rel="license",
                target=license_href(hdx_license, license_url),
                title=license_label(hdx_license),
            )
        )
    for layer, item in zip(layers, items, strict=True):
        collection.add_link(
            pystac.Link(
                rel="item",
                target=item_href(cfg, layer),
                media_type=JSON_MEDIA_TYPE,
                title=item.common_metadata.title,
            )
        )
    if pmtiles_url:
        _add_asset(collection, "pmtiles", pmtiles_url, path=pmtiles_path, title_suffix="All Layers")
    catalog = catalog_layout(cfg)
    _address(collection, self_href, catalog.root.url if catalog else self_href)
    if catalog:
        collection.add_link(
            pystac.Link(rel="parent", target=catalog.area.url, media_type=JSON_MEDIA_TYPE)
        )
    if hdx_url:
        collection.add_link(pystac.Link(rel="via", target=hdx_url, title="HDX dataset"))
    _link_page(collection, page_url)
    for title, href in related or []:
        collection.add_link(
            pystac.Link(rel="related", target=href, media_type=JSON_MEDIA_TYPE, title=title)
        )
    return collection


def _link_page(stac_object: pystac.Item | pystac.Collection, page_url: str | None) -> None:
    """STAC's way to name an HTML view of the same thing, which clients offer as a link."""
    if page_url:
        stac_object.add_link(
            pystac.Link(
                rel="alternate", target=page_url, media_type="text/html", title="Dataset page"
            )
        )


def _address(
    stac_object: pystac.Item | pystac.Collection, self_href: str | None, root_href: str | None
) -> None:
    """pystac resolves a relative `self` href against the working directory, so a
    relative catalog names its root only."""
    if self_href and "://" in self_href:
        stac_object.set_self_href(self_href)
    stac_object.remove_links("root")
    if root_href:
        stac_object.add_link(pystac.Link(rel="root", target=root_href, media_type=JSON_MEDIA_TYPE))


def to_payload(stac_object: pystac.Item | pystac.Collection) -> dict[str, Any]:
    """The document as published. These hrefs are already the addresses the files will
    be served from, so resolving them would only have pystac fetch a missing root."""
    return stac_object.to_dict(include_self_link=True, transform_hrefs=False)


def write_json(stac_object: pystac.Item | pystac.Collection, path: Path) -> None:
    # Names in other scripts are written as themselves, not as \u escapes nobody can read.
    path.write_text(
        json.dumps(to_payload(stac_object), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _present(properties: dict[str, Any]) -> dict[str, Any]:
    """A null property is one an intermediary may strip, so it is never written."""
    return {key: value for key, value in properties.items() if value is not None}


def _providers(source_metadata: SourceMetadata) -> list[pystac.Provider]:
    return [
        pystac.Provider(
            name=source_metadata.dataset_source,
            roles=[pystac.ProviderRole.PRODUCER, pystac.ProviderRole.LICENSOR],
        )
    ]


def _describe_table(item: pystac.Item, report: MetadataReport) -> None:
    table = TableExtension.ext(item, add_if_missing=True)
    table.row_count = report.feature_count
    table.primary_geometry = GEOMETRY_COLUMN
    # The extension wants the native type; the statistics have no STAC field at all.
    table.columns = [
        Column(
            {
                "name": column.name,
                "type": column.type,
                "oex:null_count": column.null_count,
                "oex:null_percent": column.null_percent,
                "oex:distinct_count": column.distinct_count,
                "oex:top_values": column.top_values,
            }
        )
        for column in report.columns
    ]


def _artifact_assets(
    layer: LayerAssets, cfg: RootConfig, category_slug: str
) -> list[tuple[str, str, Path]]:
    assets = []
    for path in sorted(layer.artifacts):
        geometry = partition_of(path.name, category_slug)
        href = layer.artifact_urls.get(path.name) or href_or_filename(
            cfg, category_slug, path.name, geometry=geometry
        )
        assets.append((asset_key(path.name, category_slug), href, path))
    return assets


def _add_asset(
    owner: pystac.Item | pystac.Collection,
    key: str,
    href: str,
    *,
    path: Path | None = None,
    title_suffix: str = "",
) -> None:
    format_name = key.rpartition("_")[2]
    suffix = href.rpartition(".")[2]
    title = ASSET_TITLES.get(format_name, format_name.upper())
    asset = pystac.Asset(
        href=href,
        media_type=_MEDIA_TYPES.get(suffix, _MEDIA_TYPES.get(format_name)),
        title=f"{title}, {title_suffix}" if title_suffix else title,
        roles=list(_TILE_ROLES if format_name == "pmtiles" else _DATA_ROLES),
    )
    owner.add_asset(key, asset)
    if path is not None and path.exists():
        FileExtension.ext(asset, add_if_missing=True).size = path.stat().st_size


def _item_assets(items: list[pystac.Item]) -> dict[str, ItemAssetDefinition]:
    """What a client can expect on any Item here, so a format picker needs no Item fetch."""
    definitions: dict[str, ItemAssetDefinition] = {}
    for item in items:
        for key, asset in item.assets.items():
            definitions.setdefault(
                key,
                ItemAssetDefinition.create(
                    title=asset.title,
                    description=None,
                    media_type=asset.media_type,
                    roles=asset.roles,
                ),
            )
    return definitions


def _extent(items: list[pystac.Item]) -> pystac.Extent:
    """`from_items` folds in each Item's start and end datetimes, which the Item
    datetime alone would miss; with no footprint anywhere it returns infinities."""
    extent = pystac.Extent.from_items(items)
    if not any(item.bbox for item in items):
        extent.spatial = pystac.SpatialExtent([[-180.0, -90.0, 180.0, 90.0]])
    return extent


def _footprint(layer: LayerAssets) -> tuple[dict[str, Any] | None, list[float] | None]:
    if layer.footprint:
        geometry = json.loads(layer.footprint)
        # West exceeds east for an outline crossing 180 degrees, as RFC 7946 section 5.2 asks.
        return geometry, antimeridian.bbox(geometry)
    data_bbox = layer.source_metadata.metadata.bbox
    if not data_bbox:
        return None, None
    return _geometry(tuple(data_bbox)), list(data_bbox)


def _geometry(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    west, south, east, north = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }


def _moment(label: str, fallback: str) -> datetime:
    """An Overture label like `2026-06-17.0` is no timestamp, so an unreadable one dates
    the Item from the run instead; `oex:snapshot` keeps the label either way."""
    for candidate in (label, label[:10]):
        try:
            return parse_utc(candidate)
        except ValueError:
            continue
    return parse_utc(fallback)


def _temporal_range(report: MetadataReport) -> tuple[datetime | None, datetime | None]:
    """The data's own time column as a STAC range, left off when the values are no timestamp."""
    if report.temporal is None or not (report.temporal.min and report.temporal.max):
        return None, None
    try:
        return (
            parse_utc(report.temporal.min),
            parse_utc(report.temporal.max),
        )
    except ValueError:
        logger.warning(
            "temporal column %s holds no timestamp (%s to %s); leaving the STAC range off",
            report.temporal.column,
            report.temporal.min,
            report.temporal.max,
        )
        return None, None
