"""The Overture runner: release resolution from the bucket listing and per-category queries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from oex.config.schema import CategoryConfig, CategoryOverture, OvertureSourceConfig, RootConfig
from oex.overture.runner import OvertureRunner, resolve_release
from oex.sources.base import CategorySkippedError

_LISTING = (
    "<ListBucketResult>"
    "<CommonPrefixes><Prefix>release/2024-10-23.9/</Prefix></CommonPrefixes>"
    "<CommonPrefixes><Prefix>release/2024-10-23.10/</Prefix></CommonPrefixes>"
    "<CommonPrefixes><Prefix>release/2024-11-13.0/</Prefix></CommonPrefixes>"
    "<CommonPrefixes><Prefix>release/2024-09-18.0/</Prefix></CommonPrefixes>"
    "</ListBucketResult>"
)


def _listing(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, raise_for_status=lambda: None)


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        (_LISTING, "2024-11-13.0"),
        (_LISTING.replace("2024-11-13.0", "2024-10-01.0"), "2024-10-23.10"),
    ],
    ids=["newest date wins", "minor version compares as a number"],
)
def test_latest_resolves_to_the_newest_release_in_the_listing(listing: str, expected: str) -> None:
    with patch("oex.overture.runner.requests.get", return_value=_listing(listing)):
        assert resolve_release("latest") == expected


def test_a_listing_with_no_release_prefixes_fails_loud() -> None:
    """Exporting under an invented release would mislabel every dataset."""
    with (
        patch("oex.overture.runner.requests.get", return_value=_listing("<ListBucketResult/>")),
        pytest.raises(RuntimeError, match="Could not detect Overture release"),
    ):
        resolve_release("latest")


def test_a_pinned_release_is_used_without_listing_the_bucket() -> None:
    with patch("oex.overture.runner.requests.get") as get:
        assert resolve_release("2024-09-18.0") == "2024-09-18.0"
    get.assert_not_called()


def _prepared(release: str = "2024-10-23.10") -> tuple[OvertureRunner, RootConfig]:
    cfg = RootConfig(iso3="NPL", key="t")
    cfg.source["overture"] = OvertureSourceConfig(release=release, s3_bucket="example-bucket")
    runner = OvertureRunner()
    runner.prepare(cfg)
    return runner, cfg


def test_a_category_query_reads_its_theme_and_type_from_the_release_and_dates_it() -> None:
    runner, cfg = _prepared()
    category = CategoryConfig(
        name="buildings",
        overture=CategoryOverture(
            theme="buildings", feature_type="building", select=["id"], where=["height > 3"]
        ),
    )

    query = runner.query_for(cfg, category)

    assert query.source_expr == (
        "read_parquet('s3://example-bucket/release/2024-10-23.10/theme=buildings/type=building/*',"
        " filename=true, hive_partitioning=1)"
    )
    assert query.select_fields == ["id"]
    assert query.where_conditions == ["height > 3"]
    assert query.snapshot_label == "2024-10-23.10"
    assert query.snapshot_date == datetime(2024, 10, 23, tzinfo=UTC)


@pytest.mark.parametrize(
    "overture",
    [
        CategoryOverture(enabled=False, theme="buildings", feature_type="building"),
        CategoryOverture(theme="", feature_type="building"),
        CategoryOverture(theme="buildings", feature_type=""),
    ],
    ids=["disabled", "no theme", "no feature type"],
)
def test_a_category_overture_cannot_read_is_skipped_not_failed(
    overture: CategoryOverture,
) -> None:
    runner, cfg = _prepared()
    with pytest.raises(CategorySkippedError):
        runner.query_for(cfg, CategoryConfig(name="buildings", overture=overture))


def test_a_query_before_prepare_is_refused() -> None:
    cfg = RootConfig(iso3="NPL", key="t")
    with pytest.raises(RuntimeError, match="prepare must run before query_for"):
        OvertureRunner().query_for(cfg, CategoryConfig(name="buildings"))
