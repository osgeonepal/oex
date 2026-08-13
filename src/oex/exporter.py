"""Per-category export loop, shared by Overture and OSM sources."""

import concurrent.futures
import itertools
import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from hdx.data.hdxobject import HDXError

from oex.boundary import resolve_boundary
from oex.config.schema import CategoryConfig, PcodesSourceConfig, RootConfig, dataset_identity
from oex.duckdb_session import connect
from oex.hdx_publisher import (
    HDX_SHORT_SOURCE,
    SOURCE_RANK,
    CombinedCategory,
    ExtraResource,
    HdxPublisher,
    PublishContext,
    category_label,
    combined_title,
    country_name,
)
from oex.logging_setup import get_logger
from oex.metadata import compute_metadata
from oex.pcodes import (
    PcodeCacheEntry,
    ensure_admin_parquets,
    resolve_pcodes_config,
    tag_table,
)
from oex.pcodes.tagger import parse_boundary_resolution
from oex.preflight import check_writable_paths
from oex.report import SourceMetadata, render_report
from oex.s3 import artifact_key, build_layer_key, list_layer_urls
from oex.s3 import resolve as s3_resolve
from oex.s3 import upload as s3_upload
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner
from oex.sql import build_select_clause, build_where_clause, materialise
from oex.state import StateStore
from oex.system import adaptive_parallel_resources, cpu_count
from oex.translit import transliterate_table
from oex.writers import (
    GEOMETRY_LABELS,
    TileLayer,
    build_combined_pmtiles,
    geometry_labels,
    write_format,
    write_geoparquet,
    write_pmtiles,
)
from oex.zip_bundle import make_zip

logger = get_logger(__name__)

_PROJECT_URL = "https://github.com/osgeonepal/oex"
# State slug for the single combined dataset's upload marker.
_COMBINED_SLUG = "__combined__"
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
    """A processed category plus the artifacts phase-B combined publishing needs."""

    result: CategoryResult
    category: CategoryConfig
    zip_paths: list[Path] = field(default_factory=list)
    metadata_json_path: Path | None = None
    metadata_obj: object | None = None
    source_metadata: SourceMetadata | None = None
    geoparquet_path: Path | None = None
    query: SourceQuery | None = None


@dataclass
class ExportResult:
    iso3: str
    source_name: str
    categories: dict[str, CategoryResult] = field(default_factory=dict)
    total_duration_s: float = 0.0

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "skipped")

    @property
    def empty(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "empty")


class Exporter:
    def __init__(self, cfg: RootConfig, runner: SourceRunner):
        self._cfg = cfg
        self._runner = runner
        self._pcodes_cfg: PcodesSourceConfig = resolve_pcodes_config(cfg.source)
        self._pcode_cache: dict[int, PcodeCacheEntry] | None = None
        self._state: StateStore | None = None
        # Spatial join peaks per session; serialise across workers to
        # prevent concurrent peaks from exhausting machine RAM.
        self._pcode_tag_semaphore = threading.Semaphore(1)
        self._adaptive_workers, self._adaptive_mem_gb = adaptive_parallel_resources()

    def run(self) -> ExportResult:
        if not dataset_identity(self._cfg):
            raise ValueError("config.iso3 or config.output.s3.folder is required")
        if not self._cfg.categories:
            raise ValueError("config.categories is empty")

        check_writable_paths(self._cfg)

        iso = self._cfg.iso3.upper() or dataset_identity(self._cfg)
        cat_names = [c.name for c in self._cfg.categories]
        logger.info(
            "[%s/%s] run starting: %d categor%s, formats=%s, parallel=%s, hdx_push=%s",
            iso,
            self._runner.name,
            len(cat_names),
            "y" if len(cat_names) == 1 else "ies",
            self._cfg.output.formats,
            self._cfg.parallel.enabled,
            self._cfg.hdx.push,
        )
        logger.info("[%s/%s] categories: %s", iso, self._runner.name, ", ".join(cat_names))
        _workers = self._cfg.parallel.threads or self._adaptive_workers
        _mem = self._cfg.parallel.memory_gb or self._adaptive_mem_gb
        logger.info(
            "[%s/%s] resources: workers=%d (cfg=%s adaptive=%d) memory_gb=%d (cfg=%s adaptive=%d)",
            iso,
            self._runner.name,
            _workers,
            self._cfg.parallel.threads,
            self._adaptive_workers,
            _mem,
            self._cfg.parallel.memory_gb,
            self._adaptive_mem_gb,
        )

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
            if self._cfg.hdx.combine:
                already_done = bool(peeked) and self._state.is_uploaded(
                    _COMBINED_SLUG, snapshot_label=peeked
                )
            else:
                already_done = bool(peeked) and all(
                    self._state.is_uploaded(_slugify(c.name), snapshot_label=peeked)
                    for c in self._cfg.categories
                )
            if already_done:
                logger.info(
                    "[%s/%s] resume: already uploaded for snapshot %s; "
                    "skipping boundary fetch, pcode cache, and per-category work",
                    iso,
                    self._runner.name,
                    peeked,
                )
                return ExportResult(iso3=iso, source_name=self._runner.name)

        boundary = resolve_boundary(self._cfg.iso3, self._cfg.boundary)
        bbox = boundary.bbox
        logger.info(
            "[%s/%s] boundary: %s bbox=(%.4f, %.4f, %.4f, %.4f)",
            iso,
            self._runner.name,
            boundary.source,
            bbox[0],
            bbox[1],
            bbox[2],
            bbox[3],
        )

        self._runner.prepare(self._cfg)

        # Serialise the download here so parallel-category threads share one cache.
        if self._pcodes_cfg.enabled:
            logger.info(
                "[%s/%s] pcodes: preparing fieldmaps cache (levels=%s, dir=%s)",
                iso,
                self._runner.name,
                self._pcodes_cfg.levels,
                self._pcodes_cfg.cache_dir,
            )
            self._pcode_cache = ensure_admin_parquets(
                cache_dir=Path(self._pcodes_cfg.cache_dir),
                levels=self._pcodes_cfg.levels,
                manifest_url=self._pcodes_cfg.manifest_url,
                parquet_url_template=self._pcodes_cfg.parquet_url_template,
                manifest_group=self._pcodes_cfg.manifest_group,
            )

        publisher: HdxPublisher | None = None
        if self._cfg.hdx.push:
            publisher = HdxPublisher(self._cfg.hdx)

        # Layer staging uploads on output.s3.enabled alone, independently of hdx.push.
        if self._cfg.output.s3.enabled:
            from oex.s3 import preflight as s3_preflight

            logger.info("[%s/%s] s3: preflight check", iso, self._runner.name)
            s3_preflight(self._cfg.output.s3)

        result = ExportResult(iso3=iso, source_name=self._runner.name)
        start = time.time()

        built_list: list[BuiltCategory] = []
        _resolved_workers = self._cfg.parallel.threads or self._adaptive_workers
        if self._cfg.parallel.enabled and len(self._cfg.categories) > 1 and _resolved_workers > 1:
            workers = min(_resolved_workers, len(self._cfg.categories))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._process_category, category, boundary, out_root, publisher
                    ): category
                    for category in self._cfg.categories
                }
                for fut in concurrent.futures.as_completed(futures):
                    built = fut.result()
                    built_list.append(built)
                    result.categories[built.result.name] = built.result
        else:
            for category in self._cfg.categories:
                built = self._process_category(category, boundary, out_root, publisher)
                built_list.append(built)
                result.categories[built.result.name] = built.result

        if self._cfg.hdx.combine:
            self._finish_combined(
                built_list, out_root, publisher, peeked_label=peeked, boundary_bbox=bbox
            )

        result.total_duration_s = time.time() - start
        logger.info(
            "[%s] %s done in %.1fs: %d ok, %d empty, %d skipped, %d failed",
            result.iso3,
            self._runner.name,
            result.total_duration_s,
            result.succeeded,
            result.empty,
            result.skipped,
            result.failed,
        )
        return result

    def _process_category(
        self,
        category: CategoryConfig,
        boundary: object,
        out_root: Path,
        publisher: HdxPublisher | None,
    ) -> BuiltCategory:
        # Combined mode defers publishing to a single phase-B call, so no
        # category publishes on its own and artifacts are kept until then.
        combine = self._cfg.hdx.combine
        cat_start = time.time()
        slug = _slugify(category.name)
        cat_tag = f"[{category.name}/{self._runner.name}]"
        logger.info("%s starting", cat_tag)

        def _early(result: CategoryResult) -> BuiltCategory:
            return BuiltCategory(result=result, category=category)

        try:
            query = self._runner.query_for(self._cfg, category)
        except CategorySkippedError as skip:
            logger.info("%s skipped: %s", cat_tag, skip)
            return _early(
                CategoryResult(
                    name=category.name,
                    status="skipped",
                    duration_s=time.time() - cat_start,
                    error=str(skip),
                )
            )

        formats = category.formats or self._cfg.output.formats
        if not formats:
            logger.info("%s skipped: no output formats configured", cat_tag)
            return _early(
                CategoryResult(
                    name=category.name,
                    status="skipped",
                    duration_s=time.time() - cat_start,
                    error="no output formats configured",
                )
            )
        # GeoParquet is a local/S3 deliverable and the combined-tile source; it is
        # never zipped or attached to the HDX page, so keep it out of the HDX formats.
        want_geoparquet = "geoparquet" in formats or (combine and self._cfg.output.pmtiles.enabled)
        hdx_formats = [f for f in formats if f != "geoparquet"]

        if self._cfg.output.resume and self._state is not None and not combine:
            entry = self._state.get(slug)
            snapshot_label = query.snapshot_label or "unknown"
            if entry and self._state.is_uploaded(slug, snapshot_label=snapshot_label):
                logger.info(
                    "%s resume: already built and uploaded (%s); skipping",
                    cat_tag,
                    entry.uploaded_utc,
                )
                return _early(
                    CategoryResult(
                        name=category.name,
                        status="ok",
                        feature_count=0,
                        duration_s=time.time() - cat_start,
                        zip_paths=[Path(p) for p in entry.zip_paths],
                        hdx_dataset=entry.hdx_dataset,
                    )
                )
            if (
                entry
                and self._state.is_built(slug, snapshot_label=snapshot_label)
                and publisher is not None
            ):
                logger.info(
                    "%s resume: already built (%s); attempting upload only",
                    cat_tag,
                    entry.built_utc,
                )
                try:
                    return _early(
                        self._upload_only(
                            category=category,
                            cat_start=cat_start,
                            cat_tag=cat_tag,
                            entry=entry,
                            publisher=publisher,
                            query=query,
                            out_root=out_root,
                            slug=slug,
                        )
                    )
                except Exception:
                    logger.exception("%s resume upload failed; rebuilding", cat_tag)
                    self._state.reset(slug)

        logger.info(
            "%s source: %s | snapshot: %s",
            cat_tag,
            query.dataset_source,
            query.snapshot_label,
        )

        from typing import cast

        from oex.boundary import Boundary

        boundary_obj = cast(Boundary, boundary)

        d = self._cfg.duckdb
        _parallel_workers = max(1, self._cfg.parallel.threads or self._adaptive_workers)
        _duckdb_threads = max(2, cpu_count() // _parallel_workers)
        table = f"{slug}_{int(time.time() * 1000)}"
        db_path = Path(d.temp_dir) / f"{table}.duckdb"
        conn = connect(
            path=db_path,
            threads=_duckdb_threads,
            memory_gb=self._cfg.parallel.memory_gb or self._adaptive_mem_gb,
            s3_region=getattr(
                self._cfg.source.get("overture"),
                "s3_region",
                "us-west-2",
            ),
            temp_dir=d.temp_dir,
            http_retries=d.http_retries,
            http_retry_wait_ms=d.http_retry_wait_ms,
            http_retry_backoff=d.http_retry_backoff,
            http_timeout_ms=d.http_timeout_ms,
            anonymous_s3_bucket=getattr(
                self._cfg.source.get("overture"), "s3_bucket", "overturemaps-us-west-2"
            ),
        )
        try:
            select_clause = build_select_clause(query.select_fields)
            where_clause = build_where_clause(boundary_obj, query.where_conditions, query.bbox_cols)
            logger.info("%s querying source...", cat_tag)
            mat_start = time.time()
            count = materialise(conn, table, query.source_expr, select_clause, where_clause)
            logger.info(
                "%s queried %s features in %.1fs",
                cat_tag,
                f"{count:,}",
                time.time() - mat_start,
            )
            if count == 0:
                logger.info("%s empty: no features within boundary", cat_tag)
                if publisher is not None and not self._cfg.hdx.combine:
                    self._refresh_empty_category(category, query, publisher, cat_tag)
                return _early(
                    CategoryResult(
                        name=category.name,
                        status="empty",
                        feature_count=0,
                        duration_s=time.time() - cat_start,
                    )
                )

            if self._pcode_cache is not None and not category.skip_pcodes:
                tag_start = time.time()
                logger.info(
                    "%s tagging with pcodes (levels=%s)...",
                    cat_tag,
                    self._pcodes_cfg.levels,
                )
                boundary_resolution = parse_boundary_resolution(
                    category.boundary_resolution or self._pcodes_cfg.boundary_resolution
                )
                with self._pcode_tag_semaphore:
                    tag_table(
                        conn,
                        table=table,
                        iso3=self._cfg.iso3,
                        cache_entries=self._pcode_cache,
                        levels=self._pcodes_cfg.levels,
                        geom_column="geom",
                        boundary_resolution=boundary_resolution,
                    )
                logger.info("%s pcodes tagged in %.1fs", cat_tag, time.time() - tag_start)
            elif self._pcode_cache is not None and category.skip_pcodes:
                logger.info("%s pcodes skipped (skip_pcodes=true)", cat_tag)

            if category.transliterate:
                translit_start = time.time()
                logger.info(
                    "%s transliterating %d column(s)...",
                    cat_tag,
                    len(category.transliterate),
                )
                transliterate_table(conn, table=table, rules=category.transliterate)
                logger.info(
                    "%s transliteration done in %.1fs",
                    cat_tag,
                    time.time() - translit_start,
                )

            need_metadata = self._cfg.output.metadata or self._cfg.output.report.enabled
            metadata_obj = None
            metadata_dict = None
            if need_metadata:
                logger.info("%s computing metadata...", cat_tag)
                metadata_obj = compute_metadata(
                    conn,
                    table,
                    temporal_column=category.temporal.column,
                )
                metadata_dict = metadata_obj.to_dict()

            dt_name = f"{self._cfg.key}_{self._cfg.iso3.lower()}_{slug}"
            metadata_json_path: Path | None = None
            source_metadata: SourceMetadata | None = None
            if self._cfg.output.report.enabled and metadata_obj is not None:
                source_metadata = SourceMetadata(
                    source_name=self._runner.name,
                    snapshot_label=query.snapshot_label,
                    dataset_source=query.dataset_source,
                    generated_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    oex_version=_oex_version(),
                    license_label=category.hdx.license,
                    license_url=category.hdx.license_url,
                    pcode_source_date=(
                        next(iter(self._pcode_cache.values())).upstream_date
                        if self._pcode_cache
                        else None
                    ),
                    boundary=boundary_obj.source,
                    metadata=metadata_obj,
                )
                metadata_json_path = out_root / f"{dt_name}_{self._runner.name}_metadata.json"
                metadata_json_path.write_text(
                    json.dumps(source_metadata.to_payload(), indent=2),
                    encoding="utf-8",
                )

            conn.execute("PRAGMA memory_limit='2GB'")
            logger.info("%s writing %d format(s): %s", cat_tag, len(hdx_formats), hdx_formats)
            zip_paths = self._materialise_outputs(
                conn=conn,
                table=table,
                slug=slug,
                category=category,
                query=query,
                formats=hdx_formats,
                out_root=out_root,
                metadata_report=metadata_dict,
                boundary=boundary_obj,
                feature_count=count,
            )

            if not zip_paths and not want_geoparquet:
                # Every requested format failed (corrupt OSM coords, OOM, etc).
                # Don't try to publish nothing; mark the category as skipped so
                # the country doesn't go red if other categories are healthy.
                logger.warning(
                    "%s no formats succeeded; skipping HDX upload for this category",
                    cat_tag,
                )
                return _early(
                    CategoryResult(
                        name=category.name,
                        status="skipped",
                        feature_count=count,
                        duration_s=time.time() - cat_start,
                        error="no formats succeeded",
                    )
                )

            total_mb = sum(p.stat().st_size for p in zip_paths) / (1024 * 1024)

            geoparquet_path: Path | None = None
            if want_geoparquet:
                geoparquet_path = write_geoparquet(
                    conn, table, out_root / "_layers" / f"{slug}.parquet"
                )
                self._stage_layer_parquet(geoparquet_path, slug, cat_tag)

            pmtiles_extra: list[ExtraResource] = []
            pmtiles_path: Path | None = None
            if self._cfg.output.pmtiles.enabled and not combine:
                pm = self._cfg.output.pmtiles
                pmtiles_path = out_root / f"{dt_name}_{self._runner.name}.pmtiles"
                write_pmtiles(conn, table, pmtiles_path, min_zoom=pm.min_zoom, max_zoom=pm.max_zoom)
                pmtiles_extra.append(
                    ExtraResource(
                        path=pmtiles_path,
                        fmt="pmtiles",
                        description=f"{category.name} vector tiles (PMTiles)",
                    )
                )

            if source_metadata is not None:
                self._write_local_report(
                    category=category,
                    source_metadata=source_metadata,
                    report_path=out_root / f"{dt_name}_{self._runner.name}_report.html",
                    pmtiles_path=pmtiles_path,
                    boundary_bbox=boundary_obj.bbox,
                    cat_tag=cat_tag,
                )

            if self._state is not None:
                self._state.mark_built(
                    slug,
                    snapshot_label=query.snapshot_label or "unknown",
                    zip_paths=zip_paths,
                    metadata_json_path=metadata_json_path,
                )

            dataset_name: str | None = None
            if publisher is not None and not combine:
                logger.info("%s uploading %d zip(s) to HDX...", cat_tag, len(zip_paths))
                t_min, t_max = _temporal_bounds_for_hdx(metadata_obj)
                ctx = PublishContext(
                    dataset_source=query.dataset_source,
                    snapshot_date=query.snapshot_date,
                    source_name=self._runner.name,
                    metadata_json_path=metadata_json_path,
                    combined_report_enabled=self._cfg.output.report.enabled,
                    output_dir=out_root,
                    s3=self._cfg.output.s3,
                    temporal_min=t_min,
                    temporal_max=t_max,
                    boundary_bbox=boundary_obj.bbox,
                )
                dataset_name = publisher.publish(
                    self._cfg, category, zip_paths, ctx, extra_resources=pmtiles_extra
                )
                if self._state is not None:
                    self._state.mark_uploaded(slug, hdx_dataset=dataset_name)
                if self._cfg.output.remove_after_upload:
                    extra_paths = [e.path for e in pmtiles_extra]
                    _remove_uploaded_outputs(zip_paths, metadata_json_path, extra_paths)
                    logger.info(
                        "%s removed %d local output(s) after upload", cat_tag, len(zip_paths)
                    )
            elif publisher is None and not combine and self._cfg.output.s3.enabled:
                self._upload_category_to_s3(zip_paths, slug, cat_tag)

            logger.info(
                "%s done: %s features, %d zip(s), %.0f MB total in %.1fs",
                cat_tag,
                f"{count:,}",
                len(zip_paths),
                total_mb,
                time.time() - cat_start,
            )
            return BuiltCategory(
                result=CategoryResult(
                    name=category.name,
                    status="ok",
                    feature_count=count,
                    duration_s=time.time() - cat_start,
                    zip_paths=zip_paths,
                    hdx_dataset=dataset_name,
                ),
                category=category,
                zip_paths=zip_paths,
                metadata_json_path=metadata_json_path,
                metadata_obj=metadata_obj,
                source_metadata=source_metadata,
                geoparquet_path=geoparquet_path,
                query=query,
            )
        except Exception as exc:  # noqa: BLE001  per-category boundary; logged + reported
            logger.exception("%s failed", cat_tag)
            return _early(
                CategoryResult(
                    name=category.name,
                    status="failed",
                    duration_s=time.time() - cat_start,
                    error=str(exc),
                )
            )
        finally:
            conn.close()
            db_path.unlink(missing_ok=True)

    def _write_local_report(
        self,
        *,
        category: CategoryConfig,
        source_metadata: SourceMetadata,
        report_path: Path,
        pmtiles_path: Path | None,
        boundary_bbox: tuple[float, float, float, float],
        cat_tag: str,
    ) -> None:
        """Write this source's report next to its outputs, mapping the local tileset."""
        # A relative URL keeps the page working under any host serving the output directory.
        tilesets = (
            {self._runner.name: (pmtiles_path.name, pmtiles_path.stem)}
            if pmtiles_path is not None
            else None
        )
        report_path.write_text(
            render_report(
                {self._runner.name: source_metadata},
                tilesets,
                boundary_bbox,
                category_label(category),
                self._cfg.output.report.palette,
                self._cfg.output.report.map_assets,
            ),
            encoding="utf-8",
        )
        logger.info("%s wrote local report -> %s", cat_tag, report_path)

    def _finish_combined(
        self,
        built_list: list[BuiltCategory],
        out_root: Path,
        publisher: HdxPublisher | None,
        *,
        peeked_label: str | None,
        boundary_bbox: tuple[float, float, float, float],
    ) -> None:
        """Build the combined artifacts, then publish them when HDX push is on."""
        iso = self._cfg.iso3.upper()
        built_ok = [b for b in built_list if b.result.status == "ok" and b.zip_paths]
        if not built_ok:
            logger.warning("[%s/%s] combine: no successful categories", iso, self._runner.name)
            return

        order = {c.name: i for i, c in enumerate(self._cfg.categories)}
        built_ok.sort(key=lambda b: order.get(b.category.name, len(order)))

        dt_name = self._cfg.hdx.combined.name or f"{self._cfg.key}_{self._cfg.iso3.lower()}"
        pmtiles_path = self._build_combined_tiles(built_ok, out_root, dt_name)

        if publisher is None:
            self._write_local_landing(
                built_ok,
                out_root,
                dt_name=dt_name,
                pmtiles_path=pmtiles_path,
                boundary_bbox=boundary_bbox,
            )
            return

        self._publish_combined(
            built_ok,
            out_root,
            publisher,
            dt_name=dt_name,
            pmtiles_path=pmtiles_path,
            peeked_label=peeked_label,
            boundary_bbox=boundary_bbox,
        )

    def _write_local_landing(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        *,
        dt_name: str,
        pmtiles_path: Path | None,
        boundary_bbox: tuple[float, float, float, float],
    ) -> None:
        """Render the combined overview from this run's own layers, with no HDX round-trip.

        The published page also covers layers an earlier run of the other source
        left on the dataset; only HDX knows about those.
        """
        if not self._cfg.output.report.enabled:
            return
        from oex.report.landing import CategoryPanel, render_landing, source_label

        panels = [
            CategoryPanel(
                slug=_slugify(b.category.name),
                label=category_label(b.category),
                sources=[b.source_metadata],
            )
            for b in built_ok
            if b.source_metadata is not None
        ]
        if not panels:
            return

        place = country_name(self._cfg.iso3, dataset_name=self._cfg.dataset_name)
        q0 = built_ok[0].query
        hdx_source = HDX_SHORT_SOURCE.get(self._runner.name) or (q0.dataset_source if q0 else "")
        html = render_landing(
            title=combined_title(self._cfg, place, hdx_source),
            subtitle=f"{len(panels)} layers from {source_label(self._runner.name)} for {place}",
            panels=panels,
            pmtiles_url=pmtiles_path.name if pmtiles_path else None,
            pmtiles_layer=pmtiles_path.stem if pmtiles_path else None,
            boundary_bbox=boundary_bbox,
            palette=self._cfg.output.report.palette,
            map_assets=self._cfg.output.report.map_assets,
        )
        landing_path = out_root / f"{dt_name}_overview.html"
        landing_path.write_text(html, encoding="utf-8")
        logger.info(
            "[%s/%s] combine: wrote local overview (%d layers) -> %s",
            self._cfg.iso3.upper(),
            self._runner.name,
            len(panels),
            landing_path,
        )

    def _publish_combined(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        publisher: HdxPublisher,
        *,
        dt_name: str,
        pmtiles_path: Path | None,
        peeked_label: str | None,
        boundary_bbox: tuple[float, float, float, float],
    ) -> None:
        iso = self._cfg.iso3.upper()
        entries = [
            CombinedCategory(
                category=b.category,
                zip_paths=b.zip_paths,
                metadata_json_path=b.metadata_json_path,
            )
            for b in built_ok
        ]
        metadata_path = self._write_combined_metadata(built_ok, out_root, dt_name)
        t_min, t_max = _combined_temporal_bounds([b.metadata_obj for b in built_ok])
        q0 = built_ok[0].query
        assert q0 is not None
        ctx = PublishContext(
            dataset_source=q0.dataset_source,
            snapshot_date=q0.snapshot_date,
            source_name=self._runner.name,
            output_dir=out_root,
            s3=self._cfg.output.s3,
            temporal_min=t_min,
            temporal_max=t_max,
            boundary_bbox=boundary_bbox,
        )
        logger.info(
            "[%s/%s] combine: publishing %d categories as single dataset %s",
            iso,
            self._runner.name,
            len(entries),
            dt_name,
        )
        dataset_name = publisher.publish_combined(
            self._cfg,
            entries,
            ctx,
            pmtiles_path=pmtiles_path,
            metadata_path=metadata_path,
            landing_enabled=self._cfg.output.report.enabled,
        )

        if self._state is not None:
            snap = peeked_label or (q0.snapshot_label or "unknown")
            self._state.mark_built(
                _COMBINED_SLUG, snapshot_label=snap, zip_paths=[], metadata_json_path=None
            )
            self._state.mark_uploaded(_COMBINED_SLUG, hdx_dataset=dataset_name)

        for b in built_ok:
            b.result.hdx_dataset = dataset_name

        if self._cfg.output.remove_after_upload:
            # Keep the per-layer GeoParquets: they are a deliverable and the
            # source of truth for future combined-tile rebuilds.
            for b in built_ok:
                _remove_uploaded_outputs(b.zip_paths, b.metadata_json_path, None)
            if pmtiles_path is not None:
                pmtiles_path.unlink(missing_ok=True)
            if metadata_path is not None:
                metadata_path.unlink(missing_ok=True)
            logger.info(
                "[%s/%s] combine: removed local outputs after upload", iso, self._runner.name
            )

    def _write_combined_metadata(
        self, built_ok: list[BuiltCategory], out_root: Path, dt_name: str
    ) -> Path | None:
        """One dataset-level metadata file covering every layer."""
        layers = [
            {"category": b.category.name, **b.source_metadata.to_payload()}
            for b in built_ok
            if b.source_metadata is not None
        ]
        if not layers:
            return None
        path = out_root / f"{dt_name}_metadata.json"
        path.write_text(
            json.dumps({"dataset": dt_name, "layers": layers}, indent=2), encoding="utf-8"
        )
        return path

    def _build_combined_tiles(
        self, built_ok: list[BuiltCategory], out_root: Path, dt_name: str
    ) -> Path | None:
        """Merge this run's GeoParquets plus other sources' staged GeoParquets into one tileset."""
        if not self._cfg.output.pmtiles.enabled:
            return None
        order = {_slugify(c.name): i for i, c in enumerate(self._cfg.categories)}
        by_slug = {_slugify(c.name): c for c in self._cfg.categories}

        def tiles_on(slug: str, source: str) -> bool:
            block = getattr(by_slug.get(slug), source, None)
            return getattr(block, "tiles", True) if block is not None else True

        layers = [
            TileLayer(
                path=str(b.geoparquet_path),
                category=_slugify(b.category.name),
                source=self._runner.name,
            )
            for b in built_ok
            if b.geoparquet_path is not None
            and tiles_on(_slugify(b.category.name), self._runner.name)
        ]
        if self._cfg.output.s3.enabled:
            for source, slug, url in list_layer_urls(
                self._cfg.output.s3, self._cfg.iso3, exclude_source=self._runner.name
            ):
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
            key=lambda ly: (SOURCE_RANK.get(ly.source, 99), order.get(ly.category, len(order)))
        )

        pm = self._cfg.output.pmtiles
        pmtiles_path = out_root / f"{dt_name}.pmtiles"
        d = self._cfg.duckdb
        tiles_db = Path(d.temp_dir) / f"{dt_name}_tiles_{int(time.time() * 1000)}.duckdb"
        conn = connect(
            path=tiles_db,
            threads=max(2, cpu_count()),
            memory_gb=self._cfg.parallel.memory_gb or self._adaptive_mem_gb,
            s3_region=getattr(self._cfg.source.get("overture"), "s3_region", "us-west-2"),
            temp_dir=d.temp_dir,
            http_retries=d.http_retries,
            http_retry_wait_ms=d.http_retry_wait_ms,
            http_retry_backoff=d.http_retry_backoff,
            http_timeout_ms=d.http_timeout_ms,
            anonymous_s3_bucket=getattr(
                self._cfg.source.get("overture"), "s3_bucket", "overturemaps-us-west-2"
            ),
        )
        try:
            build_combined_pmtiles(
                conn, layers, pmtiles_path, min_zoom=pm.min_zoom, max_zoom=pm.max_zoom
            )
            sources = sorted({ly.source for ly in layers})
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

    def _resource_prefix(self) -> str:
        """Root for resource filenames: the combined dataset name when combining,
        otherwise the per-category ``{key}_{iso3}`` (or the S3 folder id)."""
        if self._cfg.hdx.combine:
            return self._cfg.hdx.combined.name or f"{self._cfg.key}_{self._cfg.iso3.lower()}"
        ident = self._cfg.output.s3.folder or self._cfg.iso3.lower()
        # An empty key means the folder id is already the whole prefix (hotosm_project_1).
        return f"{self._cfg.key}_{ident}" if self._cfg.key else ident

    def _artifact_geometry(self, zip_path: Path) -> str:
        """The geometry label this artifact holds, read back off the name we wrote."""
        if not self._cfg.output.split_by_geometry:
            raise ValueError(
                "output.s3.nest_by_geometry needs output.split_by_geometry; without it "
                "one artifact holds every geometry type and cannot sit under one segment"
            )
        tokens = zip_path.stem.split("_")
        for label in GEOMETRY_LABELS:
            if label in tokens:
                return label
        raise ValueError(f"no geometry label in artifact name {zip_path.name}")

    def _upload_category_to_s3(self, zip_paths: list[Path], slug: str, cat_tag: str) -> None:
        """Upload a category's zips straight to S3 when s3 is enabled and HDX is off."""
        s3cfg = self._cfg.output.s3
        bucket, prefix, region, endpoint_url, acl = s3_resolve(s3cfg)
        logger.info("%s uploading %d zip(s) to S3...", cat_tag, len(zip_paths))
        for zp in zip_paths:
            key = artifact_key(
                prefix,
                self._cfg.iso3,
                slug,
                zp.name,
                folder=s3cfg.folder,
                nest_by_category=s3cfg.nest_by_category,
                geometry=self._artifact_geometry(zp) if s3cfg.nest_by_geometry else "",
            )
            s3_upload(zp, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl)
        if self._state is not None:
            self._state.mark_uploaded(slug, hdx_dataset=None)
        if self._cfg.output.remove_after_upload:
            _remove_uploaded_outputs(zip_paths, None)
            logger.info("%s removed %d local output(s) after upload", cat_tag, len(zip_paths))

    def _stage_layer_parquet(self, path: Path, slug: str, cat_tag: str) -> None:
        """Upload a per-layer GeoParquet to the stable S3 key so runs accumulate across sources."""
        s3cfg = self._cfg.output.s3
        if not s3cfg.enabled:
            return
        bucket, prefix, region, endpoint_url, acl = s3_resolve(s3cfg)
        if not bucket:
            raise ValueError(
                "output.s3.enabled is true but no bucket given via output.s3.bucket or OEX_S3_BUCKET"
            )
        key = build_layer_key(prefix, self._cfg.iso3, self._runner.name, slug)
        url = s3_upload(
            path, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl
        )
        logger.info("%s staged layer geoparquet -> %s", cat_tag, url)

    def _refresh_empty_category(
        self,
        category: CategoryConfig,
        query: SourceQuery,
        publisher: HdxPublisher,
        cat_tag: str,
    ) -> None:
        """Restate the snapshot date on an empty category's dataset.

        HDXError leaves the older date in place, which costs nothing an empty category
        was going to publish, so it is logged instead of failing the export.
        """
        try:
            publisher.refresh_metadata(
                self._cfg,
                category,
                PublishContext(
                    dataset_source=query.dataset_source,
                    snapshot_date=query.snapshot_date,
                    source_name=self._runner.name,
                    s3=self._cfg.output.s3,
                ),
            )
        except HDXError:
            logger.exception("%s metadata refresh failed", cat_tag)

    def _upload_only(
        self,
        *,
        category: CategoryConfig,
        cat_start: float,
        cat_tag: str,
        entry,  # CategoryState
        publisher: HdxPublisher,
        query: SourceQuery,
        out_root: Path,
        slug: str,
    ) -> CategoryResult:
        zip_paths = [Path(p) for p in entry.zip_paths]
        metadata_json_path = Path(entry.metadata_json_path) if entry.metadata_json_path else None
        logger.info("%s uploading %d cached zip(s) to HDX...", cat_tag, len(zip_paths))
        t_min, t_max = _temporal_bounds_from_metadata_file(metadata_json_path)
        ctx = PublishContext(
            dataset_source=query.dataset_source,
            snapshot_date=query.snapshot_date,
            source_name=self._runner.name,
            metadata_json_path=metadata_json_path,
            combined_report_enabled=self._cfg.output.report.enabled,
            output_dir=out_root,
            s3=self._cfg.output.s3,
            temporal_min=t_min,
            temporal_max=t_max,
        )
        dataset_name = publisher.publish(self._cfg, category, zip_paths, ctx)
        if self._state is not None:
            self._state.mark_uploaded(slug, hdx_dataset=dataset_name)
        if self._cfg.output.remove_after_upload:
            _remove_uploaded_outputs(zip_paths, metadata_json_path)
            logger.info("%s removed %d local output(s) after upload", cat_tag, len(zip_paths))
        return CategoryResult(
            name=category.name,
            status="ok",
            feature_count=0,
            duration_s=time.time() - cat_start,
            zip_paths=zip_paths,
            hdx_dataset=dataset_name,
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

        for fmt, (geometry, source_table) in itertools.product(formats, partitions):
            geom_seg = f"_{geometry}" if geometry else ""
            stage_dir = out_root / f"_stage_{slug}{geom_seg}_{fmt}"
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            stage_dir.mkdir(parents=True)
            try:
                try:
                    files = write_format(conn, source_table, slug, fmt, stage_dir)
                except Exception as exc:  # noqa: BLE001 - per-format boundary
                    # GDAL's COPY can fail mid-write on bogus OSM coords or
                    # blow past memory_limit on huge tables. Skip this format
                    # rather than killing the whole category; the others may
                    # still succeed.
                    logger.warning(
                        "%s [%s] format write failed (%s); skipping this format",
                        f"[{category.name}/{self._runner.name}]",
                        fmt,
                        exc,
                    )
                    continue
                if not files:
                    continue
                # HDX's filestore does not recognise FlatGeobuf, so we skip
                # zipping and publishing it. The format stays usable for any
                # downstream consumer that reads the raw file off disk before
                # the stage dir is cleaned.
                if fmt == "fgb":
                    continue
                s3cfg = self._cfg.output.s3
                source_seg = f"_{self._runner.name}" if s3cfg.name_include_source else ""
                zip_path = (
                    out_root / f"{self._resource_prefix()}_{slug}{geom_seg}{source_seg}_{fmt}.zip"
                )
                readme_lines = self._build_readme(
                    fmt=fmt,
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
                if stage_dir.exists():
                    shutil.rmtree(stage_dir, ignore_errors=True)
        return zip_paths

    def _geometry_view(self, conn, table: str, label: str, types: list[str]) -> str:
        """A view over one geometry label, so each artifact holds a single type."""
        view = f"{table}_{label}"
        in_list = ", ".join(f"'{t}'" for t in types)
        conn.execute(
            f"CREATE OR REPLACE TEMP VIEW {view} AS "
            f"SELECT * FROM {table} WHERE ST_GeometryType(geom) IN ({in_list})"
        )
        return view

    def _build_readme(
        self,
        *,
        fmt: str,
        category: CategoryConfig,
        query: SourceQuery,
        boundary,
        feature_count: int,
    ) -> list[str]:
        bbox = ", ".join(f"{x:.4f}" for x in boundary.bbox)
        license_url = category.hdx.license_url or "(not specified)"
        wrapped_desc = _wrap_paragraph(query.source_description, indent="  ", width=78)
        format_notes = _format_notes(fmt)
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
            f"Format:           {_FORMAT_LABELS.get(fmt, fmt)}",
            f"Features:         {feature_count:,}",
            "",
            f"Source:           {category.hdx.dataset_source or query.dataset_source}",
            f"Source URL:       {query.source_url}",
            f"Snapshot:         {query.snapshot_label}",
            f"License:          {category.hdx.license}",
            f"License URL:      {license_url}",
            "",
            "About the source",
            *wrapped_desc,
            "",
            *(["Notes", *format_notes, ""] if format_notes else []),
            f"Feedback:         {_PROJECT_URL}/issues",
        ] + ([line for line in query.extra_readme_lines] if query.extra_readme_lines else [])


def _remove_uploaded_outputs(
    zip_paths: list[Path],
    metadata_json_path: Path | None,
    extra_paths: list[Path] | None = None,
) -> None:
    """Delete local artifacts once HDX confirms their upload."""
    for path in zip_paths:
        path.unlink(missing_ok=True)
    if metadata_json_path is not None:
        metadata_json_path.unlink(missing_ok=True)
    for path in extra_paths or []:
        path.unlink(missing_ok=True)


def _slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).lower().strip("_")


def _wrap_paragraph(text: str, *, indent: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _parse_temporal(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning(
            "temporal bound %r is not ISO-8601; HDX period falls back to snapshot", value
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _temporal_bounds_for_hdx(metadata_obj) -> tuple[datetime | None, datetime | None]:
    if metadata_obj is None or metadata_obj.temporal is None:
        return (None, None)
    t = metadata_obj.temporal
    return (_parse_temporal(t.min), _parse_temporal(t.max))


def _combined_temporal_bounds(
    metadata_objs: list[object],
) -> tuple[datetime | None, datetime | None]:
    mins: list[datetime] = []
    maxs: list[datetime] = []
    for obj in metadata_objs:
        lo, hi = _temporal_bounds_for_hdx(obj)
        if lo is not None:
            mins.append(lo)
        if hi is not None:
            maxs.append(hi)
    return (min(mins) if mins else None, max(maxs) if maxs else None)


def _temporal_bounds_from_metadata_file(
    path: Path | None,
) -> tuple[datetime | None, datetime | None]:
    if path is None or not path.exists():
        return (None, None)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (None, None)
    temporal = payload.get("metadata", {}).get("temporal")
    if not temporal:
        return (None, None)
    return (_parse_temporal(temporal.get("min")), _parse_temporal(temporal.get("max")))


def _format_notes(fmt: str) -> list[str]:
    if fmt == "shp":
        return [
            "  - Shapefile output is split by geometry type:",
            "    <category>_polygons.shp, <category>_lines.shp, <category>_points.shp.",
            "    This is a shapefile-format limitation, not a data limitation.",
            "  - Field names are truncated to 10 characters in shp; gpkg keeps them full.",
        ]
    if fmt == "gpkg":
        return [
            "  - GeoPackage holds all geometry types in a single .gpkg file.",
            "  - Recommended for QGIS, ArcGIS, GDAL/OGR.",
        ]
    if fmt == "geojson":
        return [
            "  - GeoJSON is a single-file text format. Consider gpkg for very large layers.",
        ]
    if fmt == "kml":
        return [
            "  - KML opens directly in Google Earth and most desktop GIS.",
            "  - Single XML file; can grow large for big layers. Prefer gpkg above ~1M features.",
        ]
    return []
