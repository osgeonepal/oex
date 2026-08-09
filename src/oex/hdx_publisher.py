"""HDX dataset and resource publication. Imports hdx-python-api lazily."""

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from oex.config.schema import CategoryConfig, HdxConfig, RootConfig, S3Config
from oex.logging_setup import get_logger
from oex.s3 import artifact_key
from oex.s3 import resolve as s3_resolve
from oex.s3 import upload as s3_upload

if TYPE_CHECKING:
    from oex.report import SourceMetadata

logger = get_logger(__name__)

_HDX_PUBLISH_BACKOFF_SECONDS = (5, 15, 45, 135)
_HDX_TRANSIENT_HTTP_STATUSES = {429, 502, 503, 504}

_HDX_SITE_URLS = {
    "prod": "https://data.humdata.org",
    "demo": "https://demo.data-humdata-org.ahconu.org",
    "stage": "https://stage.data-humdata-org.ahconu.org",
}

HDX_SHORT_SOURCE = {"osm": "OpenStreetMap", "overture": "Overture"}

# Kept lowercase mid-phrase so "Points of Interest" reads naturally.
_TITLE_LOWER_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to"}


def _title_case_category(name: str) -> str:
    words = name.replace("_", " ").split()
    return " ".join(
        w.lower() if i > 0 and w.lower() in _TITLE_LOWER_WORDS else w.capitalize()
        for i, w in enumerate(words)
    )


def country_name(iso3: str, *, dataset_name: str | None = None) -> str:
    # Precedence: configured dataset_name (overrides ISO inversions like DRC
    # and supports sub-national exports), pycountry common_name, pycountry
    # name, raw iso3.
    if dataset_name:
        return dataset_name
    import pycountry

    record = pycountry.countries.get(alpha_3=iso3.upper())
    if record is None:
        return iso3.upper()
    return getattr(record, "common_name", None) or record.name


# Display order when several sources land on one dataset.
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


def category_label(category: CategoryConfig) -> str:
    return category.hdx.title or _title_case_category(category.name)


def _resolve_title(cfg: RootConfig, category: CategoryConfig) -> str:
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
    dataset_source: str
    snapshot_date: datetime
    source_name: str = ""
    metadata_json_path: Path | None = None
    combined_report_enabled: bool = False
    output_dir: Path | None = None
    s3: S3Config | None = None
    # When both set, override snapshot_date for HDX dataset time period.
    temporal_min: datetime | None = None
    temporal_max: datetime | None = None
    # Export boundary as (min_lon, min_lat, max_lon, max_lat), used to frame the map.
    boundary_bbox: tuple[float, float, float, float] | None = None


@dataclass
class ExtraResource:
    path: Path
    fmt: str
    description: str


@dataclass
class CombinedCategory:
    category: CategoryConfig
    zip_paths: list[Path]
    metadata_json_path: Path | None = None


class HdxPublisher:
    def __init__(self, hdx_cfg: HdxConfig):
        if not hdx_cfg.push:
            raise ValueError("HdxPublisher constructed but hdx.push is false")
        api_key = hdx_cfg.api_key or os.environ.get("HDX_API_KEY")
        owner_org = hdx_cfg.owner_org or os.environ.get("HDX_OWNER_ORG")
        maintainer = hdx_cfg.maintainer or os.environ.get("HDX_MAINTAINER")
        if not (api_key and owner_org and maintainer):
            raise ValueError(
                "hdx.push is enabled but HDX credentials (api_key, owner_org, "
                "maintainer) are missing. Set them in the YAML or environment."
            )

        _preflight_check(api_key, owner_org, maintainer, hdx_cfg.site)

        from hdx.api.configuration import Configuration

        Configuration.create(
            hdx_site=hdx_cfg.site,
            hdx_key=api_key,
            user_agent=hdx_cfg.user_agent,
        )
        self._owner_org = owner_org
        self._maintainer = maintainer
        logger.info("HDX session ready: site=%s org=%s", hdx_cfg.site, owner_org)

    def publish(
        self,
        cfg: RootConfig,
        category: CategoryConfig,
        zip_paths: list[Path],
        ctx: PublishContext,
        extra_resources: list[ExtraResource] | None = None,
    ) -> str:
        category_slug = _slugify(category.name)
        dt_name = f"{cfg.key}_{cfg.iso3.lower()}_{category_slug}"
        dataset = self._build_dataset_object(cfg, category, dt_name, ctx)

        # Largest resource first so HDX's default-resource preview lands on
        # the richest format instead of a sparse one (e.g., lines over points).
        sorted_zips = sorted(zip_paths, key=lambda p: p.stat().st_size, reverse=True)
        for zip_path in sorted_zips:
            res = self._make_resource_for_zip(zip_path, category, ctx, cfg.iso3, category_slug)
            dataset.add_update_resource(res)

        for extra in extra_resources or []:
            res = self._make_resource_for_path(
                path=extra.path,
                fmt=extra.fmt,
                description=extra.description,
                ctx=ctx,
                iso3=cfg.iso3,
                category_slug=category_slug,
            )
            dataset.add_update_resource(res)

        if ctx.metadata_json_path is not None:
            res = self._make_resource_for_path(
                path=ctx.metadata_json_path,
                fmt="json",
                description=f"{category.name} ({ctx.source_name}) layer metadata",
                ctx=ctx,
                iso3=cfg.iso3,
                category_slug=category_slug,
            )
            dataset.add_update_resource(res)

        # A category's dataset holds every source that publishes it, so an OSM run
        # must not delete the Overture resources already there. Pruning is opt-in.
        self._create_or_update(dataset, dt_name, remove_additional=cfg.hdx.purge_existing_resources)

        if ctx.combined_report_enabled and ctx.output_dir is not None:
            self._build_and_publish_combined_report(
                dt_name=dt_name,
                category=category,
                cfg=cfg,
                category_slug=category_slug,
                output_dir=ctx.output_dir,
                s3_cfg=ctx.s3,
                boundary_bbox=ctx.boundary_bbox,
            )
        return dt_name

    def refresh_metadata(
        self,
        cfg: RootConfig,
        category: CategoryConfig,
        ctx: PublishContext,
    ) -> str | None:
        """Restate an existing dataset's snapshot date when a category exports nothing.

        A category with no features publishes no resources, so without this its dataset
        keeps the date of the last run that did find some and drifts past the update
        frequency it advertises. The source data was still read at ``ctx.snapshot_date``,
        so recording that is accurate. Returns None when nothing is published yet, since
        an empty category is no reason to create a resourceless dataset.
        """
        from hdx.data.dataset import Dataset

        category_slug = _slugify(category.name)
        dt_name = f"{cfg.key}_{cfg.iso3.lower()}_{category_slug}"
        existing = Dataset.read_from_hdx(dt_name)
        if existing is None:
            logger.info("%s empty and not on HDX; nothing to refresh", dt_name)
            return None

        existing_org = existing.get("owner_org")
        if existing_org and existing_org != self._owner_org:
            raise RuntimeError(
                f"HDX dataset {dt_name} exists under a different organisation "
                f"(owner_org={existing_org!r}; you are publishing as {self._owner_org!r})."
            )

        if ctx.temporal_min and ctx.temporal_max:
            existing.set_time_period(ctx.temporal_min, ctx.temporal_max)
        else:
            existing.set_time_period(ctx.snapshot_date)
        _hdx_publish_with_retry(
            lambda: existing.update_in_hdx(
                update_resources=False,
                match_resources_by_metadata=True,
                hxl_update=False,
            ),
            label=f"refresh {dt_name}",
        )
        logger.info("%s empty; refreshed snapshot date to %s", dt_name, ctx.snapshot_date)
        return dt_name

    def _create_or_update(  # noqa: ANN001 - hdx Dataset
        self, dataset, dt_name: str, *, remove_additional: bool
    ) -> None:
        from hdx.data.dataset import Dataset

        existing = Dataset.read_from_hdx(dt_name)
        if existing is not None:
            existing_org = existing.get("owner_org")
            if existing_org and existing_org != self._owner_org:
                raise RuntimeError(
                    f"HDX dataset {dt_name} exists under a different organisation "
                    f"(owner_org={existing_org!r}; you are publishing as "
                    f"{self._owner_org!r}). Pick a different `key` in your config "
                    "to namespace your datasets, or have HDX transfer ownership."
                )
            dataset["id"] = existing["id"]
            logger.info(
                "Updating HDX dataset %s with %d resources",
                dt_name,
                len(dataset.get_resources()),
            )
            _hdx_publish_with_retry(
                lambda: dataset.update_in_hdx(
                    remove_additional_resources=remove_additional,
                    match_resources_by_metadata=True,
                    hxl_update=False,
                ),
                label=f"update {dt_name}",
            )
        else:
            logger.info(
                "Creating HDX dataset %s with %d resources",
                dt_name,
                len(dataset.get_resources()),
            )
            _hdx_publish_with_retry(
                lambda: dataset.create_in_hdx(
                    allow_no_resources=False,
                    hxl_update=False,
                ),
                label=f"create {dt_name}",
            )

    def publish_combined(
        self,
        cfg: RootConfig,
        entries: list[CombinedCategory],
        ctx: PublishContext,
        *,
        pmtiles_path: Path | None = None,
        metadata_path: Path | None = None,
        landing_enabled: bool = False,
    ) -> str:
        if not entries:
            raise ValueError("publish_combined called with no categories")
        dt_name = cfg.hdx.combined.name or f"{cfg.key}_{cfg.iso3.lower()}"
        dataset = self._build_combined_dataset_object(cfg, entries, dt_name, ctx)

        pmtiles_layer: str | None = None
        if pmtiles_path is not None:
            pmtiles_layer = pmtiles_path.stem
            res = self._make_resource_for_path(
                path=pmtiles_path,
                fmt="pmtiles",
                description=f"Combined vector tiles ({len(entries)} layers, coloured by category)",
                ctx=ctx,
                iso3=cfg.iso3,
                category_slug="combined",
            )
            dataset.add_update_resource(res)

        for entry in entries:
            slug = _slugify(entry.category.name)
            sorted_zips = sorted(entry.zip_paths, key=lambda p: p.stat().st_size, reverse=True)
            for zip_path in sorted_zips:
                res = self._make_resource_for_zip(zip_path, entry.category, ctx, cfg.iso3, slug)
                dataset.add_update_resource(res)

        # One dataset-level metadata resource covering every layer.
        if metadata_path is not None:
            res = self._make_resource_for_path(
                path=metadata_path,
                fmt="json",
                description=f"Layer metadata for all {len(entries)} layers",
                ctx=ctx,
                iso3=cfg.iso3,
                category_slug="combined",
            )
            dataset.add_update_resource(res)

        # A combined dataset accumulates across sources: an Overture run must not
        # delete the resources an earlier OSM run put here. Pruning is opt-in.
        self._create_or_update(dataset, dt_name, remove_additional=cfg.hdx.purge_existing_resources)

        if landing_enabled and ctx.output_dir is not None:
            self._build_and_publish_landing(
                dt_name=dt_name,
                cfg=cfg,
                entries=entries,
                pmtiles_layer=pmtiles_layer,
                ctx=ctx,
            )
        return dt_name

    def _build_combined_dataset_object(
        self,
        cfg: RootConfig,
        entries: list[CombinedCategory],
        dt_name: str,
        ctx: PublishContext,
    ):  # noqa: ANN202 - hdx-python-api Dataset
        from hdx.data.dataset import Dataset

        place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
        hdx_source = HDX_SHORT_SOURCE.get(ctx.source_name, ctx.dataset_source)
        title = combined_title(cfg, place, hdx_source)

        labels = [category_label(e.category) for e in entries]
        notes = cfg.hdx.combined.notes or (
            f"Combined {hdx_source} vector export for {place}. "
            f"Layers: {', '.join(labels)}. Each layer is provided as GIS downloads; "
            "the interactive overview maps all layers together."
        )
        dataset_args: dict[str, object] = {
            "title": title,
            "name": dt_name,
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
        licenses = {e.category.hdx.license for e in entries}
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
        if ctx.temporal_min is not None and ctx.temporal_max is not None:
            dataset.set_time_period(ctx.temporal_min, ctx.temporal_max)
        else:
            dataset.set_time_period(ctx.snapshot_date)
        dataset.set_expected_update_frequency(cfg.frequency)
        dataset.add_other_location(cfg.iso3.upper())
        seen_tags: set[str] = set()
        category_tags = (tag for entry in entries for tag in entry.category.hdx.tags)
        for tag in (*cfg.hdx.combined.tags, *category_tags):
            if tag not in seen_tags:
                dataset.add_tag(tag)
                seen_tags.add(tag)
        return dataset

    def _read_dataset(self, dt_name: str, purpose: str):  # noqa: ANN202 - hdx Dataset
        from hdx.data.dataset import Dataset

        dataset = Dataset.read_from_hdx(dt_name)
        if dataset is None:
            raise RuntimeError(f"HDX dataset {dt_name} not visible before {purpose} build")
        return dataset

    def _publish_html_viz(
        self,
        *,
        dataset,  # noqa: ANN001 - hdx Dataset
        dt_name: str,
        html_path: Path,
        description: str,
        iso3: str,
        category_slug: str,
        s3_cfg: S3Config | None,
    ) -> None:
        """Attach an HTML page to the dataset and make it the custom visualisation."""
        viz_ctx = PublishContext(
            dataset_source="",
            snapshot_date=datetime.now(),
            source_name=category_slug,
            s3=s3_cfg,
        )
        resource = self._make_resource_for_path(
            path=html_path,
            fmt="html",
            description=description,
            ctx=viz_ctx,
            iso3=iso3,
            category_slug=category_slug,
        )
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
            label=f"{category_slug} update {dt_name}",
        )
        # An uploaded resource only gets its URL from HDX after the update above.
        if url is None:
            self._set_customviz_after_upload(dt_name, html_path.name)

    def _build_and_publish_landing(
        self,
        *,
        dt_name: str,
        cfg: RootConfig,
        entries: list[CombinedCategory],
        pmtiles_layer: str | None,
        ctx: PublishContext,
    ) -> None:
        from oex.report.landing import CategoryPanel, render_landing, source_label

        dataset = self._read_dataset(dt_name, "landing")
        resources = dataset.get_resources() or []
        pmtiles_url = (
            _resource_url(resources, f"{pmtiles_layer}.pmtiles") if pmtiles_layer else None
        )

        prefix = f"{cfg.key}_{cfg.iso3.lower()}_"
        by_slug: dict[str, list[SourceMetadata]] = {}
        for name, source_metadata in _load_source_metadata(resources, dt_name):
            # Every category on the dataset, not just this run's: a later run of the
            # other source must describe the layers the tileset already carries.
            slug = _slug_from_metadata_name(name, prefix, cfg.categories)
            if slug is not None:
                by_slug.setdefault(slug, []).append(source_metadata)

        panels: list[CategoryPanel] = []
        for category in cfg.categories:
            slug = _slugify(category.name)
            sources = by_slug.get(slug)
            if not sources:
                continue
            sources.sort(key=lambda s: SOURCE_RANK.get(s.source_name, 99))
            panels.append(CategoryPanel(slug=slug, label=category_label(category), sources=sources))
        if not panels:
            raise RuntimeError(
                f"HDX dataset {dt_name}: landing requested but no metadata.json resources found"
            )

        place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
        hdx_source = HDX_SHORT_SOURCE.get(ctx.source_name, ctx.dataset_source)
        layer_count = sum(len(p.sources) for p in panels)
        present = sorted(
            {s.source_name for p in panels for s in p.sources},
            key=lambda n: SOURCE_RANK.get(n, 99),
        )
        sources_label = " and ".join(source_label(n) for n in present)
        html = render_landing(
            title=combined_title(cfg, place, hdx_source),
            subtitle=f"{layer_count} layers from {sources_label} for {place}",
            panels=panels,
            pmtiles_url=pmtiles_url,
            pmtiles_layer=pmtiles_layer,
            boundary_bbox=ctx.boundary_bbox,
            palette=cfg.output.report.palette,
            map_assets=cfg.output.report.map_assets,
        )
        assert ctx.output_dir is not None
        landing_path = ctx.output_dir / f"{dt_name}_overview.html"
        landing_path.write_text(html, encoding="utf-8")
        logger.info("Built combined landing (%d layers) -> %s", layer_count, landing_path)

        self._publish_html_viz(
            dataset=dataset,
            dt_name=dt_name,
            html_path=landing_path,
            description=f"{place} interactive overview",
            iso3=cfg.iso3,
            category_slug="overview",
            s3_cfg=ctx.s3,
        )

    def _set_customviz_after_upload(self, dt_name: str, resource_name: str) -> None:
        from hdx.data.dataset import Dataset

        dataset = Dataset.read_from_hdx(dt_name)
        if dataset is None:
            return
        resource = next(
            (r for r in (dataset.get_resources() or []) if r["name"] == resource_name), None
        )
        url = resource.get("url") if resource else None
        if not url:
            logger.warning(
                "customviz: overview resource %s has no URL yet on %s", resource_name, dt_name
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
            label=f"customviz {dt_name}",
        )

    def _build_dataset_object(
        self,
        cfg: RootConfig,
        category: CategoryConfig,
        dt_name: str,
        ctx: PublishContext,
    ):  # noqa: ANN202 - hdx-python-api Dataset, imported lazily
        from hdx.data.dataset import Dataset

        title = _resolve_title(cfg, category)
        hdx_source = category.hdx.dataset_source or HDX_SHORT_SOURCE.get(
            ctx.source_name, ctx.dataset_source
        )
        dataset_args: dict[str, object] = {
            "title": title,
            "name": dt_name,
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
        if ctx.temporal_min is not None and ctx.temporal_max is not None:
            dataset.set_time_period(ctx.temporal_min, ctx.temporal_max)
        else:
            dataset.set_time_period(ctx.snapshot_date)
        dataset.set_expected_update_frequency(cfg.frequency)
        dataset.add_other_location(cfg.iso3.upper())
        for tag in category.hdx.tags:
            dataset.add_tag(tag)
        return dataset

    def _make_resource_for_zip(
        self,
        zip_path: Path,
        category: CategoryConfig,
        ctx: PublishContext,
        iso3: str,
        category_slug: str,
    ):  # noqa: ANN202 - hdx-python-api Resource
        parts = zip_path.stem.rsplit("_", 2)
        source = parts[-2] if len(parts) >= 3 else ""
        fmt = parts[-1]
        description = (
            f"{category.name} ({source}) data in {fmt.upper()} format"
            if source
            else f"{category.name} data in {fmt.upper()} format"
        )
        return self._make_resource_for_path(
            path=zip_path,
            fmt=fmt,
            description=description,
            ctx=ctx,
            iso3=iso3,
            category_slug=category_slug,
        )

    def _make_resource_for_path(
        self,
        *,
        path: Path,
        fmt: str,
        description: str,
        ctx: PublishContext,
        iso3: str,
        category_slug: str,
    ):  # noqa: ANN202 - hdx-python-api Resource
        from hdx.data.resource import Resource

        size_bytes = path.stat().st_size
        resource_data: dict[str, object] = {
            "name": path.name,
            "description": description,
            "format": fmt,
            "size": int(size_bytes),
        }
        if ctx.s3 is not None and ctx.s3.enabled:
            bucket, prefix, region, endpoint_url, acl = s3_resolve(ctx.s3)
            if not bucket:
                raise ValueError(
                    "output.s3.enabled is true but no bucket given via "
                    "output.s3.bucket or OEX_S3_BUCKET"
                )
            key = artifact_key(
                prefix,
                iso3,
                category_slug,
                path.name,
                folder=ctx.s3.folder,
                nest_by_category=ctx.s3.nest_by_category,
            )
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
            res = Resource(resource_data)
        else:
            res = Resource(resource_data)
            res.set_file_to_upload(str(path))
        res.mark_data_updated()
        return res

    def _build_and_publish_combined_report(
        self,
        *,
        dt_name: str,
        category: CategoryConfig,
        cfg: RootConfig,
        category_slug: str,
        output_dir: Path,
        s3_cfg: S3Config | None,
        boundary_bbox: tuple[float, float, float, float] | None = None,
    ) -> None:
        # HDX serves uploaded HTML with text/html + SAMEORIGIN, so the resource URL self-iframes.
        from oex.report import render_report

        dataset = self._read_dataset(dt_name, "report")
        resources = dataset.get_resources() or []

        sources = {sm.source_name: sm for _, sm in _load_source_metadata(resources, dt_name)}
        if not sources:
            raise RuntimeError(
                f"HDX dataset {dt_name}: combined report requested but no "
                "metadata.json resources found"
            )

        report_path = output_dir / f"{cfg.key}_{cfg.iso3.lower()}_{category_slug}_report.html"
        report_path.write_text(
            render_report(
                sources,
                _category_tilesets(resources),
                boundary_bbox,
                category_label(category),
                cfg.output.report.palette,
                cfg.output.report.map_assets,
            ),
            encoding="utf-8",
        )
        logger.info(
            "Built combined report (%d source%s) -> %s",
            len(sources),
            "" if len(sources) == 1 else "s",
            report_path,
        )

        self._publish_html_viz(
            dataset=dataset,
            dt_name=dt_name,
            html_path=report_path,
            description=f"{category.name} (interactive report)",
            iso3=cfg.iso3,
            category_slug=category_slug,
            s3_cfg=s3_cfg,
        )


def _hdx_publish_with_retry(call, label: str):  # noqa: ANN001 - lambda
    """Run an HDX publish call with backoff on 429/5xx/connection-style errors."""
    from hdx.data.hdxobject import HDXError

    last_exc: Exception | None = None
    for attempt, sleep_s in enumerate(_HDX_PUBLISH_BACKOFF_SECONDS, start=1):
        try:
            return call()
        except HDXError as exc:
            if not _is_transient_hdx_error(exc):
                raise
            last_exc = exc
            if attempt < len(_HDX_PUBLISH_BACKOFF_SECONDS):
                logger.warning(
                    "HDX %s attempt %d/%d hit transient error (%s); retrying in %ds",
                    label,
                    attempt,
                    len(_HDX_PUBLISH_BACKOFF_SECONDS),
                    _summarise_hdx_error(exc),
                    sleep_s,
                )
                time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


def _is_transient_hdx_error(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, requests.exceptions.RetryError):
            return True
        if isinstance(cur, requests.exceptions.ConnectionError):
            return True
        if isinstance(cur, requests.exceptions.Timeout):
            return True
        if isinstance(cur, requests.exceptions.HTTPError):
            response = getattr(cur, "response", None)
            if response is not None and response.status_code in _HDX_TRANSIENT_HTTP_STATUSES:
                return True
        cur = cur.__cause__
    return False


def _summarise_hdx_error(exc: BaseException) -> str:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, requests.exceptions.RequestException):
            return f"{type(cur).__name__}: {cur}"
        cur = cur.__cause__
    return f"{type(exc).__name__}: {exc}"


def _download_json(url: str, *, timeout: float = 60.0) -> dict:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _slugify(value: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9]+", "_", value).lower().strip("_")


def _resource_url(resources: list, name: str) -> str | None:  # noqa: ANN001 - hdx Resource
    resource = next((r for r in resources if r["name"] == name), None)
    return resource.get("url") if resource else None


def _category_tilesets(resources: list) -> dict[str, tuple[str, str]]:  # noqa: ANN001 - hdx Resource
    """Map each source to its tileset on a per-category dataset.

    One tileset per source, named `{key}_{iso3}_{slug}_{source}.pmtiles`, whose
    single layer is the file stem.
    """
    tilesets: dict[str, tuple[str, str]] = {}
    for resource in resources:
        name = resource["name"]
        if not name.endswith(".pmtiles"):
            continue
        stem = name[: -len(".pmtiles")]
        url = resource.get("url")
        if url:
            tilesets[stem.rsplit("_", 1)[-1]] = (url, stem)
    return tilesets


def _load_source_metadata(  # noqa: ANN001 - hdx Resource
    resources: list, dt_name: str
) -> "list[tuple[str, SourceMetadata]]":
    """Read every `*_metadata.json` resource on a dataset, newest layer set included.

    Both report pages describe whatever layers the dataset carries, so both read
    the metadata resources rather than only the ones this run published.
    """
    from oex.report import SourceMetadata

    loaded: list[tuple[str, SourceMetadata]] = []
    for resource in resources:
        name = resource["name"]
        if not name.endswith("_metadata.json"):
            continue
        try:
            payload = _download_json(resource["url"])
            loaded.append((name, SourceMetadata.from_payload(payload)))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"HDX dataset {dt_name}: could not parse {name}: {exc}") from exc
    return loaded


def _slug_from_metadata_name(
    name: str, prefix: str, categories: list[CategoryConfig]
) -> str | None:
    """Match a `{key}_{iso3}_{slug}_{source}_metadata.json` resource to a category slug."""
    if not name.startswith(prefix):
        return None
    for category in categories:
        slug = _slugify(category.name)
        if name.startswith(f"{prefix}{slug}_"):
            return slug
    return None


def _preflight_check(api_key: str, owner_org: str, maintainer: str, site: str) -> None:
    """Fail fast with a precise message if HDX credentials cannot publish to owner_org.
    This runs three cheap CKAN action calls before any export work begins.
    """
    base = _HDX_SITE_URLS.get(site)
    if base is None:
        raise ValueError(f"Unknown hdx.site={site!r}. Expected one of {sorted(_HDX_SITE_URLS)}.")
    headers = {"Authorization": api_key, "User-Agent": "oex-preflight"}

    def _post(action: str, payload: dict) -> dict:
        url = f"{base}/api/3/action/{action}"
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException as exc:
            raise ValueError(f"HDX preflight: network error reaching {url}: {exc}") from exc
        try:
            body = r.json()
        except ValueError as exc:
            raise ValueError(
                f"HDX preflight: non-JSON response from {url} (status {r.status_code}): "
                f"{r.text[:200]}"
            ) from exc
        return body

    orgs_body = _post("organization_list_for_user", {"permission": "update_dataset"})
    if not orgs_body.get("success"):
        err = orgs_body.get("error", {})
        raise ValueError(
            f"HDX preflight failed at {base}: API key did not authenticate. "
            f"CKAN says: {err}. Check that HDX_API_KEY is set in this shell "
            f"(echo $HDX_API_KEY) and is valid at {base}/user/<you>/api-tokens."
        )
    orgs = orgs_body.get("result") or []
    org_names = {o.get("name") for o in orgs}
    org_ids = {o.get("id") for o in orgs}
    if owner_org not in org_names and owner_org not in org_ids:
        editable = sorted(n for n in org_names if n)
        raise ValueError(
            f"HDX preflight failed at {base}: token user has no edit rights on "
            f"{owner_org!r}. Orgs with update_dataset permission for this token: "
            f"{editable or '(none)'}. Ask an org admin to add you as Editor at "
            f"{base}/organization/{owner_org}/about, or use a token from a member "
            f"account."
        )

    user_body = _post("user_show", {"id": maintainer})
    if not user_body.get("success"):
        err = user_body.get("error", {})
        raise ValueError(
            f"HDX preflight failed at {base}: maintainer {maintainer!r} not found. "
            f"CKAN says: {err}. Set HDX_MAINTAINER (or hdx.maintainer in YAML) to "
            f"your HDX profile slug from {base}/dashboard."
        )

    fullname = user_body.get("result", {}).get("fullname") or maintainer
    logger.info(
        "HDX preflight ok: token can edit %s as %s (%s)",
        owner_org,
        fullname,
        maintainer,
    )
