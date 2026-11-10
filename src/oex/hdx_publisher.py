"""HDX dataset and resource publication. Imports hdx-python-api lazily."""

import functools
import json
import os
import re
import time
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from oex import http
from oex.config.schema import CategoryConfig, HdxConfig, RootConfig, S3Config
from oex.logging_setup import get_logger
from oex.naming import (
    ASSET_TITLES,
    COMBINED_SLUG,
    category_dataset_name,
    category_label,
    combined_dataset_name,
    layer_dataset_name,
    layer_label,
    resource_prefix,
    slugify,
    split_artifact_filename,
)
from oex.palette import DEFAULT_PALETTE
from oex.s3 import category_artifact_key, list_area_keys, resolve_with_bucket
from oex.s3 import upload as s3_upload
from oex.sources.labels import RUN_SOURCES, SOURCE_LABELS, long_label, short_label
from oex.stac import collection_filename
from oex.timestamps import parse_utc
from oex.writers import GEOMETRY_LABELS

if TYPE_CHECKING:
    from hdx.data.resource import Resource

    from oex.report import SourceMetadata
    from oex.report.page import Download, PageLayer

logger = get_logger(__name__)

_HDX_PUBLISH_BACKOFF_SECONDS = (5, 15, 45, 135)
_HDX_TRANSIENT_HTTP_STATUSES = {429, 502, 503, 504}


@functools.cache
def hdx_site_url(site: str) -> str:
    """Base URL of an HDX site named as hdx-python-api names it, such as prod, stage or dev."""
    from hdx.api.configuration import Configuration

    return Configuration(hdx_site=site, hdx_read_only=True, user_agent="oex").get_hdx_site_url()


def hdx_dataset_url(site: str, dataset_name: str) -> str:
    return f"{hdx_site_url(site)}/dataset/{dataset_name}"


def links_related_datasets(cfg: RootConfig) -> bool:
    """Only a run publishing per-category datasets to HDX has sibling pages to link, and the
    bucket's STAC Collections are how it knows which siblings exist."""
    return (
        cfg.output.report.related_datasets
        and cfg.hdx.push
        and not cfg.hdx.combine
        and cfg.output.s3.enabled
    )


def published_collections(cfg: RootConfig) -> frozenset[tuple[str, str]]:
    """(category name, source) for every category Collection in the bucket, which is what a
    related link may point at; an empty layer publishes none."""
    if not links_related_datasets(cfg):
        return frozenset()
    keys = list_area_keys(cfg.output.s3, cfg.iso3)
    _, prefix, _, _, _ = resolve_with_bucket(cfg.output.s3)
    turned_off = turned_off_sources(cfg).names
    return frozenset(
        (category.name, source)
        for category in cfg.categories
        for source in SOURCE_LABELS
        if source not in turned_off
        and category_artifact_key(
            cfg.output.s3,
            prefix,
            cfg.iso3,
            slugify(category.name),
            collection_filename(layer_dataset_name(cfg, category), source),
        )
        in keys
    )


def related_dataset_links(
    cfg: RootConfig, current: CategoryConfig, published: frozenset[tuple[str, str]]
) -> list[tuple[str, str]]:
    if not links_related_datasets(cfg):
        return []
    categories_with_data = {name for name, _ in published}
    links: list[tuple[str, str]] = []
    for category in cfg.categories:
        if category.name == current.name or category.name not in categories_with_data:
            continue
        href = hdx_dataset_url(cfg.hdx.site, category_dataset_name(cfg, category))
        links.append((category_label(category), href))
    return links


_SUMMARY_CHARS = 240


FORMAT_LABEL = {
    "gpkg": "GeoPackage",
    "shp": "Shapefile",
    "geojson": "GeoJSON",
    "kml": "KML",
    "fgb": "FlatGeobuf",
    "pmtiles": "PMTiles",
    "geoparquet": "GeoParquet",
    "json": "JSON",
    "html": "HTML",
}


def country_name(iso3: str, *, dataset_name: str | None = None) -> str:
    # dataset_name fixes ISO inversions like DRC and names sub-national exports.
    if dataset_name:
        return dataset_name
    import pycountry

    record = pycountry.countries.get(alpha_3=iso3.upper())
    if record is None:
        return iso3.upper()
    return getattr(record, "common_name", None) or record.name


SOURCE_RANK = {"osm": 0, "overture": 1}


def combined_title(cfg: RootConfig, place: str, hdx_source: str) -> str:
    if cfg.hdx.combined.title:
        return cfg.hdx.combined.title.format(country=place, iso3=cfg.iso3.upper())
    return f"{place} {hdx_source} vector data"


def _apply_custom_viz(dataset, url: str) -> None:  # noqa: ANN001 - hdx Dataset
    # HDX shows the custom visualisation only when the resource preview is off.
    # set_custom_viz alone leaves dataset_preview on 'first_resource'.
    dataset.set_custom_viz(url)
    dataset.preview_off()


def dataset_title(cfg: RootConfig, category: CategoryConfig) -> str:
    label = category_label(category)
    place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
    if cfg.hdx.title_template:
        return cfg.hdx.title_template.format(
            country=place,
            category=label,
            iso3=cfg.iso3.upper(),
        )
    return f"{label} of {place or cfg.iso3.upper()}"


@dataclass
class PublishContext:
    snapshot_date: datetime
    source_name: str = ""
    metadata_json_path: Path | None = None
    category_report_enabled: bool = False
    output_dir: Path | None = None
    s3: S3Config | None = None
    # When both are set, they replace snapshot_date as the HDX time period.
    temporal_min: datetime | None = None
    temporal_max: datetime | None = None
    # Export boundary as (min_lon, min_lat, max_lon, max_lat), used to frame the map.
    boundary_bbox: tuple[float, float, float, float] | None = None
    boundary_geojson: str | None = None
    published: frozenset[tuple[str, str]] = frozenset()


def apply_time_period(dataset, publish_context: PublishContext, start: str | None) -> None:  # noqa: ANN001 - hdx Dataset
    if publish_context.temporal_min is not None and publish_context.temporal_max is not None:
        dataset.set_time_period(publish_context.temporal_min, publish_context.temporal_max)
    elif start is not None:
        first_day = datetime.fromisoformat(start)
        if first_day.date() > publish_context.snapshot_date.date():
            raise ValueError(
                f"hdx.time_period_start {start} is after the snapshot {publish_context.snapshot_date.date()}"
            )
        dataset.set_time_period(first_day, publish_context.snapshot_date)
    else:
        dataset.set_time_period(publish_context.snapshot_date)


@dataclass(frozen=True)
class TurnedOffSources:
    """Sources the config turns off, and the prefix and categories that mark a file oex wrote."""

    names: frozenset[str]
    prefix: str
    category_slugs: frozenset[str]

    def owns(self, resource) -> bool:  # noqa: ANN001 - hdx Resource
        """Only files oex named for a configured category count, so a file added by hand stays."""
        if not self.names:
            return False
        filename = _resource_filename(resource)
        if self._is_collection(filename):
            return True
        layer = _resource_slug(resource, self.prefix, self.category_slugs)
        if layer is None:
            return False
        slug, source = layer
        category, _, geometry = slug.rpartition("_")
        is_export = (
            _layer_parquet_slug(resource) is not None
            or split_artifact_filename(filename)[2] in ASSET_TITLES
        )
        return (
            is_export
            and source in self.names
            and (
                slug in self.category_slugs
                or (geometry in GEOMETRY_LABELS and category in self.category_slugs)
            )
        )

    def _is_collection(self, filename: str) -> bool:
        return any(
            filename == collection_filename(f"{self.prefix}{part}", source)
            for source in self.names
            for part in ["", *(f"_{slug}" for slug in self.category_slugs)]
        )


def turned_off_sources(cfg: RootConfig) -> TurnedOffSources:
    return TurnedOffSources(
        names=frozenset(name for name in RUN_SOURCES if not cfg.source[name].enabled),
        prefix=resource_prefix(cfg),
        category_slugs=frozenset(slugify(category.name) for category in cfg.categories),
    )


def span_other_sources(  # noqa: ANN001 - hdx Dataset and Resources
    dataset,
    held: list,
    hdx_dataset_name: str,
    source_name: str,
    *,
    turned_off: frozenset[str],
) -> None:
    """Widen the time period over every other source's Collection on the dataset, so the source
    that publishes last does not narrow it to its own snapshot."""
    # Collection files name their source last, whatever folder or dataset name precedes it.
    other_suffixes = tuple(
        collection_filename("", other)
        for other in SOURCE_LABELS
        if other != source_name and other not in turned_off
    )
    others = [
        resource for resource in held if _resource_filename(resource).endswith(other_suffixes)
    ]
    if not others:
        return
    period = dataset.get_time_period()
    start, end = period["startdate"], period["enddate"]
    for resource in others:
        try:
            interval = _download_json(resource["url"])["extent"]["temporal"]["interval"]
            ((first, last),) = interval
            start, end = min(start, parse_utc(first)), max(end, parse_utc(last))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"HDX dataset {hdx_dataset_name}: could not read the time period of "
                f"{_resource_filename(resource)}: {exc}"
            ) from exc
    dataset.set_time_period(start, end)


def _category_summary(category: CategoryConfig) -> str:
    """Resource description for a category: `hdx.summary`, else the opening of its notes."""
    if category.hdx.summary:
        return " ".join(category.hdx.summary.split())
    notes = " ".join((category.hdx.notes or "").split())
    if not notes:
        return f"{category_label(category)} for this area."
    notes = _strip_tag_names(notes, category)
    notes = re.sub(r"\s+", " ", notes).replace(" ,", ",").replace(" :", ":").strip()
    chosen: list[str] = []
    for sentence in notes.split(". "):
        if chosen and len(". ".join([*chosen, sentence])) > _SUMMARY_CHARS:
            break
        chosen.append(sentence)
    return ". ".join(chosen).rstrip(".") + "."


def _strip_tag_names(notes: str, category: CategoryConfig) -> str:
    """Drop the OSM keys a layer selects on; a reader wants the subject, not the query."""
    keys = {key.split(":")[-1] for key in category.osm.filter} | set(category.osm.filter)
    span = re.compile(r"`([^`]+)`")
    kept = span.sub(
        lambda match: "" if match.group(1).split("=")[0].strip(":") in keys else match.group(0),
        notes,
    )
    kept = re.sub(r"\s*(tagged(\s+via| with| as)?|via)\s*(([,]|or|and)\s*)*(?=[.,:;]|$)", "", kept)
    return re.sub(r"[ ]*(,\s*)+(?=[.,:;])|(?<=\s)(,\s*)+", "", kept)


def _area_of_interest_payload(boundary_geojson: str, buffer_meters: float) -> tuple[str, str]:
    """The area of interest as a FeatureCollection carrying its area, plus the resource
    description."""
    from oex.boundary import area_sq_km

    geometry = json.loads(boundary_geojson)
    area = area_sq_km(geometry)
    widened = ", widened beyond the source geometry" if buffer_meters else ""
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"area_sq_km": round(area, 2), "buffer_meters": buffer_meters},
                "geometry": geometry,
            }
        ],
    }
    description = (
        f"The boundary every layer in this dataset is extracted from{widened}. "
        "This is the outline drawn on the map preview."
    )
    return json.dumps(collection, ensure_ascii=False), description


@dataclass
class ExtraResource:
    path: Path
    format_name: str
    description: str


@dataclass
class CombinedCategory:
    category: CategoryConfig
    zip_paths: list[Path]
    metadata_json_path: Path | None = None
    geoparquet_url: str | None = None
    geoparquet_path: Path | None = None


class HdxPublisher:
    def __init__(self, hdx_config: HdxConfig):
        if not hdx_config.push:
            raise ValueError("HdxPublisher constructed but hdx.push is false")
        api_key = hdx_config.api_key or os.environ.get("HDX_API_KEY")
        owner_org = hdx_config.owner_org or os.environ.get("HDX_OWNER_ORG")
        maintainer = hdx_config.maintainer or os.environ.get("HDX_MAINTAINER")
        if not (api_key and owner_org and maintainer):
            raise ValueError(
                "hdx.push is enabled but HDX credentials (api_key, owner_org, "
                "maintainer) are missing. Set them in the YAML or environment."
            )

        _preflight_check(api_key, owner_org, maintainer, hdx_config.site)

        from hdx.api.configuration import Configuration

        # hdx-python-api keeps one global configuration and refuses a second create; a process
        # can build several publishers, one per source or config.
        Configuration.delete()
        Configuration.create(
            hdx_site=hdx_config.site,
            hdx_key=api_key,
            user_agent=hdx_config.user_agent,
        )
        self._owner_org = owner_org
        self._maintainer = maintainer
        logger.info("HDX session ready: site=%s org=%s", hdx_config.site, owner_org)

    def publish(
        self,
        cfg: RootConfig,
        category: CategoryConfig,
        zip_paths: list[Path],
        publish_context: PublishContext,
        extra_resources: list[ExtraResource] | None = None,
        geoparquet_url: str | None = None,
        geoparquet_path: Path | None = None,
    ) -> str:
        category_slug = slugify(category.name)
        hdx_dataset_name = category_dataset_name(cfg, category)
        dataset = self._build_dataset_object(cfg, category, hdx_dataset_name, publish_context)
        self._add_layer_resources(
            dataset, cfg.iso3, category, zip_paths, publish_context, geoparquet_url, geoparquet_path
        )
        self._add_category_file_resources(
            dataset, cfg.iso3, category, publish_context, extra_resources
        )

        # A category's dataset holds every source that publishes it, so an OSM run
        # must not delete the Overture resources already there. Pruning is opt-in.
        rebuilt = publish_context.category_report_enabled and publish_context.output_dir is not None
        self._create_or_update(
            dataset,
            hdx_dataset_name,
            remove_additional=cfg.hdx.purge_existing_resources,
            source_name=publish_context.source_name,
            turned_off=turned_off_sources(cfg),
            keep_files=frozenset({f"{hdx_dataset_name}_report.html"} if rebuilt else ()),
        )

        if rebuilt:
            self._build_and_publish_category_report(
                hdx_dataset_name=hdx_dataset_name,
                category=category,
                cfg=cfg,
                category_slug=category_slug,
                output_dir=publish_context.output_dir,
                s3_config=publish_context.s3,
                boundary_bbox=publish_context.boundary_bbox,
                published=publish_context.published,
            )
        return hdx_dataset_name

    def _add_layer_resources(  # noqa: ANN001 - hdx Dataset
        self,
        dataset,
        iso3: str,
        category: CategoryConfig,
        zip_paths: list[Path],
        publish_context: PublishContext,
        geoparquet_url: str | None,
        geoparquet_path: Path | None,
    ) -> None:
        """A layer's download artifacts, and its staged GeoParquet when it has one."""
        category_slug = slugify(category.name)
        for zip_path in zip_paths:
            resource = self._make_resource_for_zip(
                zip_path, category, publish_context, iso3, category_slug
            )
            dataset.add_update_resource(resource)
        if geoparquet_url:
            dataset.add_update_resource(
                self._make_resource_for_geoparquet(
                    category, geoparquet_url, publish_context, geoparquet_path
                )
            )

    def _add_category_file_resources(  # noqa: ANN001 - hdx Dataset
        self,
        dataset,
        iso3: str,
        category: CategoryConfig,
        publish_context: PublishContext,
        extra_resources: list[ExtraResource] | None,
    ) -> None:
        """The category's tiles and STAC Collection, uploaded beside its artifacts."""
        category_slug = slugify(category.name)
        layer_title = layer_label(category, publish_context.source_name)
        for extra in extra_resources or []:
            resource = self._make_resource_for_path(
                path=extra.path,
                format_name=extra.format_name,
                description=extra.description,
                publish_context=publish_context,
                iso3=iso3,
                category_slug=category_slug,
                name=f"{layer_title}, Vector Tiles ({FORMAT_LABEL.get(extra.format_name, extra.format_name.upper())})",
            )
            dataset.add_update_resource(resource)

        if publish_context.metadata_json_path is not None:
            resource = self._make_resource_for_path(
                path=publish_context.metadata_json_path,
                format_name="json",
                description=(
                    f"STAC Collection for {category_label(category)}: its layers, columns "
                    "and downloads, for STAC clients and code."
                ),
                publish_context=publish_context,
                iso3=iso3,
                category_slug=category_slug,
                name=f"{layer_title}, STAC Collection",
            )
            dataset.add_update_resource(resource)

    def refresh_metadata(
        self,
        cfg: RootConfig,
        category: CategoryConfig,
        publish_context: PublishContext,
    ) -> str | None:
        """Restate the snapshot date on a dataset whose files stay as they are, so it does not
        drift past the frequency it advertises. None when nothing is published yet."""
        from hdx.data.dataset import Dataset

        hdx_dataset_name = category_dataset_name(cfg, category)
        existing = Dataset.read_from_hdx(hdx_dataset_name)
        if existing is None:
            logger.info("%s not on HDX; nothing to refresh", hdx_dataset_name)
            return None

        existing_org = existing.get("owner_org")
        if existing_org and existing_org != self._owner_org:
            raise RuntimeError(
                f"HDX dataset {hdx_dataset_name} exists under a different organisation "
                f"(owner_org={existing_org!r}; you are publishing as {self._owner_org!r})."
            )

        turned_off = turned_off_sources(cfg)
        apply_time_period(existing, publish_context, cfg.hdx.time_period_start)
        span_other_sources(
            existing,
            existing.get_resources(),
            hdx_dataset_name,
            publish_context.source_name,
            turned_off=turned_off.names,
        )
        _hdx_publish_with_retry(
            lambda: existing.update_in_hdx(
                update_resources=False,
                match_resources_by_metadata=True,
                hxl_update=False,
            ),
            label=f"refresh {hdx_dataset_name}",
        )
        _remove_turned_off_sources(existing, turned_off, hdx_dataset_name)
        self._order_resources(existing, hdx_dataset_name)
        logger.info(
            "%s refreshed snapshot date to %s", hdx_dataset_name, publish_context.snapshot_date
        )
        return hdx_dataset_name

    def _create_or_update(  # noqa: ANN001 - hdx Dataset
        self,
        dataset,
        hdx_dataset_name: str,
        *,
        remove_additional: bool,
        source_name: str,
        turned_off: TurnedOffSources,
        keep_files: frozenset[str] = frozenset(),
        category_labels: Sequence[str] = (),
    ) -> None:
        from hdx.data.dataset import Dataset

        existing = Dataset.read_from_hdx(hdx_dataset_name)
        if existing is not None:
            existing_org = existing.get("owner_org")
            if existing_org and existing_org != self._owner_org:
                raise RuntimeError(
                    f"HDX dataset {hdx_dataset_name} exists under a different organisation "
                    f"(owner_org={existing_org!r}; you are publishing as "
                    f"{self._owner_org!r}). Pick a different `key` in your config "
                    "to namespace your datasets, or have HDX transfer ownership."
                )
            dataset["id"] = existing["id"]
            if remove_additional:
                # A page this publish rebuilds next is kept, so it is updated in place
                # instead of being purged and re-created under a new id.
                for resource in existing.get_resources():
                    if _resource_filename(resource) in keep_files:
                        dataset.add_update_resource(resource, ignore_datasetid=True)
            _reuse_resource_ids(dataset.get_resources(), existing.get_resources())
            span_other_sources(
                dataset,
                existing.get_resources(),
                hdx_dataset_name,
                source_name,
                turned_off=turned_off.names,
            )
            written = _written_files(dataset.get_resources())
            logger.info(
                "Updating HDX dataset %s with %d resources",
                hdx_dataset_name,
                len(dataset.get_resources()),
            )
            _hdx_publish_with_retry(
                lambda: dataset.update_in_hdx(
                    remove_additional_resources=remove_additional,
                    match_resources_by_metadata=True,
                    hxl_update=False,
                ),
                label=f"update {hdx_dataset_name}",
            )
            _remove_superseded_copies(dataset, written, hdx_dataset_name)
            _remove_turned_off_sources(dataset, turned_off, hdx_dataset_name)
        else:
            logger.info(
                "Creating HDX dataset %s with %d resources",
                hdx_dataset_name,
                len(dataset.get_resources()),
            )
            _hdx_publish_with_retry(
                lambda: dataset.create_in_hdx(
                    allow_no_resources=False,
                    hxl_update=False,
                ),
                label=f"create {hdx_dataset_name}",
            )
        self._order_resources(dataset, hdx_dataset_name, category_labels)

    @staticmethod
    def _order_resources(  # noqa: ANN001 - hdx Dataset
        dataset, hdx_dataset_name: str, category_labels: Sequence[str] = ()
    ) -> None:
        """Put resources in `_resource_order_key` order. A resource updated in place keeps its
        old position, so the order is set after the write."""
        resources = dataset.get_resources() or []
        if len(resources) < 2:
            return
        ordered = sorted(
            resources, key=lambda resource: _resource_order_key(resource, category_labels)
        )
        if [resource["id"] for resource in ordered] == [resource["id"] for resource in resources]:
            return
        _hdx_publish_with_retry(
            lambda: dataset.reorder_resources([resource["id"] for resource in ordered]),
            label=f"reorder {hdx_dataset_name}",
        )
        logger.info("%s: ordered %d resources", hdx_dataset_name, len(ordered))

    def publish_combined(
        self,
        cfg: RootConfig,
        entries: list[CombinedCategory],
        publish_context: PublishContext,
        *,
        pmtiles_path: Path | None = None,
        metadata_path: Path | None = None,
        page_enabled: bool = False,
    ) -> str:
        if not entries:
            raise ValueError("publish_combined called with no categories")
        hdx_dataset_name = combined_dataset_name(cfg)
        dataset = self._build_combined_dataset_object(
            cfg, entries, hdx_dataset_name, publish_context
        )
        if pmtiles_path is not None:
            dataset.add_update_resource(
                self._combined_tiles_resource(cfg.iso3, pmtiles_path, publish_context)
            )
        # The map preview draws this outline, so the geometry behind it is published
        # alongside rather than left as something only the picture shows.
        if publish_context.boundary_geojson and publish_context.output_dir is not None:
            dataset.add_update_resource(
                self._area_of_interest_resource(
                    cfg,
                    publish_context.boundary_geojson,
                    publish_context.output_dir / f"{hdx_dataset_name}_aoi.geojson",
                    publish_context,
                )
            )
        for entry in entries:
            self._add_layer_resources(
                dataset,
                cfg.iso3,
                entry.category,
                entry.zip_paths,
                publish_context,
                entry.geoparquet_url,
                entry.geoparquet_path,
            )
        if metadata_path is not None:
            dataset.add_update_resource(
                self._combined_metadata_resource(cfg.iso3, metadata_path, publish_context)
            )

        # A combined dataset accumulates across sources: an Overture run must not
        # delete the resources an earlier OSM run put here. Pruning is opt-in.
        rebuilt = page_enabled and publish_context.output_dir is not None
        self._create_or_update(
            dataset,
            hdx_dataset_name,
            remove_additional=cfg.hdx.purge_existing_resources,
            source_name=publish_context.source_name,
            turned_off=turned_off_sources(cfg),
            keep_files=frozenset({f"{hdx_dataset_name}_overview.html"} if rebuilt else ()),
            category_labels=[category_label(category) for category in cfg.categories],
        )

        if rebuilt:
            self._build_and_publish_page(
                hdx_dataset_name=hdx_dataset_name,
                cfg=cfg,
                entries=entries,
                pmtiles_layer=pmtiles_path.stem if pmtiles_path is not None else None,
                publish_context=publish_context,
            )
        return hdx_dataset_name

    def _combined_tiles_resource(  # noqa: ANN202 - hdx-python-api Resource
        self, iso3: str, pmtiles_path: Path, publish_context: PublishContext
    ):
        return self._make_resource_for_path(
            path=pmtiles_path,
            format_name="pmtiles",
            description=(
                "Every layer in one tile archive, with a category and source "
                "attribute per feature. For web maps."
            ),
            publish_context=publish_context,
            iso3=iso3,
            category_slug=COMBINED_SLUG,
            name="Vector Tiles, All Layers (PMTiles)",
        )

    def _area_of_interest_resource(  # noqa: ANN202 - hdx-python-api Resource
        self,
        cfg: RootConfig,
        boundary_geojson: str,
        aoi_path: Path,
        publish_context: PublishContext,
    ):
        payload, description = _area_of_interest_payload(
            boundary_geojson, cfg.boundary.buffer_meters
        )
        aoi_path.write_text(payload, encoding="utf-8")
        return self._make_resource_for_path(
            path=aoi_path,
            format_name="geojson",
            description=description,
            publish_context=publish_context,
            iso3=cfg.iso3,
            category_slug=COMBINED_SLUG,
            name="Area of Interest, GeoJSON",
        )

    def _combined_metadata_resource(  # noqa: ANN202 - hdx-python-api Resource
        self, iso3: str, metadata_path: Path, publish_context: PublishContext
    ):
        return self._make_resource_for_path(
            path=metadata_path,
            format_name="json",
            description=(
                "Field-level metadata for every "
                f"{long_label(publish_context.source_name)} layer: "
                "feature counts, geometry types, columns and value distributions."
            ),
            publish_context=publish_context,
            iso3=iso3,
            category_slug=COMBINED_SLUG,
            name=f"STAC Collection ({short_label(publish_context.source_name)})",
        )

    def update_metadata(
        self, cfg: RootConfig, *, dry_run: bool = False, prune: bool = False
    ) -> tuple[str, int, int]:
        """Rewrite dataset and resource text from the config, leaving files and ids alone;
        returns the dataset name, how many resources changed and how many were pruned."""
        from hdx.data.dataset import Dataset

        hdx_dataset_name = combined_dataset_name(cfg)
        dataset = Dataset.read_from_hdx(hdx_dataset_name)
        if dataset is None:
            raise RuntimeError(
                f"HDX dataset {hdx_dataset_name} not found; publish it before updating text"
            )

        _apply_dataset_text(dataset, cfg)
        by_slug = {slugify(category.name): category for category in cfg.categories}
        changed, pruned = _sync_resource_text(
            dataset, hdx_dataset_name, by_slug, dry_run=dry_run, prune=prune
        )

        if dry_run:
            return hdx_dataset_name, changed, pruned
        _hdx_publish_with_retry(
            lambda: dataset.update_in_hdx(
                update_resources=False, hxl_update=False, remove_additional_resources=False
            ),
            label=f"metadata {hdx_dataset_name}",
        )
        self._order_resources(
            dataset, hdx_dataset_name, [category_label(category) for category in cfg.categories]
        )
        logger.info("%s: metadata updated, %d resource(s) changed", hdx_dataset_name, changed)
        return hdx_dataset_name, changed, pruned

    def _make_resource_for_geoparquet(
        self,
        category: CategoryConfig,
        url: str,
        publish_context: PublishContext,
        path: Path | None,
    ):  # noqa: ANN202 - hdx-python-api Resource
        """Point at the layer GeoParquet already staged on S3, unzipped and read in place."""
        from hdx.data.resource import Resource

        payload = geoparquet_resource_payload(category, url, publish_context.source_name)
        if path is not None:
            payload["size"] = path.stat().st_size
        resource = Resource(payload)
        resource.mark_data_updated()
        return resource

    def _build_combined_dataset_object(
        self,
        cfg: RootConfig,
        entries: list[CombinedCategory],
        hdx_dataset_name: str,
        publish_context: PublishContext,
    ):  # noqa: ANN202 - hdx-python-api Dataset
        from hdx.data.dataset import Dataset

        place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
        hdx_source = long_label(publish_context.source_name)
        title = combined_title(cfg, place, hdx_source)

        labels = [category_label(entry.category) for entry in entries]
        notes = cfg.hdx.combined.notes or (
            f"Combined {hdx_source} vector export for {place}. "
            f"Layers: {', '.join(labels)}. Each layer is provided as GIS downloads; "
            "the interactive overview maps all layers together."
        )
        dataset_args: dict[str, object] = {
            "title": title,
            "name": hdx_dataset_name,
            "notes": notes,
            "caveats": cfg.hdx.combined.caveats or entries[0].category.hdx.caveats,
            "private": False,
            "dataset_source": cfg.hdx.combined.source or hdx_source,
            "methodology": cfg.hdx.methodology,
            "methodology_other": cfg.hdx.methodology_other,
            "owner_org": self._owner_org,
            "maintainer": self._maintainer,
            "subnational": cfg.subnational,
        }
        licenses = {entry.category.hdx.license for entry in entries}
        if len(licenses) == 1 and next(iter(licenses)) == "hdx-odc-odbl":
            dataset_args["license_id"] = "hdx-odc-odbl"
        elif len(licenses) == 1:
            dataset_args["license_id"] = "hdx-other"
            dataset_args["license_other"] = next(iter(licenses))
        else:
            dataset_args["license_id"] = "hdx-other"
            dataset_args["license_other"] = (
                "Layers carry individual licences; see each layer's metadata."
            )

        dataset = Dataset(dataset_args)
        apply_time_period(dataset, publish_context, cfg.hdx.time_period_start)
        dataset.set_expected_update_frequency(cfg.frequency)
        dataset.add_other_location(cfg.iso3.upper())
        seen_tags: set[str] = set()
        category_tags = (tag for entry in entries for tag in entry.category.hdx.tags)
        for tag in (*cfg.hdx.combined.tags, *category_tags):
            if tag not in seen_tags:
                dataset.add_tag(tag)
                seen_tags.add(tag)
        return dataset

    def _read_dataset(self, hdx_dataset_name: str, purpose: str):  # noqa: ANN202 - hdx Dataset
        from hdx.data.dataset import Dataset

        dataset = Dataset.read_from_hdx(hdx_dataset_name)
        if dataset is None:
            raise RuntimeError(f"HDX dataset {hdx_dataset_name} not visible before {purpose} build")
        return dataset

    def _publish_html_viz(
        self,
        *,
        dataset,  # noqa: ANN001 - hdx Dataset
        hdx_dataset_name: str,
        html_path: Path,
        description: str,
        iso3: str,
        category_slug: str,
        s3_config: S3Config | None,
        name: str | None = None,
        category_labels: Sequence[str] = (),
    ) -> None:
        """Attach an HTML page to the dataset and make it the custom visualisation. HDX serves
        uploaded HTML as text/html with SAMEORIGIN, so the resource URL can be iframed."""
        viz_context = PublishContext(
            snapshot_date=datetime.now(),
            source_name=category_slug,
            s3=s3_config,
        )
        resource = self._make_resource_for_path(
            path=html_path,
            format_name="html",
            description=description,
            publish_context=viz_context,
            iso3=iso3,
            category_slug=category_slug,
            name=name,
        )
        _reuse_resource_ids([resource], dataset.get_resources())
        written = _written_files([resource])
        dataset.add_update_resource(resource)
        url = resource["url"] if "url" in resource.data else None
        if url:
            logger.info("Setting customviz: %s", url)
            _apply_custom_viz(dataset, url)
        _hdx_publish_with_retry(
            lambda: dataset.update_in_hdx(
                remove_additional_resources=False,
                match_resources_by_metadata=True,
                hxl_update=False,
            ),
            label=f"{category_slug} update {hdx_dataset_name}",
        )
        _remove_superseded_copies(dataset, written, hdx_dataset_name)
        # A page uploaded for the first time lands last, after the order was set.
        self._order_resources(dataset, hdx_dataset_name, category_labels)
        # An uploaded resource only gets its URL from HDX after the update above.
        if url is None:
            self._set_customviz_after_upload(hdx_dataset_name, html_path.name)

    def _build_and_publish_page(
        self,
        *,
        hdx_dataset_name: str,
        cfg: RootConfig,
        entries: list[CombinedCategory],
        pmtiles_layer: str | None,
        publish_context: PublishContext,
    ) -> None:
        from oex.report.page import render_dataset_page

        dataset = self._read_dataset(hdx_dataset_name, "page")
        resources = dataset.get_resources() or []
        pmtiles_url = (
            _resource_url(resources, f"{pmtiles_layer}.pmtiles") if pmtiles_layer else None
        )
        tileset = (pmtiles_url, pmtiles_layer) if pmtiles_url and pmtiles_layer else None

        # Resource filenames start with the dataset name, which `hdx.combined.name` can set.
        prefix = f"{hdx_dataset_name}_"
        by_slug: dict[str, list[PublishedLayer]] = {}
        for published in _load_source_metadata(resources, hdx_dataset_name):
            # Every category on the dataset, not just this run's: a later run of the
            # other source must describe the layers the tileset already carries.
            slug = _slug_from_metadata_name(published.name, prefix, cfg.categories)
            if slug is not None:
                by_slug.setdefault(slug, []).append(published)

        layers = _page_layers(cfg, by_slug, tileset)
        if not layers:
            raise RuntimeError(
                f"HDX dataset {hdx_dataset_name}: dataset page requested but no metadata.json resources found"
            )

        place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
        page_title = combined_title(cfg, place, long_label(publish_context.source_name))
        # Every source rebuilds this page, so its description leaves the source out.
        description_subject = page_title if cfg.hdx.combined.title else place
        html = render_dataset_page(
            title=page_title,
            layers=layers,
            theme=cfg.output.report.theme,
            boundary_bbox=publish_context.boundary_bbox,
            boundary_geojson=publish_context.boundary_geojson,
            map_assets=cfg.output.report.map_assets,
            dataset_url=hdx_dataset_url(cfg.hdx.site, hdx_dataset_name),
        )
        assert publish_context.output_dir is not None
        page_path = publish_context.output_dir / f"{hdx_dataset_name}_overview.html"
        page_path.write_text(html, encoding="utf-8")
        logger.info("Built dataset page (%d layers) -> %s", len(layers), page_path)

        self._publish_html_viz(
            dataset=dataset,
            hdx_dataset_name=hdx_dataset_name,
            html_path=page_path,
            description=(
                f"Interactive map and data quality report for {description_subject}: every layer on one "
                "map, with feature counts, attribute completeness and source per layer. "
                "Open this first to see what the dataset contains."
            ),
            iso3=cfg.iso3,
            category_slug="overview",
            s3_config=publish_context.s3,
            name="Data Quality Report and Interactive Map",
            category_labels=[category_label(category) for category in cfg.categories],
        )

    def _set_customviz_after_upload(self, hdx_dataset_name: str, resource_name: str) -> None:
        from hdx.data.dataset import Dataset

        dataset = Dataset.read_from_hdx(hdx_dataset_name)
        if dataset is None:
            return
        resource = next(
            (
                candidate
                for candidate in (dataset.get_resources() or [])
                if candidate["name"] == resource_name
            ),
            None,
        )
        url = resource.get("url") if resource else None
        if not url:
            logger.warning(
                "customviz: overview resource %s has no URL yet on %s",
                resource_name,
                hdx_dataset_name,
            )
            return
        logger.info("Setting customviz (post-upload): %s", url)
        _apply_custom_viz(dataset, url)
        _hdx_publish_with_retry(
            lambda: dataset.update_in_hdx(
                remove_additional_resources=False,
                match_resources_by_metadata=True,
                hxl_update=False,
            ),
            label=f"customviz {hdx_dataset_name}",
        )

    def _build_dataset_object(
        self,
        cfg: RootConfig,
        category: CategoryConfig,
        hdx_dataset_name: str,
        publish_context: PublishContext,
    ):  # noqa: ANN202 - hdx-python-api Dataset, imported lazily
        from hdx.data.dataset import Dataset

        title = dataset_title(cfg, category)
        hdx_source = category.hdx.dataset_source or long_label(publish_context.source_name)
        dataset_args: dict[str, object] = {
            "title": title,
            "name": hdx_dataset_name,
            "notes": category.hdx.notes,
            "caveats": category.hdx.caveats,
            "private": False,
            "dataset_source": hdx_source,
            "methodology": cfg.hdx.methodology,
            "methodology_other": cfg.hdx.methodology_other,
            "owner_org": self._owner_org,
            "maintainer": self._maintainer,
            "subnational": cfg.subnational,
        }
        if category.hdx.license == "hdx-odc-odbl":
            dataset_args["license_id"] = "hdx-odc-odbl"
        else:
            dataset_args["license_id"] = "hdx-other"
            dataset_args["license_other"] = category.hdx.license
        if category.hdx.license_url:
            dataset_args["license_url"] = category.hdx.license_url

        dataset = Dataset(dataset_args)
        apply_time_period(dataset, publish_context, cfg.hdx.time_period_start)
        dataset.set_expected_update_frequency(cfg.frequency)
        dataset.add_other_location(cfg.iso3.upper())
        for tag in category.hdx.tags:
            dataset.add_tag(tag)
        return dataset

    def _make_resource_for_zip(
        self,
        zip_path: Path,
        category: CategoryConfig,
        publish_context: PublishContext,
        iso3: str,
        category_slug: str,
    ):  # noqa: ANN202 - hdx-python-api Resource
        _, source, format_name = split_artifact_filename(zip_path.name)
        name, description = layer_resource_text(category, source, format_name)
        return self._make_resource_for_path(
            path=zip_path,
            format_name=format_name,
            description=description,
            publish_context=publish_context,
            iso3=iso3,
            category_slug=category_slug,
            name=name,
        )

    def _make_resource_for_path(
        self,
        *,
        path: Path,
        format_name: str,
        description: str,
        publish_context: PublishContext,
        iso3: str,
        category_slug: str,
        name: str | None = None,
    ):  # noqa: ANN202 - hdx-python-api Resource
        from hdx.data.resource import Resource

        size_bytes = path.stat().st_size
        resource_data: dict[str, object] = {
            "name": name or path.name,
            "description": description,
            "format": format_name,
            "size": int(size_bytes),
        }
        if publish_context.s3 is not None and publish_context.s3.enabled:
            bucket, prefix, region, endpoint_url, acl = resolve_with_bucket(publish_context.s3)
            key = category_artifact_key(publish_context.s3, prefix, iso3, category_slug, path.name)
            url = s3_upload(
                path,
                bucket=bucket,
                key=key,
                region=region,
                endpoint_url=endpoint_url,
                acl=acl,
            )
            logger.info(
                "Uploaded %s to s3://%s/%s (%.2f MB)",
                path.name,
                bucket,
                key,
                size_bytes / (1024 * 1024),
            )
            resource_data["url"] = url
            resource = Resource(resource_data)
        else:
            resource = Resource(resource_data)
            resource.set_file_to_upload(str(path))
        resource.mark_data_updated()
        return resource

    def _build_and_publish_category_report(
        self,
        *,
        hdx_dataset_name: str,
        category: CategoryConfig,
        cfg: RootConfig,
        category_slug: str,
        output_dir: Path,
        s3_config: S3Config | None,
        boundary_bbox: tuple[float, float, float, float] | None = None,
        published: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        from oex.report.page import render_dataset_page

        dataset = self._read_dataset(hdx_dataset_name, "report")
        resources = dataset.get_resources() or []

        on_dataset = _load_source_metadata(resources, hdx_dataset_name)
        if not on_dataset:
            raise RuntimeError(
                f"HDX dataset {hdx_dataset_name}: category report requested but no "
                "metadata.json resources found"
            )
        tilesets = _category_tilesets(resources, category_slug)
        layers = _page_layers(cfg, {category_slug: on_dataset}, None)
        layers = [
            replace(layer, tileset=tilesets.get(layer.source.source_name)) for layer in layers
        ]

        report_path = output_dir / f"{hdx_dataset_name}_report.html"
        report_path.write_text(
            render_dataset_page(
                title=dataset_title(cfg, category),
                layers=layers,
                theme=cfg.output.report.theme,
                boundary_bbox=boundary_bbox,
                map_assets=cfg.output.report.map_assets,
                dataset_url=hdx_dataset_url(cfg.hdx.site, hdx_dataset_name),
                related=related_dataset_links(cfg, category, published),
            ),
            encoding="utf-8",
        )
        logger.info(
            "Built category report (%d source%s) -> %s",
            len(layers),
            "" if len(layers) == 1 else "s",
            report_path,
        )

        self._publish_html_viz(
            dataset=dataset,
            hdx_dataset_name=hdx_dataset_name,
            html_path=report_path,
            description=(
                f"Interactive map and data quality report for {category_label(category)}: "
                "feature counts, attribute completeness and value distributions."
            ),
            iso3=cfg.iso3,
            category_slug=category_slug,
            s3_config=s3_config,
            name=f"{category_label(category)}, Data Quality Report",
        )


def _hdx_publish_with_retry(call, label: str):  # noqa: ANN001 - lambda
    """Run an HDX publish call, backing off on connection errors and the statuses in
    _HDX_TRANSIENT_HTTP_STATUSES."""
    from hdx.data.hdxobject import HDXError

    last_error: Exception | None = None
    for attempt, delay_seconds in enumerate(_HDX_PUBLISH_BACKOFF_SECONDS, start=1):
        try:
            return call()
        except HDXError as exc:
            if not _is_transient_hdx_error(exc):
                raise
            last_error = exc
            if attempt < len(_HDX_PUBLISH_BACKOFF_SECONDS):
                logger.warning(
                    "HDX %s attempt %d/%d hit transient error (%s); retrying in %ds",
                    label,
                    attempt,
                    len(_HDX_PUBLISH_BACKOFF_SECONDS),
                    _summarise_hdx_error(exc),
                    delay_seconds,
                )
                time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def _is_transient_hdx_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, requests.exceptions.RetryError):
            return True
        if isinstance(current, requests.exceptions.ConnectionError):
            return True
        if isinstance(current, requests.exceptions.Timeout):
            return True
        if isinstance(current, requests.exceptions.HTTPError):
            response = getattr(current, "response", None)
            if response is not None and response.status_code in _HDX_TRANSIENT_HTTP_STATUSES:
                return True
        current = current.__cause__
    return False


def _summarise_hdx_error(exc: BaseException) -> str:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, requests.exceptions.RequestException):
            return f"{type(current).__name__}: {current}"
        current = current.__cause__
    return f"{type(exc).__name__}: {exc}"


def _download_json(url: str, *, timeout: float = 60.0) -> dict:
    response = http.session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _resource_url(resources: list, filename: str) -> str | None:  # noqa: ANN001 - hdx Resource
    """Look up by uploaded filename: resource names are human-readable titles."""
    resource = next(
        (candidate for candidate in resources if _resource_filename(candidate) == filename), None
    )
    return resource.get("url") if resource else None


def _category_tilesets(  # noqa: ANN001 - hdx Resource
    resources: list, category_slug: str
) -> dict[str, tuple[str, str]]:
    """Each source's tileset on a per-category dataset, `{key}_{iso3}_{slug}_{source}.pmtiles`,
    whose one layer is named for the category."""
    tilesets: dict[str, tuple[str, str]] = {}
    for resource in resources:
        name = _resource_filename(resource)
        if not name.endswith(".pmtiles"):
            continue
        stem = name[: -len(".pmtiles")]
        url = resource.get("url")
        if url:
            tilesets[stem.rsplit("_", 1)[-1]] = (url, category_slug)
    return tilesets


def _layer_parquet_slug(resource) -> tuple[str, str] | None:  # noqa: ANN001
    """(slug, source) for a staged GeoParquet, whose key carries no dataset prefix."""
    url = resource.get("url") or ""
    path = urllib.parse.unquote(url.split("?", 1)[0])
    match = re.search(r"/_layers/([^/]+)/([^/]+)\.parquet$", path)
    if match and match.group(1) in SOURCE_LABELS:
        return match.group(2), match.group(1)
    return None


def _resource_slug(
    resource,  # noqa: ANN001
    prefix: str,
    known_slugs: Iterable[str] = (),
) -> tuple[str, str] | None:
    """(category slug, source) a layer resource belongs to, or None when it is not a layer."""
    layer_parquet = _layer_parquet_slug(resource)
    if layer_parquet is not None:
        return layer_parquet
    filename = _resource_filename(resource)
    if not filename.startswith(f"{prefix}_"):
        return None
    slug, source, _ = split_artifact_filename(filename[len(prefix) + 1 :], known_slugs)
    if not slug or not source:
        return None
    return slug, source


def _is_dropped(layer: tuple[str, str], by_slug: dict) -> bool:
    """True when the config no longer publishes this category from this source."""
    slug, source = layer
    category = by_slug.get(slug)
    if category is None:
        return True
    block = getattr(category, source, None)
    return block is not None and not block.enabled


def layer_resource_text(category: CategoryConfig, source: str, format_name: str) -> tuple[str, str]:
    """HDX name and description for one artifact of a layer; `source` may be empty."""
    label = category_label(category)
    format_label = FORMAT_LABEL.get(format_name, format_name.upper())
    summary = _category_summary(category)
    # The format is in the name because ResourceMatcher merges same-named resources.
    if not source:
        return f"{label}, {format_label}", f"{summary} As {format_label}."
    return (
        f"{label} ({short_label(source)}), {format_label}",
        f"{summary} From {long_label(source)}, as {format_label}.",
    )


def geoparquet_resource_payload(category: CategoryConfig, url: str, source_name: str) -> dict:
    """Resource fields for a layer GeoParquet, which is published unzipped at its staged URL."""
    label = category_label(category)
    source = short_label(source_name)
    return {
        "name": f"{label} ({source}), GeoParquet",
        "description": (
            f"{label} as GeoParquet, served unzipped so a client can range-read it over "
            "HTTP instead of downloading the whole file."
        ),
        "format": "geoparquet",
        "url": url,
    }


def _resource_text(resource, hdx_dataset_name: str, by_slug: dict) -> tuple[str, str] | None:  # noqa: ANN001
    """Name and description for a layer resource, keyed off the filename rather than
    the display name, which is not an identifier."""
    layer_parquet = _layer_parquet_slug(resource)
    if layer_parquet is not None:
        slug, source = layer_parquet
        category = by_slug.get(slug)
        if category is None:
            return None
        payload = geoparquet_resource_payload(category, resource["url"], source)
        return payload["name"], payload["description"]

    filename = _resource_filename(resource)
    if not filename.startswith(f"{hdx_dataset_name}_"):
        return None
    slug, source, format_name = split_artifact_filename(
        filename[len(hdx_dataset_name) + 1 :], by_slug
    )
    category = by_slug.get(slug)
    if category is None or not source:
        return None
    return layer_resource_text(category, source, format_name)


def _reuse_resource_ids(new: list, existing: list) -> None:  # noqa: ANN001 - hdx Resources
    """HDX matches resources by id, then name, never by URL, so a file whose title changed
    would be added again beside its old entry; carrying the id over renames it in place."""
    by_location: dict[str, list] = {}
    for resource in existing:
        if (location := _resource_location(resource)) is not None:
            by_location.setdefault(location, []).append(resource)
    taken: set[str] = set()
    for resource in new:
        location = _resource_location(resource)
        if resource.get("id") or location is None:
            continue
        candidates = [
            candidate for candidate in by_location.get(location, []) if candidate["id"] not in taken
        ]
        if not candidates:
            continue
        same_name = [candidate for candidate in candidates if candidate["name"] == resource["name"]]
        match = (same_name or candidates)[0]
        resource["id"] = match["id"]
        taken.add(match["id"])


def _pending_upload(resource: "dict | Resource") -> str | None:
    """Only an hdx Resource can hold a file waiting to upload."""
    from hdx.data.resource import Resource

    return resource.get_file_to_upload() if isinstance(resource, Resource) else None


def _resource_location(resource) -> str | None:  # noqa: ANN001 - hdx Resource
    """The full URL for a linked file, since two sources stage `buildings.parquet` in different
    folders; only an upload to HDX itself, or one about to be, is known by filename alone."""
    url = (resource.get("url") or "").split("?")[0]
    if url and "/download/" not in url:
        return url
    upload = _pending_upload(resource)
    name = Path(upload).name if upload else urllib.parse.unquote(url.rsplit("/", 1)[-1])
    return f"upload:{name}" if name else None


def _file_key(resource) -> str:  # noqa: ANN001 - hdx Resource
    """The file a resource holds wherever it lives; a staged GeoParquet keeps its source folder,
    since every source stages `<slug>.parquet`."""
    layer_parquet = _layer_parquet_slug(resource)
    if layer_parquet is not None:
        slug, source = layer_parquet
        return f"_layers/{source}/{slug}.parquet"
    return _resource_filename(resource)


def _written_files(resources: list) -> set[tuple[str, str | None]]:  # noqa: ANN001 - hdx Resources
    return {(_file_key(resource), _resource_location(resource)) for resource in resources}


def _remove_superseded_copies(  # noqa: ANN001 - hdx Dataset
    dataset, written: set[tuple[str, str | None]], hdx_dataset_name: str
) -> None:
    """A dataset keeps one resource per file, so a copy of a file this publish wrote that sits
    at another location is deleted."""
    written_keys = {key for key, _ in written}
    superseded = [
        resource
        for resource in dataset.get_resources() or []
        if (key := _file_key(resource)) in written_keys
        and (key, _resource_location(resource)) not in written
    ]
    _delete_resources(dataset, superseded, hdx_dataset_name, "superseded by this publish")


def _remove_turned_off_sources(  # noqa: ANN001 - hdx Dataset
    dataset, turned_off: TurnedOffSources, hdx_dataset_name: str
) -> None:
    """The config decides which sources a dataset holds, so a source turned off loses its files."""
    owned = [resource for resource in dataset.get_resources() or [] if turned_off.owns(resource)]
    _delete_resources(dataset, owned, hdx_dataset_name, "its source is turned off in the config")


def _delete_resources(  # noqa: ANN001 - hdx Dataset and Resources
    dataset, resources: list, hdx_dataset_name: str, reason: str
) -> None:
    for resource in resources:
        logger.info(
            "%s: removing %s at %s, %s",
            hdx_dataset_name,
            resource["name"],
            resource.get("url"),
            reason,
        )
        deleted = _hdx_publish_with_retry(
            lambda resource=resource: dataset.delete_resource(resource),
            label=f"delete {resource['name']} from {hdx_dataset_name}",
        )
        if not deleted:
            raise RuntimeError(f"{hdx_dataset_name}: HDX did not delete {resource['name']}")


def _resource_order_key(  # noqa: ANN001 - hdx Resource
    resource, category_labels: Sequence[str] = ()
) -> tuple[int, int, int, int, int, str]:
    """The report or overview page first, then downloads in ASSET_TITLES order by source and
    geometry, then STAC Collections, then the rest; a combined dataset groups each category."""
    filename = _resource_filename(resource)
    name = (resource.get("name") or "").casefold()
    category = _category_rank(resource, category_labels)
    if filename.endswith(("_report.html", "_overview.html")):
        return 0, 0, 0, 0, 0, name
    if filename.endswith("_metadata.json"):
        return 2, category, 0, 0, 0, name
    head, source, format_name = split_artifact_filename(filename)
    if format_name == "parquet":
        format_name = "geoparquet"
    if format_name not in ASSET_TITLES:
        return 3, 0, 0, 0, 0, name
    if (layer_parquet := _layer_parquet_slug(resource)) is not None:
        source = layer_parquet[1]
    geometry = head.rpartition("_")[2]
    return (
        1,
        category,
        list(ASSET_TITLES).index(format_name),
        SOURCE_RANK.get(source, len(SOURCE_RANK)),
        GEOMETRY_LABELS.index(geometry) if geometry in GEOMETRY_LABELS else len(GEOMETRY_LABELS),
        name,
    )


def _category_rank(resource, category_labels: Sequence[str]) -> int:  # noqa: ANN001 - hdx Resource
    """Position of the category a resource belongs to, read from the label that starts the
    name `layer_resource_text` gives it."""
    name = resource.get("name") or ""
    label = name.split(" (", 1)[0].split(",", 1)[0]
    return category_labels.index(label) if label in category_labels else len(category_labels)


def _resource_filename(resource) -> str:  # noqa: ANN001 - hdx Resource
    """The uploaded filename behind a resource, whatever its display name is; a file still
    waiting to upload has no URL yet, so its own name stands in."""
    url = resource.get("url") or ""
    if not url and (upload := _pending_upload(resource)):
        return Path(upload).name
    return urllib.parse.unquote(url.rsplit("/", 1)[-1].split("?")[0]) or resource["name"]


def _page_layers(
    cfg: RootConfig,
    by_slug: "dict[str, list[PublishedLayer]]",
    tileset: tuple[str, str] | None,
) -> "list[PageLayer]":
    """One page layer per category and source, coloured in the order they are listed."""
    from oex.report.page import PageLayer

    palette = cfg.output.report.palette or DEFAULT_PALETTE
    layers: list[PageLayer] = []
    for category in cfg.categories:
        slug = slugify(category.name)
        published = by_slug.get(slug)
        if not published:
            continue
        published.sort(key=lambda layer: SOURCE_RANK.get(layer.source.source_name, 99))
        for entry in published:
            source_name = entry.source.source_name
            key = f"{slug}-{source_name}"
            if any(layer.key == key for layer in layers):
                raise RuntimeError(
                    f"{category.name} from {source_name} is published more than once; "
                    "a dataset holds one STAC Collection per category and source"
                )
            layers.append(
                PageLayer(
                    key=key,
                    label=layer_label(category, source_name),
                    color=palette[len(layers) % len(palette)],
                    source=entry.source,
                    downloads=entry.downloads,
                    metadata_href=entry.metadata_href,
                    tileset=tileset,
                    match_key=f"{slug}|{source_name}" if tileset else None,
                )
            )
    return layers


@dataclass(frozen=True)
class PublishedLayer:
    """A layer already on a dataset: what the page shows and what it offers."""

    name: str
    source: "SourceMetadata"
    downloads: "list[Download]"
    metadata_href: str | None = None


def _load_source_metadata(  # noqa: ANN001 - hdx Resource
    resources: list, hdx_dataset_name: str
) -> "list[PublishedLayer]":
    """Every layer on the dataset, earlier runs included, read through its `*_metadata.json`
    Collections; matched on filename because a resource's name is only a title."""
    from oex.report import SourceMetadata
    from oex.report.page import downloads_from_assets

    loaded: list[PublishedLayer] = []
    for resource in resources:
        name = _resource_filename(resource)
        if not name.endswith("_metadata.json"):
            continue
        try:
            collection = _download_json(resource["url"])
            for href in _item_hrefs(collection, resource["url"]):
                item = _download_json(href)
                loaded.append(
                    PublishedLayer(
                        name=_entry_name(collection, item),
                        source=SourceMetadata.from_payload(item),
                        downloads=downloads_from_assets(item.get("assets", {})),
                        metadata_href=href,
                    )
                )
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"HDX dataset {hdx_dataset_name}: could not parse {name}: {exc}"
            ) from exc
    return loaded


def _item_hrefs(collection: dict, collection_url: str) -> list[str]:
    """Where each layer of a Collection lives, resolved against the Collection itself."""
    return [
        urllib.parse.urljoin(collection_url, link["href"])
        for link in collection["links"]
        if link["rel"] == "item"
    ]


def _entry_name(collection: dict, item: dict) -> str:
    """`<dataset>_<category>_<source>_metadata.json`, which the slug matcher resolves."""
    properties = item["properties"]
    return (
        f"{collection['id']}_{properties['oex:category']}_{properties['oex:source']}_metadata.json"
    )


def _slug_from_metadata_name(
    name: str, prefix: str, categories: list[CategoryConfig]
) -> str | None:
    """Match a `{key}_{iso3}_{slug}_{source}_metadata.json` resource to a category slug."""
    if not name.startswith(prefix):
        return None
    for category in categories:
        slug = slugify(category.name)
        if name.startswith(f"{prefix}{slug}_"):
            return slug
    return None


def _preflight_check(api_key: str, owner_org: str, maintainer: str, site: str) -> None:
    """Fail fast when the token cannot edit owner_org or the maintainer does not exist."""
    base = hdx_site_url(site)
    headers = {"Authorization": api_key, "User-Agent": "oex-preflight"}

    def post(action: str, payload: dict) -> dict:
        url = f"{base}/api/3/action/{action}"
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException as exc:
            raise ValueError(f"HDX preflight: network error reaching {url}: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise ValueError(
                f"HDX preflight: non-JSON response from {url} (status {response.status_code}): "
                f"{response.text[:200]}"
            ) from exc
        return body

    organisations_body = post("organization_list_for_user", {"permission": "update_dataset"})
    if not organisations_body.get("success"):
        error_detail = organisations_body.get("error", {})
        raise ValueError(
            f"HDX preflight failed at {base}: API key did not authenticate. "
            f"CKAN says: {error_detail}. Check that HDX_API_KEY is set in this shell "
            f"(echo $HDX_API_KEY) and is valid at {base}/user/<you>/api-tokens."
        )
    organisations = organisations_body.get("result") or []
    organisation_names = {organisation.get("name") for organisation in organisations}
    organisation_ids = {organisation.get("id") for organisation in organisations}
    if owner_org not in organisation_names and owner_org not in organisation_ids:
        editable = sorted(name for name in organisation_names if name)
        raise ValueError(
            f"HDX preflight failed at {base}: token user has no edit rights on "
            f"{owner_org!r}. Orgs with update_dataset permission for this token: "
            f"{editable or '(none)'}. Ask an org admin to add you as Editor at "
            f"{base}/organization/{owner_org}/about, or use a token from a member "
            f"account."
        )

    user_body = post("user_show", {"id": maintainer})
    if not user_body.get("success"):
        error_detail = user_body.get("error", {})
        raise ValueError(
            f"HDX preflight failed at {base}: maintainer {maintainer!r} not found. "
            f"CKAN says: {error_detail}. Set HDX_MAINTAINER (or hdx.maintainer in YAML) to "
            f"your HDX profile slug from {base}/dashboard."
        )

    fullname = user_body.get("result", {}).get("fullname") or maintainer
    logger.info(
        "HDX preflight ok: token can edit %s as %s (%s)",
        owner_org,
        fullname,
        maintainer,
    )


def _apply_dataset_text(dataset, cfg: RootConfig) -> None:  # noqa: ANN001 - hdx Dataset
    """The combined dataset's title, notes, tags and frequency as the config states them."""
    place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
    hdx_source = cfg.hdx.combined.source or long_label("osm")
    if cfg.hdx.combined.title:
        dataset["title"] = combined_title(cfg, place, hdx_source)
    for field, value in (
        ("notes", cfg.hdx.combined.notes),
        ("caveats", cfg.hdx.combined.caveats),
        ("dataset_source", cfg.hdx.combined.source),
        ("methodology_other", cfg.hdx.methodology_other),
    ):
        if value:
            dataset[field] = value
    if cfg.hdx.combined.tags:
        dataset.add_tags(list(cfg.hdx.combined.tags))
    dataset.set_expected_update_frequency(cfg.frequency)


def _sync_resource_text(  # noqa: ANN001 - hdx Dataset
    dataset,
    hdx_dataset_name: str,
    by_slug: dict[str, CategoryConfig],
    *,
    dry_run: bool,
    prune: bool,
) -> tuple[int, int]:
    """Rename and redescribe each resource from the config, pruning dropped layers when asked;
    returns how many changed and how many were pruned."""
    changed = 0
    pruned = 0
    for resource in list(dataset.get_resources() or []):
        layer = _resource_slug(resource, hdx_dataset_name, by_slug)
        if prune and layer is not None and _is_dropped(layer, by_slug):
            logger.info(
                "%s: removing %s, no longer in the config", hdx_dataset_name, resource["name"]
            )
            pruned += 1
            if not dry_run:
                resource.delete_from_hdx()
            continue
        wanted = _resource_text(resource, hdx_dataset_name, by_slug)
        if wanted is None:
            continue
        name, description = wanted
        if resource["name"] == name and resource.get("description") == description:
            continue
        changed += 1
        if dry_run:
            continue
        resource["name"] = name
        resource["description"] = description
        resource.update_in_hdx()
    return changed, pruned
