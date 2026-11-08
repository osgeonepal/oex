"""Overture source runner: reads a release from its public S3 bucket through DuckDB."""

import re
from datetime import UTC, datetime
from typing import Any, cast

import requests

from oex.config.schema import (
    CategoryConfig,
    OvertureSourceConfig,
    RootConfig,
)
from oex.logging_setup import get_logger
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner

logger = get_logger(__name__)


def resolve_release(release: str, *, bucket: str = "overturemaps-us-west-2") -> str:
    """Return a concrete release version, resolving "latest" via S3 listing."""
    if release and release != "latest":
        return release

    list_url = f"https://{bucket}.s3.us-west-2.amazonaws.com/"
    logger.info("Resolving Overture latest release via %s?prefix=release/", list_url)
    response = requests.get(
        list_url,
        params={"prefix": "release/", "delimiter": "/"},
        timeout=60,
    )
    response.raise_for_status()
    matches = re.findall(r"<Prefix>release/(\d{4}-\d{2}-\d{2}\.\d+)/</Prefix>", response.text)
    if not matches:
        raise RuntimeError(f"Could not detect Overture release prefixes from {list_url}")
    latest = max(matches, key=_release_sort_key)
    logger.info("Overture latest release resolved: %s", latest)
    return latest


def _release_sort_key(release: str) -> tuple[str, int]:
    """The minor counter compares as a number, so `.10` sorts after `.9`."""
    date, minor = release.split(".")
    return date, int(minor)


def _release_to_date(release: str) -> datetime:
    return datetime.strptime(release.split(".")[0], "%Y-%m-%d").replace(tzinfo=UTC)


class OvertureRunner(SourceRunner):
    name = "overture"

    def __init__(self) -> None:
        self._release: str | None = None

    def prepare(self, cfg: RootConfig) -> None:
        overture_config = cast(OvertureSourceConfig, cfg.source["overture"])
        if not overture_config.enabled:
            raise RuntimeError("Overture source is disabled in config")
        self._release = resolve_release(overture_config.release, bucket=overture_config.s3_bucket)
        logger.info(
            "Overture source: release=%s bucket=%s", self._release, overture_config.s3_bucket
        )

    def query_for(self, cfg: RootConfig, category: CategoryConfig) -> SourceQuery:
        if self._release is None:
            raise RuntimeError("OvertureRunner.prepare must run before query_for")

        overture_config: dict[str, Any] | OvertureSourceConfig = cfg.source["overture"]
        bucket: str
        if isinstance(overture_config, OvertureSourceConfig):
            bucket = overture_config.s3_bucket
        else:
            bucket = overture_config.get("s3_bucket", "overturemaps-us-west-2")

        if not category.overture.enabled:
            raise CategorySkippedError(f"{category.name}: overture disabled for category")
        if not category.overture.theme or not category.overture.feature_type:
            raise CategorySkippedError(
                f"{category.name}: overture.theme or overture.feature_type missing"
            )

        s3_glob = (
            f"s3://{bucket}/release/{self._release}/"
            f"theme={category.overture.theme}/type={category.overture.feature_type}/*"
        )
        source_expr = f"read_parquet('{s3_glob}', filename=true, hive_partitioning=1)"
        return SourceQuery(
            source_expr=source_expr,
            select_fields=list(category.overture.select),
            where_conditions=list(category.overture.where),
            bbox_mode="bbox",
            dataset_source=f"OvertureMap {self._release}",
            source_url="https://overturemaps.org/",
            source_description=(
                "Overture Maps publishes a unified, conflated geographic dataset "
                "stitched from OpenStreetMap, Microsoft, Google, Esri, TomTom, Meta "
                "and others. Licenses vary per theme; see this dataset's License field."
            ),
            snapshot_date=_release_to_date(self._release),
            snapshot_label=self._release,
            extra_readme_lines=[
                f"Theme: {category.overture.theme} / type: {category.overture.feature_type}",
            ],
        )
