"""Per-category export loop, shared by Overture and OSM sources."""

import concurrent.futures
import hashlib
import itertools
import json
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import duckdb
import pystac
from hdx.data.hdxobject import HDXError

from oex.boundary import Boundary, resolve_boundary
from oex.catalog import S3CatalogStore, link_collections
from oex.catalog import layout as catalog_layout
from oex.config.schema import (
    CategoryConfig,
    PcodesSourceConfig,
    RootConfig,
    dataset_identity,
    license_label,
)
from oex.duckdb_session import connect
from oex.hdx_publisher import (
    SOURCE_RANK,
    CombinedCategory,
    ExtraResource,
    HdxPublisher,
    PublishContext,
    combined_title,
    country_name,
    dataset_title,
    hdx_dataset_url,
    links_related_datasets,
    published_collections,
    related_dataset_links,
    turned_off_sources,
)
from oex.logging_setup import get_logger
from oex.metadata import MetadataReport, TemporalReport, compute_metadata, temporal_bounds
from oex.naming import (
    COMBINED_SLUG,
    category_dataset_name,
    category_label,
    combined_dataset_name,
    layer_dataset_name,
    layer_label,
    resource_prefix,
    slugify,
)
from oex.palette import DEFAULT_PALETTE
from oex.pcodes import (
    PcodeCacheEntry,
    ensure_admin_parquets,
    resolve_pcodes_config,
    tag_table,
    write_country_admin_parquets,
)
from oex.pcodes.tagger import parse_boundary_resolution
from oex.preflight import check_tile_builder, check_writable_paths
from oex.report import SourceMetadata
from oex.report.page import PageLayer, downloads_from_assets, render_dataset_page
from oex.s3 import build_layer_key, category_artifact_key, list_layer_urls, resolve_with_bucket
from oex.s3 import resolve as s3_resolve
from oex.s3 import upload as s3_upload
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner
from oex.sources.labels import long_label
from oex.sql import build_select_clause, build_where_clause, materialise
from oex.stac import (
    LayerAssets,
    asset_href,
    asset_key,
    build_collection,
    build_item,
    collection_filename,
    href_or_filename,
    item_filename,
    item_href,
    license_href,
    partition_of,
    to_payload,
    write_json,
)
from oex.state import StateStore
from oex.system import adaptive_parallel_resources, cpu_count
from oex.timestamps import parse_utc
from oex.translit import transliterate_table
from oex.writers import (
    TileLayer,
    build_combined_pmtiles,
    geometry_labels,
    geometry_type_filter,
    write_format,
    write_geoparquet,
    write_pmtiles,
)
from oex.zip_bundle import make_zip

logger = get_logger(__name__)

_PROJECT_URL = "https://github.com/osgeonepal/oex"
# The state key for the single combined dataset's upload, distinct from the S3 segment
# naming.COMBINED_SLUG; state files already written use this value.
COMBINED_STATE_KEY = "__combined__"
_FORMAT_LABELS = {
    "gpkg": "GeoPackage (gpkg)",
    "shp": "ESRI Shapefile (shp)",
    "geojson": "GeoJSON (geojson)",
    "kml": "Keyhole Markup Language (kml)",
}


def _oex_version() -> str:
    try:
        return version("oex")
    except PackageNotFoundError:
        return "0.2.0+source"


@dataclass
class CategoryResult:
    name: str
    status: str
    feature_count: int = 0
    duration_s: float = 0.0
    zip_paths: list[Path] = field(default_factory=list)
    hdx_dataset: str | None = None
    error: str | None = None


@dataclass
class BuiltCategory:
    """A processed category plus the artifacts combined publishing needs."""

    result: CategoryResult
    category: CategoryConfig
    zip_paths: list[Path] = field(default_factory=list)
    metadata_json_path: Path | None = None
    item_path: Path | None = None
    metadata_report: MetadataReport | None = None
    source_metadata: SourceMetadata | None = None
    geoparquet_path: Path | None = None
    publish_geoparquet: bool = False
    geoparquet_url: str | None = None
    query: SourceQuery | None = None


@dataclass
class ExportResult:
    iso3: str
    source_name: str
    categories: dict[str, CategoryResult] = field(default_factory=dict)
    total_duration_s: float = 0.0

    @property
    def succeeded(self) -> int:
        return sum(1 for result in self.categories.values() if result.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for result in self.categories.values() if result.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.categories.values() if result.status == "skipped")

    @property
    def empty(self) -> int:
        return sum(1 for result in self.categories.values() if result.status == "empty")

    @property
    def unchanged(self) -> int:
        return sum(1 for result in self.categories.values() if result.status == "unchanged")


@dataclass
class _CategoryJob:
    """One category's export in progress: what every export step reads."""

    category: CategoryConfig
    query: SourceQuery
    boundary: Boundary
    out_root: Path
    publisher: HdxPublisher | None
    conn: duckdb.DuckDBPyConnection
    table: str
    started: float
    log_prefix: str

    @property
    def slug(self) -> str:
        return slugify(self.category.name)

    def result(self, status: str, **fields: Any) -> CategoryResult:
        return CategoryResult(
            name=self.category.name,
            status=status,
            duration_s=time.time() - self.started,
            **fields,
        )


@dataclass
class _LayerOutputs:
    """The files one category's export wrote, which the metadata and publish steps hand on."""

    zip_paths: list[Path]
    publish_geoparquet: bool
    export_seconds: float
    geoparquet_path: Path | None = None
    geoparquet_url: str | None = None
    pmtiles_path: Path | None = None
    metadata_json_path: Path | None = None
    item_path: Path | None = None
    layer: LayerAssets | None = None


def _early_result(
    category: CategoryConfig, status: str, started: float, **fields: Any
) -> BuiltCategory:
    """A category that ended before producing artifacts."""
    return BuiltCategory(
        result=CategoryResult(
            name=category.name, status=status, duration_s=time.time() - started, **fields
        ),
        category=category,
    )


class Exporter:
    def __init__(self, cfg: RootConfig, runner: SourceRunner):
        self._cfg = cfg
        self._runner = runner
        self._pcodes_config: PcodesSourceConfig = resolve_pcodes_config(cfg.source)
        self._pcode_cache: dict[int, PcodeCacheEntry] | None = None
        self._state: StateStore | None = None
        # Pcode tagging peaks in memory, so workers take turns rather than peak together.
        self._pcode_tag_semaphore = threading.Semaphore(1)
        self._adaptive_workers, self._adaptive_memory_gb = adaptive_parallel_resources()
        self._staged_collections: list[tuple[str, str]] = []
        self._published: frozenset[tuple[str, str]] = frozenset()

    @property
    def _worker_count(self) -> int:
        return self._cfg.parallel.threads or self._adaptive_workers

    @property
    def _memory_gb(self) -> int:
        return self._cfg.parallel.memory_gb or self._adaptive_memory_gb

    def _open_duckdb(self, path: Path, threads: int) -> duckdb.DuckDBPyConnection:
        duckdb_config = self._cfg.duckdb
        return connect(
            path=path,
            threads=threads,
            memory_gb=self._memory_gb,
            s3_region=getattr(self._cfg.source.get("overture"), "s3_region", "us-west-2"),
            temp_dir=duckdb_config.temp_dir,
            http_retries=duckdb_config.http_retries,
            http_retry_wait_ms=duckdb_config.http_retry_wait_ms,
            http_retry_backoff=duckdb_config.http_retry_backoff,
            http_timeout_s=duckdb_config.http_timeout_s,
            anonymous_s3_bucket=getattr(
                self._cfg.source.get("overture"), "s3_bucket", "overturemaps-us-west-2"
            ),
        )

    def run(self) -> ExportResult:
        self._check_config()
        check_writable_paths(self._cfg)
        check_tile_builder(self._cfg)

        area_id = self._cfg.iso3.upper() or dataset_identity(self._cfg)
        self._log_run_plan(area_id)

        out_root = Path(self._cfg.output.dir) / dataset_identity(self._cfg) / self._runner.name
        out_root.mkdir(parents=True, exist_ok=True)
        self._state = StateStore(
            path=out_root / ".state.json",
            iso3=self._cfg.iso3,
            source=self._runner.name,
        )

        peeked: str | None = None
        if self._cfg.output.resume:
            peeked = self._runner.peek_snapshot_label(self._cfg)
            if peeked and self._already_uploaded(peeked):
                logger.info(
                    "[%s/%s] resume: already uploaded for snapshot %s; "
                    "skipping boundary fetch, pcode cache, and per-category work",
                    area_id,
                    self._runner.name,
                    peeked,
                )
                return ExportResult(iso3=area_id, source_name=self._runner.name)

        boundary = resolve_boundary(self._cfg.iso3, self._cfg.boundary)
        bbox = boundary.bbox
        self._published = published_collections(self._cfg)
        logger.info(
            "[%s/%s] boundary: %s bbox=(%.4f, %.4f, %.4f, %.4f)",
            area_id,
            self._runner.name,
            boundary.source,
            bbox[0],
            bbox[1],
            bbox[2],
            bbox[3],
        )

        self._runner.prepare(self._cfg)
        tags_pcodes = self._prepare_pcodes(area_id, out_root)

        publisher: HdxPublisher | None = None
        if self._cfg.hdx.push:
            publisher = HdxPublisher(self._cfg.hdx)

        # Layer staging uploads on output.s3.enabled alone, independently of hdx.push.
        if self._cfg.output.s3.enabled:
            from oex.s3 import preflight as s3_preflight

            logger.info("[%s/%s] s3: preflight check", area_id, self._runner.name)
            s3_preflight(self._cfg.output.s3)

        result = ExportResult(iso3=area_id, source_name=self._runner.name)
        start = time.time()

        built_list = self._process_all(boundary, out_root, publisher)
        for built in built_list:
            result.categories[built.result.name] = built.result

        if self._cfg.hdx.combine:
            self._finish_combined(
                built_list,
                out_root,
                publisher,
                peeked_label=peeked,
                boundary_bbox=bbox,
                boundary_geojson=boundary.geojson,
            )

        if self._staged_collections and (catalog := catalog_layout(self._cfg)):
            link_collections(
                catalog,
                self._cfg.output.stac,
                S3CatalogStore(self._cfg),
                self._staged_collections,
                area_title=self._area_title(),
            )

        if tags_pcodes:
            shutil.rmtree(out_root / "_pcodes")

        result.total_duration_s = time.time() - start
        logger.info(
            "[%s] %s done in %.1fs: %d ok, %d unchanged, %d empty, %d skipped, %d failed",
            result.iso3,
            self._runner.name,
            result.total_duration_s,
            result.succeeded,
            result.unchanged,
            result.empty,
            result.skipped,
            result.failed,
        )
        return result

    def _check_config(self) -> None:
        """Settings that cannot work together fail the run before any category is built."""
        output = self._cfg.output
        if not dataset_identity(self._cfg):
            raise ValueError("config.iso3 or config.output.s3.folder is required")
        if not self._cfg.categories:
            raise ValueError("config.categories is empty")
        if self._cfg.hdx.push and self._runner.name in turned_off_sources(self._cfg).names:
            raise ValueError(
                f"source.{self._runner.name}.enabled is false: a publish removes the files "
                "of every source the config turns off, so this run would remove its own"
            )
        if self._want_metadata():
            for category in self._cfg.categories:
                license_href(category.hdx.license, category.hdx.license_url)
        if self._cfg.hdx.push and self._want_metadata() and not output.s3.enabled:
            raise ValueError(
                "hdx.push is true and layer metadata is on, but output.s3.enabled is false: "
                "the Collection links each layer's Item by URL and the Items are published "
                "to the bucket, so without S3 every link would be a 404"
            )
        publishes_geoparquet = any(
            "geoparquet" in (category.formats or output.formats)
            for category in self._cfg.categories
        )
        if publishes_geoparquet and self._cfg.hdx.push and not output.s3.enabled:
            raise ValueError(
                "output.formats includes 'geoparquet' and hdx.push is true, but "
                "output.s3.enabled is false: the HDX resource points at the staged "
                "object, so there is nothing to publish"
            )
        if output.s3.enabled and output.s3.nest_by_geometry and not output.split_by_geometry:
            raise ValueError(
                "output.s3.nest_by_geometry needs output.split_by_geometry; without it "
                "one artifact holds every geometry type and cannot sit under one segment"
            )

    def _log_run_plan(self, area_id: str) -> None:
        category_names = [category.name for category in self._cfg.categories]
        logger.info(
            "[%s/%s] run starting: %d categor%s, formats=%s, parallel=%s, hdx_push=%s",
            area_id,
            self._runner.name,
            len(category_names),
            "y" if len(category_names) == 1 else "ies",
            self._cfg.output.formats,
            self._cfg.parallel.enabled,
            self._cfg.hdx.push,
        )
        logger.info("[%s/%s] categories: %s", area_id, self._runner.name, ", ".join(category_names))
        logger.info(
            "[%s/%s] resources: workers=%d (cfg=%s adaptive=%d) memory_gb=%d (cfg=%s adaptive=%d)",
            area_id,
            self._runner.name,
            self._worker_count,
            self._cfg.parallel.threads,
            self._adaptive_workers,
            self._memory_gb,
            self._cfg.parallel.memory_gb,
            self._adaptive_memory_gb,
        )

    def _already_uploaded(self, snapshot_label: str) -> bool:
        assert self._state is not None
        if self._cfg.hdx.combine:
            return self._state.is_uploaded(COMBINED_STATE_KEY, snapshot_label=snapshot_label)
        return all(
            self._state.is_uploaded(slugify(category.name), snapshot_label=snapshot_label)
            for category in self._cfg.categories
        )

    def _prepare_pcodes(self, area_id: str, out_root: Path) -> bool:
        """Download the admin polygons once, so parallel categories share one cache; True when a
        category tags with them and the per-country copies need removing after the run."""
        tags_pcodes = self._pcodes_config.enabled and any(
            not category.skip_pcodes for category in self._cfg.categories
        )
        if not self._pcodes_config.enabled:
            return tags_pcodes
        logger.info(
            "[%s/%s] pcodes: preparing fieldmaps cache (levels=%s, dir=%s)",
            area_id,
            self._runner.name,
            self._pcodes_config.levels,
            self._pcodes_config.cache_dir,
        )
        self._pcode_cache = ensure_admin_parquets(
            cache_dir=Path(self._pcodes_config.cache_dir),
            levels=self._pcodes_config.levels,
            manifest_url=self._pcodes_config.manifest_url,
            parquet_url_template=self._pcodes_config.parquet_url_template,
            manifest_group=self._pcodes_config.manifest_group,
        )
        if tags_pcodes:
            with connect(
                path=":memory:",
                memory_gb=self._memory_gb,
                temp_dir=self._cfg.duckdb.temp_dir,
            ) as admin_conn:
                self._pcode_cache = write_country_admin_parquets(
                    admin_conn,
                    iso3=area_id,
                    cache_entries=self._pcode_cache,
                    target_dir=out_root / "_pcodes",
                )
        return tags_pcodes

    def _process_all(
        self, boundary: Boundary, out_root: Path, publisher: HdxPublisher | None
    ) -> list[BuiltCategory]:
        """Every category, in completion order."""
        categories = self._cfg.categories
        if not (self._cfg.parallel.enabled and len(categories) > 1 and self._worker_count > 1):
            return [
                self._process_category(category, boundary, out_root, publisher)
                for category in categories
            ]
        workers = min(self._worker_count, len(categories))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._process_category, category, boundary, out_root, publisher)
                for category in categories
            ]
            return [future.result() for future in concurrent.futures.as_completed(futures)]

    def _process_category(
        self,
        category: CategoryConfig,
        boundary: Boundary,
        out_root: Path,
        publisher: HdxPublisher | None,
    ) -> BuiltCategory:
        started = time.time()
        log_prefix = f"[{category.name}/{self._runner.name}]"
        logger.info("%s starting", log_prefix)

        try:
            query = self._runner.query_for(self._cfg, category)
        except CategorySkippedError as skip:
            logger.info("%s skipped: %s", log_prefix, skip)
            return _early_result(category, "skipped", started, error=str(skip))

        formats = category.formats or self._cfg.output.formats
        if not formats:
            logger.info("%s skipped: no output formats configured", log_prefix)
            return _early_result(category, "skipped", started, error="no output formats configured")

        resumed = self._resumed_result(category, query, started, log_prefix)
        if resumed is not None:
            return resumed

        logger.info(
            "%s source: %s | snapshot: %s",
            log_prefix,
            query.dataset_source,
            query.snapshot_label,
        )
        duckdb_threads = max(2, cpu_count() // max(1, self._worker_count))
        table = f"{slugify(category.name)}_{os.getpid()}_{int(time.time() * 1000)}"
        db_path = Path(self._cfg.duckdb.temp_dir) / f"{table}.duckdb"
        conn = self._open_duckdb(db_path, duckdb_threads)
        job = _CategoryJob(
            category=category,
            query=query,
            boundary=boundary,
            out_root=out_root,
            publisher=publisher,
            conn=conn,
            table=table,
            started=started,
            log_prefix=log_prefix,
        )
        try:
            return self._export_category(job, formats)
        except Exception as exc:  # noqa: BLE001  per-category boundary; logged + reported
            logger.exception("%s failed", log_prefix)
            return _early_result(category, "failed", started, error=str(exc))
        finally:
            conn.close()
            db_path.unlink(missing_ok=True)

    def _resumed_result(
        self, category: CategoryConfig, query: SourceQuery, started: float, log_prefix: str
    ) -> BuiltCategory | None:
        """The earlier result of a category this snapshot already uploaded; combined mode tracks
        the single combined upload instead."""
        if not self._cfg.output.resume or self._state is None or self._cfg.hdx.combine:
            return None
        slug = slugify(category.name)
        entry = self._state.get(slug)
        snapshot_label = query.snapshot_label or "unknown"
        if not entry or not self._state.is_uploaded(slug, snapshot_label=snapshot_label):
            return None
        logger.info(
            "%s resume: already built and uploaded (%s); skipping", log_prefix, entry.uploaded_utc
        )
        return _early_result(
            category,
            "ok",
            started,
            feature_count=0,
            zip_paths=[Path(path) for path in entry.zip_paths],
            hdx_dataset=entry.hdx_dataset,
        )

    def _export_category(self, job: _CategoryJob, formats: list[str]) -> BuiltCategory:
        feature_count = self._query_into_table(job)
        if feature_count == 0:
            return BuiltCategory(result=self._empty_layer_result(job), category=job.category)

        content_hash = None
        if job.publisher is not None and self._skips_unchanged(self._cfg.hdx.combine):
            content_hash = self._content_hash(job.conn, job.table, job.category)
            unchanged = self._skip_if_unchanged(job, job.publisher, content_hash, feature_count)
            if unchanged is not None:
                return BuiltCategory(result=unchanged, category=job.category)

        self._tag_pcodes(job)
        self._transliterate(job)
        source_metadata = self._source_metadata(job)

        outputs = self._write_outputs(job, formats, feature_count, source_metadata)
        if outputs is None:
            # Every format failed; skipping rather than failing keeps one bad category
            # from turning the whole country red.
            logger.warning(
                "%s no formats succeeded; skipping HDX upload for this category", job.log_prefix
            )
            result = job.result(
                "skipped", feature_count=feature_count, error="no formats succeeded"
            )
            return BuiltCategory(result=result, category=job.category)
        total_mb = sum(path.stat().st_size for path in outputs.zip_paths) / (1024 * 1024)

        if source_metadata is not None:
            self._describe_layer(job, source_metadata, outputs)
        if self._state is not None:
            self._state.mark_built(
                job.slug,
                snapshot_label=job.query.snapshot_label or "unknown",
                zip_paths=outputs.zip_paths,
                metadata_json_path=outputs.metadata_json_path,
            )
        hdx_dataset = self._publish_layer(job, outputs, source_metadata, content_hash)

        logger.info(
            "%s done: %s features, %d artifact(s), %.0f MB total in %.1fs",
            job.log_prefix,
            f"{feature_count:,}",
            len(outputs.zip_paths),
            total_mb,
            time.time() - job.started,
        )
        return self._built_category(job, outputs, feature_count, source_metadata, hdx_dataset)

    def _describe_layer(
        self, job: _CategoryJob, source_metadata: SourceMetadata, outputs: _LayerOutputs
    ) -> None:
        """Write the layer's Item, its Collection when it is its own dataset, and its report."""
        outputs.layer = self._final_layer_assets(job, source_metadata, outputs)
        outputs.item_path = self._write_layer_metadata(
            outputs.layer, job.out_root, outputs.metadata_json_path
        )
        if self._cfg.output.report.enabled:
            self._write_local_report(
                category=job.category,
                source_metadata=source_metadata,
                report_path=job.out_root / f"{self._layer_file_stem(job)}_report.html",
                zip_paths=outputs.zip_paths,
                pmtiles_path=outputs.pmtiles_path,
                boundary_bbox=job.boundary.bbox,
                boundary_geojson=job.boundary.geojson,
                log_prefix=job.log_prefix,
            )

    def _built_category(
        self,
        job: _CategoryJob,
        outputs: _LayerOutputs,
        feature_count: int,
        source_metadata: SourceMetadata | None,
        hdx_dataset: str | None,
    ) -> BuiltCategory:
        return BuiltCategory(
            result=job.result(
                "ok",
                feature_count=feature_count,
                zip_paths=outputs.zip_paths,
                hdx_dataset=hdx_dataset,
            ),
            category=job.category,
            zip_paths=outputs.zip_paths,
            metadata_json_path=outputs.metadata_json_path,
            item_path=outputs.item_path,
            metadata_report=source_metadata.metadata if source_metadata is not None else None,
            source_metadata=source_metadata,
            geoparquet_path=outputs.geoparquet_path,
            publish_geoparquet=outputs.publish_geoparquet,
            geoparquet_url=outputs.geoparquet_url,
            query=job.query,
        )

    def _query_into_table(self, job: _CategoryJob) -> int:
        """Materialise the category's features inside the boundary; returns the feature count."""
        select_clause = build_select_clause(job.query.select_fields)
        where_clause = build_where_clause(
            job.boundary, job.query.where_conditions, job.query.bbox_mode
        )
        logger.info("%s querying source...", job.log_prefix)
        materialise_start = time.time()
        count = materialise(job.conn, job.table, job.query.source_expr, select_clause, where_clause)
        logger.info(
            "%s queried %s features in %.1fs",
            job.log_prefix,
            f"{count:,}",
            time.time() - materialise_start,
        )
        return count

    def _empty_layer_result(self, job: _CategoryJob) -> CategoryResult:
        logger.info("%s empty: no features within boundary", job.log_prefix)
        if job.publisher is not None and not self._cfg.hdx.combine:
            try:
                self._refresh_dataset_dates(job.category, job.query, job.publisher)
            except HDXError:
                # An empty layer has nothing to re-export, so a failed refresh only leaves
                # the older date in place.
                logger.exception("%s metadata refresh failed", job.log_prefix)
        return job.result("empty", feature_count=0)

    def _skip_if_unchanged(
        self, job: _CategoryJob, publisher: HdxPublisher, content_hash: str, feature_count: int
    ) -> CategoryResult | None:
        """The unchanged result once HDX holds the newer dates; None when the layer changed or
        is not on HDX, so it is exported. An HDXError during the refresh fails the layer."""
        if content_hash != self._published_content_hash(job.category):
            return None
        logger.info("%s unchanged since its last publish; dates only", job.log_prefix)
        refreshed = self._refresh_dataset_dates(
            job.category,
            job.query,
            publisher,
            temporal=_data_temporal_bounds(job.conn, job.table, job.category),
        )
        if refreshed is None:
            logger.info("%s has no HDX dataset to refresh; exporting", job.log_prefix)
            return None
        if self._state is not None:
            self._state.mark_built(
                job.slug,
                snapshot_label=job.query.snapshot_label or "unknown",
                zip_paths=[],
                metadata_json_path=None,
            )
            self._state.mark_uploaded(job.slug, hdx_dataset=refreshed)
        return job.result("unchanged", feature_count=feature_count, hdx_dataset=refreshed)

    def _tag_pcodes(self, job: _CategoryJob) -> None:
        if self._pcode_cache is None:
            return
        if job.category.skip_pcodes:
            logger.info("%s pcodes skipped (skip_pcodes=true)", job.log_prefix)
            return
        tag_start = time.time()
        logger.info(
            "%s tagging with pcodes (levels=%s)...", job.log_prefix, self._pcodes_config.levels
        )
        boundary_resolution = parse_boundary_resolution(
            job.category.boundary_resolution or self._pcodes_config.boundary_resolution
        )
        with self._pcode_tag_semaphore:
            tag_table(
                job.conn,
                table=job.table,
                iso3=self._cfg.iso3,
                cache_entries=self._pcode_cache,
                levels=self._pcodes_config.levels,
                geom_column="geom",
                boundary_resolution=boundary_resolution,
            )
        logger.info("%s pcodes tagged in %.1fs", job.log_prefix, time.time() - tag_start)

    def _transliterate(self, job: _CategoryJob) -> None:
        rules = job.category.transliterate
        if not rules:
            return
        transliteration_start = time.time()
        logger.info("%s transliterating %d column(s)...", job.log_prefix, len(rules))
        transliterate_table(job.conn, table=job.table, rules=rules)
        logger.info(
            "%s transliteration done in %.1fs", job.log_prefix, time.time() - transliteration_start
        )

    def _source_metadata(self, job: _CategoryJob) -> SourceMetadata | None:
        """The layer's metadata, when the run writes metadata or a report."""
        if not self._want_metadata():
            return None
        logger.info("%s computing metadata...", job.log_prefix)
        metadata_report = compute_metadata(
            job.conn,
            job.table,
            temporal_column=job.category.temporal.column,
        )
        return SourceMetadata(
            source_name=self._runner.name,
            snapshot_label=job.query.snapshot_label,
            dataset_source=job.query.dataset_source,
            generated_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            oex_version=_oex_version(),
            license_label=license_label(job.category.hdx.license),
            license_url=job.category.hdx.license_url,
            pcode_source_date=(
                next(iter(self._pcode_cache.values())).upstream_date if self._pcode_cache else None
            ),
            boundary=job.boundary.source,
            metadata=metadata_report,
        )

    def _layer_file_stem(self, job: _CategoryJob) -> str:
        return f"{layer_dataset_name(self._cfg, job.category)}_{self._runner.name}"

    def _write_outputs(
        self,
        job: _CategoryJob,
        formats: list[str],
        feature_count: int,
        source_metadata: SourceMetadata | None,
    ) -> _LayerOutputs | None:
        """Write every requested artifact; None when no format succeeded and nothing else was
        asked for."""
        combine = self._cfg.hdx.combine
        # Never zipped, so a client reads it in place over HTTP; it also feeds the
        # combined tiles, so it can be wanted without being requested.
        publish_geoparquet = "geoparquet" in formats
        want_geoparquet = publish_geoparquet or (combine and self._cfg.output.pmtiles.enabled)
        hdx_formats = [format_name for format_name in formats if format_name != "geoparquet"]

        logger.info("%s writing %d format(s): %s", job.log_prefix, len(hdx_formats), hdx_formats)
        zip_paths = self._materialise_outputs(
            conn=job.conn,
            table=job.table,
            slug=job.slug,
            category=job.category,
            query=job.query,
            formats=hdx_formats,
            out_root=job.out_root,
            metadata_report=self._archived_item(job, source_metadata),
            boundary=job.boundary,
            feature_count=feature_count,
        )
        if not zip_paths and not want_geoparquet:
            return None

        geoparquet_path = None
        if want_geoparquet:
            geoparquet_path = write_geoparquet(
                job.conn, job.table, job.out_root / "_layers" / f"{job.slug}.parquet"
            )
        pmtiles_path = self._write_category_tiles(job)
        # Taken before staging so the S3 upload stays out of the build time.
        export_seconds = time.time() - job.started
        geoparquet_url = None
        if geoparquet_path is not None:
            geoparquet_url = self._stage_layer_parquet(geoparquet_path, job.slug, job.log_prefix)

        metadata_json_path = None
        if source_metadata is not None and not combine:
            # A combined dataset is covered by one Collection over every layer.
            metadata_json_path = job.out_root / collection_filename(
                layer_dataset_name(self._cfg, job.category), self._runner.name
            )
        return _LayerOutputs(
            zip_paths=zip_paths,
            publish_geoparquet=publish_geoparquet,
            export_seconds=export_seconds,
            geoparquet_path=geoparquet_path,
            geoparquet_url=geoparquet_url,
            pmtiles_path=pmtiles_path,
            metadata_json_path=metadata_json_path,
        )

    def _archived_item(
        self, job: _CategoryJob, source_metadata: SourceMetadata | None
    ) -> dict | None:
        """The Item copy each zip carries; named before the artifacts exist, so it lists none."""
        if source_metadata is None:
            return None
        return to_payload(
            build_item(
                LayerAssets(
                    category=job.category,
                    title=dataset_title(self._cfg, job.category),
                    source_metadata=source_metadata,
                    dataset_name=layer_dataset_name(self._cfg, job.category),
                    footprint=self._item_footprint(job.boundary.geojson),
                ),
                self._cfg,
            )
        )

    def _write_category_tiles(self, job: _CategoryJob) -> Path | None:
        """The category's own tileset; combined mode builds one tileset over every layer."""
        if not self._cfg.output.pmtiles.enabled or self._cfg.hdx.combine:
            return None
        pmtiles_config = self._cfg.output.pmtiles
        pmtiles_path = job.out_root / f"{self._layer_file_stem(job)}.pmtiles"
        # Named for the category, so one map style serves every country's tiles.
        write_pmtiles(
            job.conn,
            job.table,
            pmtiles_path,
            layer=job.slug,
            min_zoom=pmtiles_config.min_zoom,
            max_zoom=pmtiles_config.max_zoom,
            max_extra_zooms=pmtiles_config.max_extra_zooms,
        )
        return pmtiles_path

    def _final_layer_assets(
        self, job: _CategoryJob, source_metadata: SourceMetadata, outputs: _LayerOutputs
    ) -> LayerAssets:
        """After the artifacts exist, so the layer can name every one as an asset."""
        return LayerAssets(
            category=job.category,
            title=dataset_title(self._cfg, job.category),
            source_metadata=source_metadata,
            dataset_name=layer_dataset_name(self._cfg, job.category),
            artifacts=outputs.zip_paths,
            geoparquet_url=outputs.geoparquet_url,
            geoparquet_path=outputs.geoparquet_path,
            pmtiles_url=self._artifact_url(outputs.pmtiles_path, job.slug),
            pmtiles_path=outputs.pmtiles_path,
            export_seconds=outputs.export_seconds,
            footprint=self._item_footprint(job.boundary.geojson),
        )

    def _publish_layer(
        self,
        job: _CategoryJob,
        outputs: _LayerOutputs,
        source_metadata: SourceMetadata | None,
        content_hash: str | None,
    ) -> str | None:
        """Publish a category on its own; combined mode publishes every layer at once later.
        Returns the HDX dataset name when HDX received it."""
        if self._cfg.hdx.combine:
            return None
        if job.publisher is not None:
            return self._publish_to_hdx(job, job.publisher, outputs, source_metadata, content_hash)
        if self._cfg.output.s3.enabled:
            self._upload_category_to_s3(outputs.zip_paths, job.slug, job.log_prefix)
        return None

    def _publish_to_hdx(
        self,
        job: _CategoryJob,
        publisher: HdxPublisher,
        outputs: _LayerOutputs,
        source_metadata: SourceMetadata | None,
        content_hash: str | None,
    ) -> str:
        logger.info("%s uploading %d artifact(s) to HDX...", job.log_prefix, len(outputs.zip_paths))
        temporal = source_metadata.metadata.temporal if source_metadata is not None else None
        temporal_min, temporal_max = _hdx_date_range(temporal)
        extra_resources = []
        if outputs.pmtiles_path is not None:
            extra_resources.append(
                ExtraResource(
                    path=outputs.pmtiles_path,
                    format_name="pmtiles",
                    description=(
                        f"Vector tiles of {category_label(job.category)} for web maps (PMTiles)."
                    ),
                )
            )
        publish_context = PublishContext(
            snapshot_date=job.query.snapshot_date,
            source_name=self._runner.name,
            metadata_json_path=outputs.metadata_json_path,
            category_report_enabled=self._cfg.output.report.enabled,
            output_dir=job.out_root,
            s3=self._cfg.output.s3,
            temporal_min=temporal_min,
            temporal_max=temporal_max,
            boundary_bbox=job.boundary.bbox,
            published=self._published,
        )
        dataset_name = publisher.publish(
            self._cfg,
            job.category,
            outputs.zip_paths,
            publish_context,
            extra_resources=extra_resources,
            geoparquet_url=outputs.geoparquet_url if outputs.publish_geoparquet else None,
            geoparquet_path=outputs.geoparquet_path,
        )
        if self._state is not None:
            self._state.mark_uploaded(job.slug, hdx_dataset=dataset_name)
        if content_hash is not None and outputs.layer is not None:
            # Only a layer HDX accepted may be skipped next time.
            outputs.layer.content_hash = content_hash
            self._write_item(outputs.layer, job.out_root)
        if self._cfg.output.remove_after_upload:
            extra_paths = [resource.path for resource in extra_resources]
            if outputs.item_path is not None:
                extra_paths.append(outputs.item_path)
            _remove_uploaded_outputs(outputs.zip_paths, outputs.metadata_json_path, extra_paths)
            logger.info(
                "%s removed %d local output(s) after upload", job.log_prefix, len(outputs.zip_paths)
            )
        return dataset_name

    def _page_layer(
        self,
        category: CategoryConfig,
        source_metadata: SourceMetadata,
        zip_paths: list[Path],
        pmtiles_path: Path | None,
        *,
        tileset: tuple[str, str] | None = None,
    ) -> PageLayer:
        """A layer as the page shows it. Hrefs are filenames, so the page works under
        any host serving the output directory."""
        slug = slugify(category.name)
        assets = {
            asset_key(path.name, slug): {"href": path.name, "file:size": path.stat().st_size}
            for path in zip_paths
        }
        geoparquet = Path("_layers") / f"{slug}.parquet"
        if zip_paths and (zip_paths[0].parent / geoparquet).exists():
            size = (zip_paths[0].parent / geoparquet).stat().st_size
            assets["geoparquet"] = {"href": str(geoparquet), "file:size": size}
        item = item_filename(layer_dataset_name(self._cfg, category), self._runner.name)
        downloads = downloads_from_assets(assets)
        own_tiles = (pmtiles_path.name, slug) if pmtiles_path else None
        index = [slugify(category.name) for category in self._cfg.categories].index(slug)
        palette = self._cfg.output.report.palette or DEFAULT_PALETTE
        return PageLayer(
            key=f"{slug}-{self._runner.name}",
            label=layer_label(category, self._runner.name),
            color=palette[index % len(palette)],
            source=source_metadata,
            downloads=downloads,
            tileset=tileset or own_tiles,
            match_key=f"{slug}|{self._runner.name}" if tileset else None,
            metadata_href=item if self._want_metadata() else None,
        )

    def _write_local_report(
        self,
        *,
        category: CategoryConfig,
        source_metadata: SourceMetadata,
        report_path: Path,
        zip_paths: list[Path],
        pmtiles_path: Path | None,
        boundary_bbox: tuple[float, float, float, float],
        log_prefix: str,
        boundary_geojson: str | None = None,
    ) -> None:
        """Write this source's report next to its outputs, mapping the local tileset."""
        layer = self._page_layer(category, source_metadata, zip_paths, pmtiles_path)
        report_path.write_text(
            render_dataset_page(
                title=dataset_title(self._cfg, category),
                layers=[layer],
                theme=self._cfg.output.report.theme,
                boundary_bbox=boundary_bbox,
                boundary_geojson=boundary_geojson,
                map_assets=self._cfg.output.report.map_assets,
                dataset_url=self._hdx_url(self._published_dataset_name(category)),
                related=related_dataset_links(self._cfg, category, self._published),
            ),
            encoding="utf-8",
        )
        logger.info("%s wrote local report -> %s", log_prefix, report_path)

    def _finish_combined(
        self,
        built_list: list[BuiltCategory],
        out_root: Path,
        publisher: HdxPublisher | None,
        *,
        peeked_label: str | None,
        boundary_bbox: tuple[float, float, float, float],
        boundary_geojson: str | None = None,
    ) -> None:
        """Build the combined artifacts, then publish them when HDX push is on."""
        iso3 = self._cfg.iso3.upper()
        built_ok = [
            built for built in built_list if built.result.status == "ok" and built.zip_paths
        ]
        if not built_ok:
            logger.warning("[%s/%s] combine: no successful categories", iso3, self._runner.name)
            return

        order = {category.name: index for index, category in enumerate(self._cfg.categories)}
        built_ok.sort(key=lambda built: order.get(built.category.name, len(order)))

        hdx_dataset_name = combined_dataset_name(self._cfg)
        pmtiles_path = self._build_combined_tiles(built_ok, out_root, hdx_dataset_name)

        if publisher is None:
            # Written locally too, so the output directory reads as a catalog on its own.
            self._write_combined_metadata(
                built_ok, out_root, hdx_dataset_name, pmtiles_path, boundary_geojson
            )
            self._write_local_page(
                built_ok,
                out_root,
                hdx_dataset_name=hdx_dataset_name,
                pmtiles_path=pmtiles_path,
                boundary_bbox=boundary_bbox,
                boundary_geojson=boundary_geojson,
            )
            return

        self._publish_combined(
            built_ok,
            out_root,
            publisher,
            hdx_dataset_name=hdx_dataset_name,
            pmtiles_path=pmtiles_path,
            peeked_label=peeked_label,
            boundary_bbox=boundary_bbox,
            boundary_geojson=boundary_geojson,
        )

    def _write_local_page(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        *,
        hdx_dataset_name: str,
        pmtiles_path: Path | None,
        boundary_bbox: tuple[float, float, float, float],
        boundary_geojson: str | None = None,
    ) -> None:
        """The dataset page from this run's own layers. The published page also covers layers an
        earlier run of another source left on the dataset, which only HDX knows about."""
        if not self._cfg.output.report.enabled:
            return
        tileset = (pmtiles_path.name, pmtiles_path.stem) if pmtiles_path else None
        layers = [
            self._page_layer(
                built.category, built.source_metadata, built.zip_paths, None, tileset=tileset
            )
            for built in built_ok
            if built.source_metadata is not None
        ]
        if not layers:
            return

        place = country_name(self._cfg.iso3, dataset_name=self._cfg.dataset_name)
        hdx_source = long_label(self._runner.name)
        html = render_dataset_page(
            title=combined_title(self._cfg, place, hdx_source),
            layers=layers,
            theme=self._cfg.output.report.theme,
            boundary_bbox=boundary_bbox,
            boundary_geojson=boundary_geojson,
            map_assets=self._cfg.output.report.map_assets,
            dataset_url=self._hdx_url(hdx_dataset_name),
        )
        page_path = out_root / f"{hdx_dataset_name}_overview.html"
        page_path.write_text(html, encoding="utf-8")
        logger.info(
            "[%s/%s] combine: wrote local overview (%d layers) -> %s",
            self._cfg.iso3.upper(),
            self._runner.name,
            len(layers),
            page_path,
        )

    def _publish_combined(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        publisher: HdxPublisher,
        *,
        hdx_dataset_name: str,
        pmtiles_path: Path | None,
        peeked_label: str | None,
        boundary_bbox: tuple[float, float, float, float],
        boundary_geojson: str | None = None,
    ) -> None:
        iso3 = self._cfg.iso3.upper()
        entries = [
            CombinedCategory(
                category=built.category,
                zip_paths=built.zip_paths,
                metadata_json_path=built.metadata_json_path,
                geoparquet_url=built.geoparquet_url if built.publish_geoparquet else None,
                geoparquet_path=built.geoparquet_path,
            )
            for built in built_ok
        ]
        metadata_path = self._write_combined_metadata(
            built_ok, out_root, hdx_dataset_name, pmtiles_path, boundary_geojson
        )
        temporal_min, temporal_max = _combined_temporal_bounds(
            [built.metadata_report for built in built_ok]
        )
        first_query = built_ok[0].query
        assert first_query is not None
        publish_context = PublishContext(
            snapshot_date=first_query.snapshot_date,
            source_name=self._runner.name,
            output_dir=out_root,
            s3=self._cfg.output.s3,
            temporal_min=temporal_min,
            temporal_max=temporal_max,
            boundary_bbox=boundary_bbox,
            boundary_geojson=boundary_geojson,
        )
        logger.info(
            "[%s/%s] combine: publishing %d categories as single dataset %s",
            iso3,
            self._runner.name,
            len(entries),
            hdx_dataset_name,
        )
        published_name = publisher.publish_combined(
            self._cfg,
            entries,
            publish_context,
            pmtiles_path=pmtiles_path,
            metadata_path=metadata_path,
            page_enabled=self._cfg.output.report.enabled,
        )

        if self._state is not None:
            snapshot_label = peeked_label or (first_query.snapshot_label or "unknown")
            self._state.mark_built(
                COMBINED_STATE_KEY,
                snapshot_label=snapshot_label,
                zip_paths=[],
                metadata_json_path=None,
            )
            self._state.mark_uploaded(COMBINED_STATE_KEY, hdx_dataset=published_name)

        for built in built_ok:
            built.result.hdx_dataset = published_name

        if self._cfg.output.remove_after_upload:
            # Keep the per-layer GeoParquets: they are a deliverable and the
            # source of truth for future combined-tile rebuilds.
            for built in built_ok:
                item_paths = [built.item_path] if built.item_path else None
                _remove_uploaded_outputs(built.zip_paths, built.metadata_json_path, item_paths)
            if pmtiles_path is not None:
                pmtiles_path.unlink(missing_ok=True)
            if metadata_path is not None:
                metadata_path.unlink(missing_ok=True)
            logger.info(
                "[%s/%s] combine: removed local outputs after upload", iso3, self._runner.name
            )

    def _item_footprint(self, boundary_geojson: str | None) -> str | None:
        """Only a simplified outline is small enough to embed in every Item."""
        return boundary_geojson if self._cfg.boundary.simplify_meters else None

    def _write_combined_metadata(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        hdx_dataset_name: str,
        pmtiles_path: Path | None = None,
        boundary_geojson: str | None = None,
    ) -> Path | None:
        """One Collection over every layer this source built, named per source so a second
        source's run does not overwrite the first's."""
        layers = [
            LayerAssets(
                category=built.category,
                title=dataset_title(self._cfg, built.category),
                source_metadata=metadata,
                dataset_name=layer_dataset_name(self._cfg, built.category),
                artifacts=built.zip_paths,
                geoparquet_url=built.geoparquet_url,
                footprint=self._item_footprint(boundary_geojson),
            )
            for built in built_ok
            if (metadata := built.source_metadata) is not None
        ]
        if not layers or not self._want_metadata():
            return None
        path = out_root / collection_filename(hdx_dataset_name, self._runner.name)
        write_json(
            self._collection_for(
                layers, hdx_dataset_name, COMBINED_SLUG, pmtiles_path=pmtiles_path
            ),
            path,
        )
        if self._stage_metadata(path, COMBINED_SLUG):
            self._staged_collections.append(
                (
                    self._collection_title(layers),
                    href_or_filename(self._cfg, COMBINED_SLUG, path.name),
                )
            )
        return path

    def _artifact_url(self, path: Path | None, category_slug: str) -> str | None:
        return asset_href(self._cfg, category_slug, path.name) if path else None

    def _stages_stac(self) -> bool:
        """STAC goes to the bucket only when the files it links to go there too; a combined
        run uploads its files only when it publishes to HDX."""
        return self._cfg.output.s3.enabled and (self._cfg.hdx.push or not self._cfg.hdx.combine)

    def _want_metadata(self) -> bool:
        """The report is rendered from the layer metadata, so asking for it asks for both."""
        return self._cfg.output.metadata or self._cfg.output.report.enabled

    def _write_layer_metadata(
        self, layer: LayerAssets, out_root: Path, collection_path: Path | None
    ) -> Path:
        """The layer's Item file, and its Collection when the layer is its own dataset."""
        item_path = self._write_item(layer, out_root)
        if collection_path is not None:
            collection_id, collection_slug, collection_href = self._layer_collection(layer)
            write_json(
                self._collection_for([layer], collection_id, collection_slug), collection_path
            )
            if self._stage_metadata(collection_path, collection_slug):
                self._staged_collections.append((self._collection_title([layer]), collection_href))
        return item_path

    def _layer_collection(self, layer: LayerAssets) -> tuple[str, str, str]:
        combined = self._cfg.hdx.combine
        collection_id = combined_dataset_name(self._cfg) if combined else layer.dataset_name
        collection_slug = COMBINED_SLUG if combined else slugify(layer.category.name)
        collection_href = href_or_filename(
            self._cfg, collection_slug, collection_filename(collection_id, self._runner.name)
        )
        return collection_id, collection_slug, collection_href

    def _write_item(self, layer: LayerAssets, out_root: Path) -> Path:
        """The filename is derived here so the staged key and the `rel="item"` href cannot
        drift."""
        item_path = out_root / item_filename(layer.dataset_name, self._runner.name)
        collection_id, _, collection_href = self._layer_collection(layer)
        item = build_item(
            layer,
            self._cfg,
            self_href=item_href(self._cfg, layer),
            collection_id=collection_id,
            collection_href=collection_href,
            hdx_url=self._hdx_url(self._published_dataset_name(layer.category)),
            page_url=self._page_url(layer.category),
        )
        write_json(item, item_path)
        self._stage_metadata(item_path, slugify(layer.category.name))
        return item_path

    def _area_title(self) -> str:
        cfg = self._cfg
        if not cfg.iso3:
            return cfg.dataset_name or cfg.output.s3.folder
        place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
        return (
            combined_title(cfg, place, "") if cfg.hdx.combine and cfg.hdx.combined.title else place
        )

    def _published_dataset_name(self, category: CategoryConfig) -> str:
        """The HDX dataset a category's layer is published to, which an S3 folder does not
        change, unlike the names of the files themselves."""
        if self._cfg.hdx.combine:
            return combined_dataset_name(self._cfg)
        return category_dataset_name(self._cfg, category)

    def _hdx_url(self, dataset_name: str) -> str | None:
        """The HDX page a published dataset lives on; None when nothing goes to HDX."""
        return hdx_dataset_url(self._cfg.hdx.site, dataset_name) if self._cfg.hdx.push else None

    def _collection_for(
        self,
        layers: list[LayerAssets],
        dataset_name: str,
        category_slug: str,
        *,
        pmtiles_path: Path | None = None,
    ) -> pystac.Collection:
        filename = collection_filename(dataset_name, self._runner.name)
        return build_collection(
            dataset_name=dataset_name,
            title=self._collection_title(layers),
            description=self._collection_description(layers),
            layers=layers,
            cfg=self._cfg,
            self_href=href_or_filename(self._cfg, category_slug, filename),
            pmtiles_url=self._artifact_url(pmtiles_path, category_slug),
            pmtiles_path=pmtiles_path,
            hdx_url=self._hdx_url(self._published_dataset_name(layers[0].category)),
            related=self._related_collections(layers[0].category),
            page_url=self._page_url(layers[0].category),
        )

    def _page_url(self, category: CategoryConfig) -> str | None:
        """Where the publisher puts the dataset's page, which only a run publishing to HDX builds."""
        cfg = self._cfg
        if not (cfg.hdx.push and cfg.output.report.enabled):
            return None
        name = self._published_dataset_name(category)
        if cfg.hdx.combine:
            return asset_href(cfg, "overview", f"{name}_overview.html")
        return asset_href(cfg, slugify(category.name), f"{name}_report.html")

    def _related_collections(self, category: CategoryConfig) -> list[tuple[str, str]]:
        """The area's other category Collections, which a run publishing to HDX uploads."""
        if not links_related_datasets(self._cfg):
            return []
        return [
            (
                category_label(other),
                href_or_filename(
                    self._cfg,
                    slugify(other.name),
                    collection_filename(layer_dataset_name(self._cfg, other), self._runner.name),
                ),
            )
            for other in self._cfg.categories
            if other.name != category.name and (other.name, self._runner.name) in self._published
        ]

    def _collection_title(self, layers: list[LayerAssets]) -> str:
        """The same title the dataset carries on HDX, so the two do not diverge."""
        if not self._cfg.hdx.combine:
            return dataset_title(self._cfg, layers[0].category)
        place = country_name(self._cfg.iso3, dataset_name=self._cfg.dataset_name)
        return combined_title(self._cfg, place, long_label(self._runner.name))

    def _collection_description(self, layers: list[LayerAssets]) -> str:
        if not self._cfg.hdx.combine:
            return layers[0].category.hdx.notes
        return self._cfg.hdx.combined.notes or self._collection_title(layers)

    def _stage_metadata(self, path: Path, category_slug: str) -> bool:
        """Items are linked from the Collection rather than listed on HDX, so they go
        straight to the bucket the Collection points at."""
        if not self._stages_stac():
            return False
        s3_config = self._cfg.output.s3
        bucket, prefix, region, endpoint_url, acl = resolve_with_bucket(s3_config)
        key = category_artifact_key(s3_config, prefix, self._cfg.iso3, category_slug, path.name)
        s3_upload(path, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl)
        return True

    def _build_combined_tiles(
        self, built_ok: list[BuiltCategory], out_root: Path, hdx_dataset_name: str
    ) -> Path | None:
        """Merge this run's GeoParquets plus other sources' staged GeoParquets into one tileset."""
        if not self._cfg.output.pmtiles.enabled:
            return None
        order = {
            slugify(category.name): index for index, category in enumerate(self._cfg.categories)
        }
        by_slug = {slugify(category.name): category for category in self._cfg.categories}

        def tiles_on(slug: str, source: str) -> bool:
            block = getattr(by_slug.get(slug), source, None)
            return getattr(block, "tiles", True) if block is not None else True

        layers = [
            TileLayer(
                path=str(built.geoparquet_path),
                category=slugify(built.category.name),
                source=self._runner.name,
            )
            for built in built_ok
            if built.geoparquet_path is not None
            and tiles_on(slugify(built.category.name), self._runner.name)
        ]
        if self._cfg.output.s3.enabled:
            turned_off = turned_off_sources(self._cfg).names
            for source, slug, url in list_layer_urls(
                self._cfg.output.s3, self._cfg.iso3, exclude_source=self._runner.name
            ):
                if source in turned_off:
                    logger.info(
                        "[%s/%s] combine: skipping staged layer %s/%s, source turned off",
                        self._cfg.iso3.upper(),
                        self._runner.name,
                        source,
                        slug,
                    )
                    continue
                # The staging area is never pruned, so it outlives categories this
                # config dropped. Merging them bloats the tileset with unlabelled data.
                if slug not in order:
                    logger.info(
                        "[%s/%s] combine: skipping staged layer %s/%s, not in this config",
                        self._cfg.iso3.upper(),
                        self._runner.name,
                        source,
                        slug,
                    )
                    continue
                if not tiles_on(slug, source):
                    logger.info(
                        "[%s/%s] combine: skipping %s/%s, tiles disabled for category",
                        self._cfg.iso3.upper(),
                        self._runner.name,
                        source,
                        slug,
                    )
                    continue
                layers.append(TileLayer(path=url, category=slug, source=source))
        if not layers:
            return None
        layers.sort(
            key=lambda layer: (
                SOURCE_RANK.get(layer.source, 99),
                order.get(layer.category, len(order)),
            )
        )

        pmtiles_config = self._cfg.output.pmtiles
        pmtiles_path = out_root / f"{hdx_dataset_name}.pmtiles"
        tiles_db = (
            Path(self._cfg.duckdb.temp_dir)
            / f"{hdx_dataset_name}_tiles_{int(time.time() * 1000)}.duckdb"
        )
        conn = self._open_duckdb(tiles_db, max(2, cpu_count()))
        try:
            build_combined_pmtiles(
                conn,
                layers,
                pmtiles_path,
                min_zoom=pmtiles_config.min_zoom,
                max_zoom=pmtiles_config.max_zoom,
                max_extra_zooms=pmtiles_config.max_extra_zooms,
            )
            sources = sorted({layer.source for layer in layers})
            logger.info(
                "[%s/%s] combine: built tileset from %d layers across %s -> %s",
                self._cfg.iso3.upper(),
                self._runner.name,
                len(layers),
                ", ".join(sources),
                pmtiles_path,
            )
        finally:
            conn.close()
            tiles_db.unlink(missing_ok=True)
        return pmtiles_path

    def _artifact_geometry(self, zip_path: Path, category_slug: str) -> str:
        """The geometry label in an artifact's filename."""
        geometry = partition_of(zip_path.name, category_slug)
        if not geometry:
            raise ValueError(f"no geometry label in artifact name {zip_path.name}")
        return geometry

    def _upload_category_to_s3(self, zip_paths: list[Path], slug: str, log_prefix: str) -> None:
        """Upload a category's artifacts under its S3 keys."""
        s3_config = self._cfg.output.s3
        bucket, prefix, region, endpoint_url, acl = resolve_with_bucket(s3_config)
        logger.info("%s uploading %d artifact(s) to S3...", log_prefix, len(zip_paths))
        for zip_path in zip_paths:
            key = category_artifact_key(
                s3_config,
                prefix,
                self._cfg.iso3,
                slug,
                zip_path.name,
                geometry=self._artifact_geometry(zip_path, slug)
                if s3_config.nest_by_geometry
                else "",
            )
            s3_upload(
                zip_path, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl
            )
        if self._state is not None:
            self._state.mark_uploaded(slug, hdx_dataset=None)
        if self._cfg.output.remove_after_upload:
            _remove_uploaded_outputs(zip_paths, None)
            logger.info("%s removed %d local output(s) after upload", log_prefix, len(zip_paths))

    def _stage_layer_parquet(self, path: Path, slug: str, log_prefix: str) -> str | None:
        """Upload a layer's GeoParquet to its stable key, which every source's runs share and
        the HDX GeoParquet resource links to rather than copying."""
        s3_config = self._cfg.output.s3
        if not s3_config.enabled:
            return None
        bucket, prefix, region, endpoint_url, acl = resolve_with_bucket(s3_config)
        key = build_layer_key(
            prefix, self._cfg.iso3, self._runner.name, slug, self._cfg.output.s3.folder
        )
        url = s3_upload(
            path, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl
        )
        logger.info("%s staged layer geoparquet -> %s", log_prefix, url)
        return url

    def _skips_unchanged(self, combine: bool) -> bool:
        """The fingerprint lives on the published STAC Item, which needs metadata and S3."""
        output = self._cfg.output
        return (
            output.skip_unchanged
            and output.resume
            and output.s3.enabled
            and not combine
            and self._want_metadata()
        )

    def _content_hash(
        self, conn: duckdb.DuckDBPyConnection, table: str, category: CategoryConfig
    ) -> str:
        """Order-free hash of the queried features, joined with everything that shapes the
        published layer, so a new oex, setting or admin source also counts as a change."""
        # A sum, unlike XOR, keeps two copies of a row from cancelling out.
        [(total, rows)] = conn.execute(
            f"SELECT sum(hash(t)::HUGEINT), count(*) FROM {table} t"
        ).fetchall()
        recipe = json.dumps(self._layer_recipe(category), sort_keys=True)
        return hashlib.sha256(f"{total}:{rows}:{recipe}".encode()).hexdigest()

    def _layer_recipe(self, category: CategoryConfig) -> dict:
        """Settings that change the published layer; paths and resource limits differ between
        machines and runs without changing it, so they stay out."""
        cfg = self._cfg
        output = asdict(cfg.output)
        for local in ("dir", "resume", "remove_after_upload", "skip_unchanged"):
            output.pop(local)
        hdx = {
            key: value
            for key, value in asdict(cfg.hdx).items()
            if key not in ("api_key", "user_agent")
        }
        pcodes = {
            key: value for key, value in asdict(self._pcodes_config).items() if key != "cache_dir"
        }
        return {
            "oex": _oex_version(),
            "dataset": [cfg.iso3, cfg.key, cfg.dataset_name, cfg.subnational, cfg.frequency],
            "boundary": asdict(cfg.boundary),
            "output": output,
            "hdx": hdx,
            "category": asdict(category),
            "pcodes": pcodes,
            "pcode_dates": sorted(
                entry.upstream_date for entry in (self._pcode_cache or {}).values()
            ),
        }

    def _published_content_hash(self, category: CategoryConfig) -> str | None:
        """None before a layer's first publish, so the layer is exported and uploaded."""
        filename = item_filename(layer_dataset_name(self._cfg, category), self._runner.name)
        s3_config = self._cfg.output.s3
        _, prefix, _, _, _ = s3_resolve(s3_config)
        key = category_artifact_key(
            s3_config, prefix, self._cfg.iso3, slugify(category.name), filename
        )
        item, _ = S3CatalogStore(self._cfg).read(key)
        return None if item is None else item.get("properties", {}).get("oex:content_hash")

    def _refresh_dataset_dates(
        self,
        category: CategoryConfig,
        query: SourceQuery,
        publisher: HdxPublisher,
        temporal: tuple[datetime | None, datetime | None] = (None, None),
    ) -> str | None:
        """Restate the snapshot date on a dataset whose files stay as they are; None when it is
        not on HDX. An HDXError raises, so each caller decides what a failed refresh means."""
        return publisher.refresh_metadata(
            self._cfg,
            category,
            PublishContext(
                snapshot_date=query.snapshot_date,
                source_name=self._runner.name,
                s3=self._cfg.output.s3,
                temporal_min=temporal[0],
                temporal_max=temporal[1],
            ),
        )

    def _materialise_outputs(
        self,
        *,
        conn,
        table: str,
        slug: str,
        category: CategoryConfig,
        query: SourceQuery,
        formats: list[str],
        out_root: Path,
        metadata_report: dict | None,
        boundary,
        feature_count: int,
    ) -> list[Path]:
        zip_paths: list[Path] = []
        # An empty label means one artifact per format, covering every geometry.
        if self._cfg.output.split_by_geometry:
            partitions = [
                (label, self._geometry_view(conn, table, label, types))
                for label, types in sorted(geometry_labels(conn, table).items())
            ]
        else:
            partitions = [("", table)]

        for format_name, (geometry, source_table) in itertools.product(formats, partitions):
            geometry_segment = f"_{geometry}" if geometry else ""
            stage_dir = out_root / f"_stage_{slug}{geometry_segment}_{format_name}"
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            stage_dir.mkdir(parents=True)
            try:
                try:
                    files = write_format(conn, source_table, slug, format_name, stage_dir)
                except Exception as exc:  # noqa: BLE001 - per-format boundary
                    # GDAL can fail mid-write on corrupt OSM coordinates or run out of memory,
                    # and the category's other formats may still succeed.
                    logger.warning(
                        "%s [%s] format write failed (%s); skipping this format",
                        f"[{category.name}/{self._runner.name}]",
                        format_name,
                        exc,
                    )
                    continue
                if not files:
                    continue
                # HDX's filestore does not recognise FlatGeobuf, so it is neither zipped nor
                # published.
                if format_name == "fgb":
                    continue
                s3_config = self._cfg.output.s3
                source_segment = f"_{self._runner.name}" if s3_config.name_include_source else ""
                stem = f"{resource_prefix(self._cfg)}_{slug}{geometry_segment}{source_segment}"
                if not self._should_zip(category, format_name):
                    zip_paths.append(
                        _publish_unzipped(
                            files[0],
                            out_root / f"{stem}.{format_name}",
                            f"[{category.name}/{self._runner.name}]",
                        )
                    )
                    continue
                zip_path = out_root / f"{stem}_{format_name}.zip"
                readme_lines = self._build_readme(
                    format_name=format_name,
                    category=category,
                    query=query,
                    boundary=boundary,
                    feature_count=feature_count,
                )
                make_zip(
                    stage_dir,
                    zip_path,
                    readme_lines=readme_lines,
                    config_snapshot={
                        "iso3": self._cfg.iso3,
                        "category": asdict(category),
                        "source": self._runner.name,
                    },
                    metadata_report=metadata_report,
                )
                zip_paths.append(zip_path)
            finally:
                # make_zip already removed it when the format was zipped.
                if stage_dir.exists():
                    shutil.rmtree(stage_dir)
        return zip_paths

    def _should_zip(self, category: CategoryConfig, format_name: str) -> bool:
        """A shapefile is a set of sidecar files, so it is zipped whatever the config says."""
        if format_name == "shp":
            return True
        chosen = category.zip_formats
        if chosen is None:
            chosen = self._cfg.output.zip_formats
        return chosen is None or format_name in chosen

    def _geometry_view(self, conn, table: str, label: str, types: list[str]) -> str:
        """A view over one geometry label, so each artifact holds a single type."""
        view = f"{table}_{label}"
        conn.execute(
            f"CREATE OR REPLACE TEMP VIEW {view} AS "
            f"SELECT * FROM {table} WHERE {geometry_type_filter(types)}"
        )
        return view

    def _build_readme(
        self,
        *,
        format_name: str,
        category: CategoryConfig,
        query: SourceQuery,
        boundary,
        feature_count: int,
    ) -> list[str]:
        bbox = ", ".join(f"{coordinate:.4f}" for coordinate in boundary.bbox)
        license_url = category.hdx.license_url or "(not specified)"
        wrapped_description = _wrap_paragraph(query.source_description, indent="  ", width=78)
        format_notes = _format_notes(format_name)
        return [
            "oex export",
            "==========",
            "",
            f"Generated:        {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"oex version:      {_oex_version()}",
            f"Project:          {_PROJECT_URL}",
            "",
            f"Country (ISO3):   {self._cfg.iso3.upper()}",
            f"Boundary:         {boundary.source}",
            f"Bounding box:     ({bbox})",
            "",
            f"Dataset:          {category.name}",
            f"Format:           {_FORMAT_LABELS.get(format_name, format_name)}",
            f"Features:         {feature_count:,}",
            "",
            f"Source:           {category.hdx.dataset_source or query.dataset_source}",
            f"Source URL:       {query.source_url}",
            f"Snapshot:         {query.snapshot_label}",
            f"License:          {license_label(category.hdx.license)}",
            f"License URL:      {license_url}",
            "",
            "About the source",
            *wrapped_description,
            "",
            *(["Notes", *format_notes, ""] if format_notes else []),
            f"Feedback:         {_PROJECT_URL}/issues",
        ] + ([line for line in query.extra_readme_lines] if query.extra_readme_lines else [])


def _publish_unzipped(staged: Path, target: Path, log_prefix: str) -> Path:
    """Move a single-file format out of the stage directory, which is deleted straight after."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), target)
    logger.info(
        "%s published %s unzipped (%.2f MB)",
        log_prefix,
        target.name,
        target.stat().st_size / (1024**2),
    )
    return target


def _remove_uploaded_outputs(
    zip_paths: list[Path],
    metadata_json_path: Path | None,
    extra_paths: list[Path] | None = None,
) -> None:
    """Delete local artifacts once their upload succeeded."""
    for path in zip_paths:
        path.unlink(missing_ok=True)
    if metadata_json_path is not None:
        metadata_json_path.unlink(missing_ok=True)
    for path in extra_paths or []:
        path.unlink(missing_ok=True)


def _wrap_paragraph(text: str, *, indent: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _parse_temporal(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc(value)
    except ValueError:
        logger.warning(
            "temporal bound %r is not ISO-8601; HDX period falls back to snapshot", value
        )
        return None


def _data_temporal_bounds(
    conn: duckdb.DuckDBPyConnection, table: str, category: CategoryConfig
) -> tuple[datetime | None, datetime | None]:
    """The data's own date range, which a full publish also gives HDX."""
    if not category.temporal.column:
        return (None, None)
    return _hdx_date_range(temporal_bounds(conn, table, category.temporal.column))


def _hdx_date_range(temporal: TemporalReport | None) -> tuple[datetime | None, datetime | None]:
    if temporal is None:
        return (None, None)
    return (_parse_temporal(temporal.min), _parse_temporal(temporal.max))


def _combined_temporal_bounds(
    metadata_reports: list[MetadataReport | None],
) -> tuple[datetime | None, datetime | None]:
    earliest_values: list[datetime] = []
    latest_values: list[datetime] = []
    for report in metadata_reports:
        earliest, latest = _hdx_date_range(report.temporal if report is not None else None)
        if earliest is not None:
            earliest_values.append(earliest)
        if latest is not None:
            latest_values.append(latest)
    return (
        min(earliest_values) if earliest_values else None,
        max(latest_values) if latest_values else None,
    )


def _format_notes(format_name: str) -> list[str]:
    if format_name == "shp":
        return [
            "  - Shapefile output is split by geometry type:",
            "    <category>_polygons.shp, <category>_lines.shp, <category>_points.shp.",
            "    This is a shapefile-format limitation, not a data limitation.",
            "  - Field names are truncated to 10 characters in shp; gpkg keeps them full.",
        ]
    if format_name == "gpkg":
        return [
            "  - GeoPackage holds all geometry types in a single .gpkg file.",
            "  - Recommended for QGIS, ArcGIS, GDAL/OGR.",
        ]
    if format_name == "geojson":
        return [
            "  - GeoJSON is a single-file text format. Consider gpkg for very large layers.",
        ]
    if format_name == "kml":
        return [
            "  - KML opens directly in Google Earth and most desktop GIS.",
            "  - Single XML file; can grow large for big layers. Prefer gpkg above ~1M features.",
        ]
    return []
