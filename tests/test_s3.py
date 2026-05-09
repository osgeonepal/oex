"""Unit tests for the S3 key + URL builders. No network access."""

from pathlib import Path

import pytest

from oex.config.schema import S3Config
from oex.s3 import build_key, content_type_for, public_url, resolve


def test_build_key_uses_iso3_then_category_then_filename() -> None:
    assert build_key("", "NPL", "buildings", "x.zip") == "NPL/buildings/x.zip"


def test_build_key_uppercases_iso3() -> None:
    assert build_key("", "vnm", "roads", "y.zip") == "VNM/roads/y.zip"


def test_build_key_prepends_prefix_when_set() -> None:
    assert (
        build_key("hotosm/exports", "NPL", "buildings", "x.zip")
        == "hotosm/exports/NPL/buildings/x.zip"
    )


def test_build_key_strips_stray_slashes_in_prefix() -> None:
    assert (
        build_key("/hotosm/exports/", "NPL", "buildings", "x.zip")
        == "hotosm/exports/NPL/buildings/x.zip"
    )


def test_public_url_us_east_1_uses_legacy_host() -> None:
    url = public_url(bucket="my-bucket", key="a/b/c.zip", region="us-east-1", endpoint_url=None)
    assert url == "https://my-bucket.s3.amazonaws.com/a/b/c.zip"


def test_public_url_other_region_includes_region_in_host() -> None:
    url = public_url(bucket="my-bucket", key="a/b.zip", region="ap-south-1", endpoint_url=None)
    assert url == "https://my-bucket.s3.ap-south-1.amazonaws.com/a/b.zip"


def test_public_url_empty_region_falls_back_to_legacy() -> None:
    url = public_url(bucket="b", key="k", region="", endpoint_url=None)
    assert url == "https://b.s3.amazonaws.com/k"


def test_public_url_custom_endpoint_overrides_aws_host() -> None:
    url = public_url(bucket="b", key="k.zip", region="", endpoint_url="https://r2.example.com/")
    assert url == "https://r2.example.com/b/k.zip"


def test_content_type_html() -> None:
    assert content_type_for(Path("report.html")) == "text/html"


def test_content_type_json() -> None:
    assert content_type_for(Path("metadata.json")) == "application/json"


def test_content_type_zip() -> None:
    assert content_type_for(Path("buildings_gpkg.zip")) == "application/zip"


def test_content_type_unknown_falls_back_to_octet_stream() -> None:
    assert content_type_for(Path("blob.unknown_ext_xyz")) == "application/octet-stream"


def test_resolve_uses_yaml_values_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OEX_S3_BUCKET", "env-bucket")
    monkeypatch.setenv("OEX_S3_PREFIX", "env-prefix")
    cfg = S3Config(enabled=True, bucket="yaml-bucket", prefix="yaml-prefix", region="us-east-1")
    bucket, prefix, region, endpoint, acl = resolve(cfg)
    assert (bucket, prefix, region) == ("yaml-bucket", "yaml-prefix", "us-east-1")


def test_resolve_falls_back_to_env_when_yaml_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OEX_S3_BUCKET", "env-bucket")
    monkeypatch.setenv("OEX_S3_PREFIX", "env-prefix")
    monkeypatch.setenv("OEX_S3_REGION", "ap-south-1")
    monkeypatch.setenv("OEX_S3_ENDPOINT_URL", "https://r2.example.com")
    cfg = S3Config(enabled=True)
    bucket, prefix, region, endpoint, acl = resolve(cfg)
    assert bucket == "env-bucket"
    assert prefix == "env-prefix"
    assert region == "ap-south-1"
    assert endpoint == "https://r2.example.com"
    assert acl == "public-read"


def test_resolve_returns_empty_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OEX_S3_BUCKET", "OEX_S3_PREFIX", "OEX_S3_REGION", "OEX_S3_ENDPOINT_URL"):
        monkeypatch.delenv(var, raising=False)
    bucket, prefix, region, endpoint, acl = resolve(S3Config(enabled=True))
    assert bucket == ""
    assert prefix == ""
    assert region == ""
    assert endpoint is None
