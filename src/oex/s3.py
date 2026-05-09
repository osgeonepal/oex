"""Upload artifacts to S3 and return a public URL for HDX linking."""

import mimetypes
import os
from pathlib import Path
from typing import Any

from oex.config.schema import S3Config


def content_type_for(path: Path) -> str:
    # Without an explicit ContentType, S3 serves objects as application/octet-stream
    # and browsers prompt download instead of rendering (breaks the customviz iframe).
    ctype, _ = mimetypes.guess_type(path.name)
    return ctype or "application/octet-stream"


def resolve(cfg: S3Config) -> tuple[str, str, str, str | None, str]:
    bucket = cfg.bucket or os.environ.get("OEX_S3_BUCKET", "")
    prefix = cfg.prefix or os.environ.get("OEX_S3_PREFIX", "")
    region = cfg.region or os.environ.get("OEX_S3_REGION", "")
    endpoint_url = cfg.endpoint_url or os.environ.get("OEX_S3_ENDPOINT_URL")
    return bucket, prefix, region, endpoint_url, cfg.acl


def build_key(prefix: str, iso3: str, category_slug: str, filename: str) -> str:
    parts = [p.strip("/") for p in (prefix, iso3.upper(), category_slug) if p]
    parts.append(filename)
    return "/".join(parts)


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
    extra: dict[str, Any] = {"ContentType": content_type_for(path)}
    if acl:
        extra["ACL"] = acl
    client.upload_file(str(path), bucket, key, ExtraArgs=extra)
    return public_url(bucket=bucket, key=key, region=region, endpoint_url=endpoint_url)


def preflight(cfg: S3Config) -> None:
    import boto3
    import botocore.exceptions

    bucket, prefix, region, endpoint_url, acl = resolve(cfg)
    if not bucket:
        raise ValueError(
            "output.s3.enabled is true but no bucket given via output.s3.bucket or OEX_S3_BUCKET"
        )

    client = boto3.client("s3", region_name=region or None, endpoint_url=endpoint_url)
    try:
        client.head_bucket(Bucket=bucket)
    except botocore.exceptions.ClientError as exc:
        raise RuntimeError(f"S3 preflight: cannot reach bucket {bucket!r}: {exc}") from exc

    test_key = f"{prefix.strip('/')}/.oex_preflight" if prefix else ".oex_preflight"
    extra: dict[str, Any] = {"ACL": acl} if acl else {}
    try:
        client.put_object(Bucket=bucket, Key=test_key, Body=b"oex preflight ok", **extra)
    except botocore.exceptions.ClientError as exc:
        raise RuntimeError(
            f"S3 preflight: cannot put to s3://{bucket}/{test_key} with ACL={acl!r}: {exc}"
        ) from exc
