"""Unit tests for the S3 key + URL builders. No network access."""

from oex.s3 import build_key, public_url


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
