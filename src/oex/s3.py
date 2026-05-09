"""Upload artifacts to S3 and return a public URL for HDX linking."""

from pathlib import Path
from typing import Any


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
    extra: dict[str, Any] = {"ACL": acl} if acl else {}
    client.upload_file(str(path), bucket, key, ExtraArgs=extra or None)
    return public_url(bucket=bucket, key=key, region=region, endpoint_url=endpoint_url)
