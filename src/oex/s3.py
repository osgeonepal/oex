"""Upload artifacts to S3 and return a public URL for HDX linking."""

import mimetypes
import os
from pathlib import Path
from typing import Any

from oex.config.schema import S3Config


def content_type_for(path: Path) -> str:
    # Without an explicit ContentType, S3 serves objects as application/octet-stream
    # and browsers prompt download instead of rendering (breaking the custom visualisation iframe).
    content_type, _ = mimetypes.guess_type(path.name)
    return content_type or "application/octet-stream"


def resolve(s3_config: S3Config) -> tuple[str, str, str, str | None, str]:
    bucket = s3_config.bucket or os.environ.get("OEX_S3_BUCKET", "")
    prefix = s3_config.prefix or os.environ.get("OEX_S3_PREFIX", "")
    region = s3_config.region or os.environ.get("OEX_S3_REGION", "")
    endpoint_url = s3_config.endpoint_url or os.environ.get("OEX_S3_ENDPOINT_URL")
    return bucket, prefix, region, endpoint_url, s3_config.acl


def resolve_with_bucket(s3_config: S3Config) -> tuple[str, str, str, str | None, str]:
    """`resolve`, raising when neither the config nor the environment names a bucket."""
    resolved = resolve(s3_config)
    if not resolved[0]:
        raise ValueError(
            "output.s3.enabled is true but no bucket given via output.s3.bucket or OEX_S3_BUCKET"
        )
    return resolved


def artifact_key(
    prefix: str,
    iso3: str,
    category_slug: str,
    filename: str,
    *,
    folder: str = "",
    nest_by_category: bool = True,
    geometry: str = "",
) -> str:
    """Object key nested by area (`folder`, else iso3), then optionally by category and by a
    points/lines/polygons `geometry` segment."""
    segments = [prefix, folder or iso3.upper()]
    if nest_by_category:
        segments.append(category_slug)
    if geometry:
        segments.append(geometry)
    parts = [segment.strip("/") for segment in segments if segment]
    parts.append(filename)
    return "/".join(parts)


def category_artifact_key(
    s3_config: S3Config,
    prefix: str,
    iso3: str,
    category_slug: str,
    filename: str,
    *,
    geometry: str = "",
) -> str:
    """`artifact_key` laid out by the S3 config, under the prefix `resolve` gave; `geometry`
    applies only with nest_by_geometry."""
    return artifact_key(
        prefix,
        iso3,
        category_slug,
        filename,
        folder=s3_config.folder,
        nest_by_category=s3_config.nest_by_category,
        geometry=geometry if s3_config.nest_by_geometry else "",
    )


def build_layer_key(prefix: str, iso3: str, source: str, slug: str, folder: str = "") -> str:
    """Stable key for a layer's GeoParquet, so runs accumulate across sources; ``folder`` keeps
    two configs for the same country from overwriting each other."""
    return artifact_key(prefix, folder or iso3, f"_layers/{source}", f"{slug}.parquet")


def list_layer_urls(
    s3_config: S3Config, iso3: str, *, exclude_source: str | None = None
) -> list[tuple[str, str, str]]:
    """Staged per-layer GeoParquets as (source, slug, url)."""
    import boto3

    bucket, prefix, region, endpoint_url, _ = resolve(s3_config)
    if not bucket:
        return []
    area_id = (s3_config.folder or iso3).upper()
    key_prefix = "/".join(part.strip("/") for part in (prefix, area_id, "_layers") if part) + "/"
    client = boto3.client("s3", region_name=region or None, endpoint_url=endpoint_url)
    layers: list[tuple[str, str, str]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key_prefix):
        for listed in page.get("Contents", []):
            key = listed["Key"]
            if not key.endswith(".parquet"):
                continue
            relative_parts = key[len(key_prefix) :].split("/")
            if len(relative_parts) != 2:
                continue
            source, filename = relative_parts
            if exclude_source is not None and source == exclude_source:
                continue
            slug = filename[: -len(".parquet")]
            url = public_url(bucket=bucket, key=key, region=region, endpoint_url=endpoint_url)
            layers.append((source, slug, url))
    return layers


def public_url(*, bucket: str, key: str, region: str, endpoint_url: str | None) -> str:
    if endpoint_url:
        return f"{endpoint_url.rstrip('/')}/{bucket}/{key}"
    if region and region != "us-east-1":
        return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def upload(
    path: Path,
    *,
    bucket: str,
    key: str,
    region: str = "",
    endpoint_url: str | None = None,
    acl: str = "public-read",
) -> str:
    import boto3

    client = boto3.client("s3", region_name=region or None, endpoint_url=endpoint_url)
    extra_args: dict[str, Any] = {"ContentType": content_type_for(path)}
    if acl:
        extra_args["ACL"] = acl
    client.upload_file(str(path), bucket, key, ExtraArgs=extra_args)
    return public_url(bucket=bucket, key=key, region=region, endpoint_url=endpoint_url)


def preflight(s3_config: S3Config) -> None:
    import boto3
    import botocore.exceptions

    bucket, prefix, region, endpoint_url, acl = resolve_with_bucket(s3_config)

    client = boto3.client("s3", region_name=region or None, endpoint_url=endpoint_url)
    try:
        client.head_bucket(Bucket=bucket)
    except botocore.exceptions.ClientError as exc:
        raise RuntimeError(f"S3 preflight: cannot reach bucket {bucket!r}: {exc}") from exc

    test_key = f"{prefix.strip('/')}/.oex_preflight" if prefix else ".oex_preflight"
    extra_args: dict[str, Any] = {"ACL": acl} if acl else {}
    try:
        client.put_object(Bucket=bucket, Key=test_key, Body=b"oex preflight ok", **extra_args)
    except botocore.exceptions.ClientError as exc:
        raise RuntimeError(
            f"S3 preflight: cannot put to s3://{bucket}/{test_key} with ACL={acl!r}: {exc}"
        ) from exc
