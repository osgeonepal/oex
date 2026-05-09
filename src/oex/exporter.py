"""Per-category export loop, shared by Overture and OSM sources."""

import concurrent.futures
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from oex.boundary import resolve_boundary
from oex.config.schema import CategoryConfig, PcodesSourceConfig, RootConfig
from oex.duckdb_session import connect
from oex.hdx_publisher import HdxPublisher, PublishContext
from oex.logging_setup import get_logger
from oex.metadata import compute_metadata
from oex.pcodes import (
    PcodeCacheEntry,
    ensure_admin_parquets,
    resolve_pcodes_config,
    tag_table,
)
from oex.report import SourceMetadata, render_report
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner
from oex.sql import build_select_clause, build_where_clause, materialise
from oex.system import default_thread_count
from oex.translit import transliterate_table
from oex.writers import write_format
from oex.zip_bundle import make_zip

logger = get_logger(__name__)

_PROJECT_URL = "https://github.com/osgeonepal/oex"
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

    def run(self) -> ExportResult:
        if not self._cfg.iso3:
            raise ValueError("config.iso3 is required")
        if not self._cfg.categories:
            raise ValueError("config.categories is empty")

        iso = self._cfg.iso3.upper()
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
            if self._cfg.output.s3.enabled:
                from oex.s3 import preflight as s3_preflight

                logger.info("[%s/%s] s3: preflight check", iso, self._runner.name)
                s3_preflight(self._cfg.output.s3)

        out_root = Path(self._cfg.output.dir) / self._cfg.iso3.lower() / self._runner.name
        out_root.mkdir(parents=True, exist_ok=True)

        result = ExportResult(iso3=iso, source_name=self._runner.name)
        start = time.time()

        if self._cfg.parallel.enabled and len(self._cfg.categories) > 1:
            workers = min(
                self._cfg.parallel.threads or default_thread_count(),
                len(self._cfg.categories),
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._process_category, category, boundary, out_root, publisher
                    ): category
                    for category in self._cfg.categories
                }
                for fut in concurrent.futures.as_completed(futures):
                    cat_result = fut.result()
                    result.categories[cat_result.name] = cat_result
        else:
            for category in self._cfg.categories:
                cat_result = self._process_category(category, boundary, out_root, publisher)
                result.categories[cat_result.name] = cat_result

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
    ) -> CategoryResult:
        cat_start = time.time()
        slug = _slugify(category.name)
        cat_tag = f"[{category.name}/{self._runner.name}]"
        logger.info("%s starting", cat_tag)
        try:
            query = self._runner.query_for(self._cfg, category)
        except CategorySkippedError as skip:
            logger.info("%s skipped: %s", cat_tag, skip)
            return CategoryResult(
                name=category.name,
                status="skipped",
                duration_s=time.time() - cat_start,
                error=str(skip),
            )

        formats = category.formats or self._cfg.output.formats
        if not formats:
            logger.info("%s skipped: no output formats configured", cat_tag)
            return CategoryResult(
                name=category.name,
                status="skipped",
                duration_s=time.time() - cat_start,
                error="no output formats configured",
            )

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
        conn = connect(
            threads=self._cfg.parallel.threads,
            memory_gb=self._cfg.parallel.memory_gb,
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
        )
        try:
            table = f"{slug}_{int(time.time() * 1000)}"
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
                return CategoryResult(
                    name=category.name,
                    status="empty",
                    feature_count=0,
                    duration_s=time.time() - cat_start,
                )

            if self._pcode_cache is not None:
                tag_start = time.time()
                logger.info(
                    "%s tagging with pcodes (levels=%s)...",
                    cat_tag,
                    self._pcodes_cfg.levels,
                )
                tag_table(
                    conn,
                    table=table,
                    iso3=self._cfg.iso3,
                    cache_entries=self._pcode_cache,
                    levels=self._pcodes_cfg.levels,
                    geom_column="geom",
                )
                logger.info("%s pcodes tagged in %.1fs", cat_tag, time.time() - tag_start)

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
                metadata_obj = compute_metadata(conn, table)
                metadata_dict = metadata_obj.to_dict()

            metadata_json_path: Path | None = None
            if self._cfg.output.report.enabled and metadata_obj is not None:
                dt_name = f"{self._cfg.key}_{self._cfg.iso3.lower()}_{slug}"
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

                local_report_path = out_root / f"{dt_name}_{self._runner.name}_report.html"
                local_report_path.write_text(
                    render_report({self._runner.name: source_metadata}),
                    encoding="utf-8",
                )
                logger.info(
                    "%s wrote metadata.json + local report -> %s",
                    cat_tag,
                    out_root,
                )

            logger.info("%s writing %d format(s): %s", cat_tag, len(formats), formats)
            zip_paths = self._materialise_outputs(
                conn=conn,
                table=table,
                slug=slug,
                category=category,
                query=query,
                formats=formats,
                out_root=out_root,
                metadata_report=metadata_dict,
                boundary=boundary_obj,
                feature_count=count,
            )

            total_mb = sum(p.stat().st_size for p in zip_paths) / (1024 * 1024)

            dataset_name: str | None = None
            if publisher is not None:
                logger.info("%s uploading %d zip(s) to HDX...", cat_tag, len(zip_paths))
                ctx = PublishContext(
                    dataset_source=query.dataset_source,
                    snapshot_date=query.snapshot_date,
                    source_name=self._runner.name,
                    metadata_json_path=metadata_json_path,
                    combined_report_enabled=self._cfg.output.report.enabled,
                    output_dir=out_root,
                    s3=self._cfg.output.s3,
                )
                dataset_name = publisher.publish(self._cfg, category, zip_paths, ctx)

            logger.info(
                "%s done: %s features, %d zip(s), %.0f MB total in %.1fs",
                cat_tag,
                f"{count:,}",
                len(zip_paths),
                total_mb,
                time.time() - cat_start,
            )
            return CategoryResult(
                name=category.name,
                status="ok",
                feature_count=count,
                duration_s=time.time() - cat_start,
                zip_paths=zip_paths,
                hdx_dataset=dataset_name,
            )
        except Exception as exc:  # noqa: BLE001  per-category boundary; logged + reported
            logger.exception("%s failed", cat_tag)
            return CategoryResult(
                name=category.name,
                status="failed",
                duration_s=time.time() - cat_start,
                error=str(exc),
            )
        finally:
            conn.close()

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
        for fmt in formats:
            stage_dir = out_root / f"_stage_{slug}_{fmt}"
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            stage_dir.mkdir(parents=True)
            try:
                files = write_format(conn, table, slug, fmt, stage_dir)
                if not files:
                    continue
                dt_name = f"{self._cfg.key}_{self._cfg.iso3.lower()}_{slug}"
                zip_path = out_root / f"{dt_name}_{self._runner.name}_{fmt}.zip"
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


def _slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).lower().strip("_")


def _wrap_paragraph(text: str, *, indent: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


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
