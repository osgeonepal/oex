"""HdxPublisher.publish: batched resource pattern + 429 backoff."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from oex.config.schema import (
    CategoryConfig,
    CategoryHdx,
    CategoryOsm,
    HdxConfig,
    OutputConfig,
    RootConfig,
    S3Config,
)
from oex.hdx_publisher import (
    PublishContext,
    _hdx_publish_with_retry,
    _is_transient_hdx_error,
)


class _FakeHDXError(Exception):
    """Mimics hdx.data.hdxobject.HDXError for tests."""


def _patch_hdx_error():
    return patch("oex.hdx_publisher._hdx_publish_with_retry.__globals__")


def test_is_transient_recognises_retry_and_429() -> None:
    retry = requests.exceptions.RetryError("max retries")
    assert _is_transient_hdx_error(retry)

    response = requests.Response()
    response.status_code = 429
    http_429 = requests.exceptions.HTTPError("429 Too Many", response=response)
    assert _is_transient_hdx_error(http_429)

    response_503 = requests.Response()
    response_503.status_code = 503
    http_503 = requests.exceptions.HTTPError("503 Bad Gateway", response=response_503)
    assert _is_transient_hdx_error(http_503)

    assert _is_transient_hdx_error(requests.exceptions.ConnectionError("dns"))
    assert _is_transient_hdx_error(requests.exceptions.Timeout("slow"))


def test_is_transient_ignores_non_retryable() -> None:
    response = requests.Response()
    response.status_code = 400
    bad_request = requests.exceptions.HTTPError("400 Bad Request", response=response)
    assert not _is_transient_hdx_error(bad_request)
    assert not _is_transient_hdx_error(ValueError("bad config"))


def test_retry_succeeds_after_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oex.hdx_publisher._HDX_PUBLISH_BACKOFF_SECONDS", (0, 0, 0, 0))

    from hdx.data.hdxobject import HDXError

    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            cause = requests.exceptions.RetryError("max retries")
            err = HDXError("transient")
            err.__cause__ = cause
            raise err
        return "ok"

    result = _hdx_publish_with_retry(call, label="test")
    assert result == "ok"
    assert attempts["n"] == 2


def test_retry_propagates_non_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oex.hdx_publisher._HDX_PUBLISH_BACKOFF_SECONDS", (0, 0))

    from hdx.data.hdxobject import HDXError

    def call():
        raise HDXError("auth failed")

    with pytest.raises(HDXError, match="auth failed"):
        _hdx_publish_with_retry(call, label="test")


def test_retry_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oex.hdx_publisher._HDX_PUBLISH_BACKOFF_SECONDS", (0, 0, 0))

    from hdx.data.hdxobject import HDXError

    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        cause = requests.exceptions.ConnectionError("dns")
        err = HDXError("network")
        err.__cause__ = cause
        raise err

    with pytest.raises(HDXError, match="network"):
        _hdx_publish_with_retry(call, label="test")
    assert attempts["n"] == 3


def test_publish_attaches_resources_via_dataset_not_per_resource(tmp_path: Path) -> None:
    """publish() should call dataset.create_in_hdx ONCE, not Resource.create_in_hdx per zip."""

    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(
            push=True,
            api_key="x",
            owner_org="org",
            maintainer="me",
        ),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[
            CategoryConfig(
                name="buildings",
                hdx=CategoryHdx(license="hdx-odc-odbl"),
                osm=CategoryOsm(filter={"building": True}),
            )
        ],
    )

    zip1 = tmp_path / "hotosm_npl_buildings_osm_gpkg.zip"
    zip1.write_bytes(b"a" * 1024)
    zip2 = tmp_path / "hotosm_npl_buildings_osm_shp.zip"
    zip2.write_bytes(b"b" * 2048)

    fake_dataset = MagicMock(name="dataset")
    fake_dataset.get_resources.return_value = []
    fake_resources_added: list[object] = []
    fake_dataset.add_update_resource.side_effect = lambda r: fake_resources_added.append(r)

    fake_resource_class = MagicMock(name="Resource")

    def make_resource(data):
        m = MagicMock()
        m.data = dict(data)
        return m

    fake_resource_class.side_effect = make_resource

    create_calls: list[dict] = []

    def fake_create_in_hdx(**kw):
        create_calls.append(kw)
        return {"buildings": 0}

    fake_dataset.create_in_hdx.side_effect = fake_create_in_hdx

    with (
        patch("oex.hdx_publisher.HdxPublisher.__init__", return_value=None),
        patch(
            "oex.hdx_publisher.HdxPublisher._build_dataset_object",
            return_value=fake_dataset,
        ),
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=None),
        patch("hdx.data.resource.Resource", fake_resource_class),
    ):
        from oex.hdx_publisher import HdxPublisher

        publisher = HdxPublisher.__new__(HdxPublisher)
        publisher._owner_org = "org"
        publisher._maintainer = "me"

        ctx = PublishContext(
            dataset_source="OpenStreetMap",
            snapshot_date=datetime.now(UTC),
            source_name="osm",
            s3=cfg.output.s3,
        )
        publisher.publish(cfg, cfg.categories[0], [zip1, zip2], ctx)

    assert len(fake_resources_added) == 2, "expected one add_update_resource per zip"
    assert len(create_calls) == 1, "expected exactly ONE batched create_in_hdx call"
    assert create_calls[0]["allow_no_resources"] is False
    assert create_calls[0]["hxl_update"] is False
