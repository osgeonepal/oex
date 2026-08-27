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
    CombinedHdx,
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


def test_apply_custom_viz_turns_the_resource_preview_off() -> None:
    # HDX ignores customviz while dataset_preview is 'first_resource', which is
    # what set_custom_viz alone leaves behind.
    from hdx.api.configuration import Configuration
    from hdx.data.dataset import Dataset

    from oex.hdx_publisher import _apply_custom_viz

    Configuration.delete()
    Configuration.create(hdx_site="demo", user_agent="oex-test", hdx_key="dummy")
    try:
        dataset = Dataset({"name": "demo_npl", "dataset_preview": "first_resource"})
        _apply_custom_viz(dataset, "https://example.org/overview.html")

        assert dataset["customviz"] == [{"url": "https://example.org/overview.html"}]
        assert dataset["dataset_preview"] == "no_preview"
    finally:
        Configuration.delete()


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


def _build_combined_dataset(cfg: RootConfig) -> MagicMock:
    from oex.hdx_publisher import CombinedCategory, HdxPublisher

    def make_dataset(args):
        dataset = MagicMock()
        dataset.data = dict(args)
        dataset.tags = []
        dataset.add_tag.side_effect = dataset.tags.append
        return dataset

    entries = [CombinedCategory(category=c, zip_paths=[]) for c in cfg.categories]
    ctx = PublishContext(
        dataset_source="OpenStreetMap",
        snapshot_date=datetime.now(UTC),
        source_name="osm",
    )
    publisher = HdxPublisher.__new__(HdxPublisher)
    publisher._owner_org = "org"
    publisher._maintainer = "me"
    with patch("hdx.data.dataset.Dataset", MagicMock(side_effect=make_dataset)):
        return publisher._build_combined_dataset_object(cfg, entries, "eq_ven", ctx)


def test_combined_dataset_metadata_overrides_are_used() -> None:
    cfg = RootConfig(
        iso3="VEN",
        key="eq",
        hdx=HdxConfig(
            combine=True,
            combined=CombinedHdx(
                notes="Magnitude 7.5 earthquake, GLIDE EQ-2026-000093-VEN.",
                caveats="Overture footprints are partly AI-generated.",
                source="OpenStreetMap contributors; Overture Maps",
                tags=["crisis-venezuela-earthquake", "natural disasters"],
            ),
        ),
        categories=[CategoryConfig(name="buildings", hdx=CategoryHdx(tags=["geodata"]))],
    )
    dataset = _build_combined_dataset(cfg)

    assert dataset.data["notes"] == "Magnitude 7.5 earthquake, GLIDE EQ-2026-000093-VEN."
    assert dataset.data["caveats"] == "Overture footprints are partly AI-generated."
    assert dataset.data["dataset_source"] == "OpenStreetMap contributors; Overture Maps"
    # Dataset-level tags come first, then the per-category tags, deduplicated.
    assert dataset.tags == ["crisis-venezuela-earthquake", "natural disasters", "geodata"]


def test_combined_dataset_metadata_falls_back_when_unset() -> None:
    cfg = RootConfig(
        iso3="VEN",
        key="eq",
        hdx=HdxConfig(combine=True),
        categories=[
            CategoryConfig(name="buildings", hdx=CategoryHdx(caveats="Community verified."))
        ],
    )
    dataset = _build_combined_dataset(cfg)

    assert "Layers: Buildings" in str(dataset.data["notes"])
    assert dataset.data["caveats"] == "Community verified."
    assert dataset.data["dataset_source"] == "OpenStreetMap"


@pytest.mark.parametrize(("purge", "expected"), [(False, False), (True, True)])
def test_per_category_update_preserves_other_sources_resources_unless_purging(
    tmp_path: Path, purge: bool, expected: bool
) -> None:
    """A category's dataset holds every source, so an OSM run must not wipe Overture's.

    The report's source tabs and its map both read the other source's resources
    straight off the dataset.
    """
    from oex.hdx_publisher import HdxPublisher

    cfg = RootConfig(
        iso3="NPL",
        key="demo_dq",
        hdx=HdxConfig(purge_existing_resources=purge),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[CategoryConfig(name="Buildings")],
    )
    zip_path = tmp_path / "demo_dq_npl_buildings_osm_gpkg.zip"
    zip_path.write_bytes(b"z" * 512)

    existing = MagicMock()
    existing.__getitem__.side_effect = {"id": "abc", "owner_org": "org"}.__getitem__
    existing.get.return_value = "org"

    dataset = MagicMock()
    dataset.get_resources.return_value = []
    update_calls: list[dict] = []
    dataset.update_in_hdx.side_effect = lambda **kw: update_calls.append(kw)

    with (
        patch.object(HdxPublisher, "_build_dataset_object", return_value=dataset),
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=existing),
        patch.object(HdxPublisher, "_make_resource_for_zip", return_value=MagicMock(data={})),
    ):
        publisher = HdxPublisher.__new__(HdxPublisher)
        publisher._owner_org = "org"
        publisher._maintainer = "me"
        publisher.publish(
            cfg,
            cfg.categories[0],
            [zip_path],
            PublishContext(
                dataset_source="OpenStreetMap",
                snapshot_date=datetime.now(UTC),
                source_name="osm",
            ),
        )

    assert len(update_calls) == 1
    assert update_calls[0]["remove_additional_resources"] is expected


@pytest.mark.parametrize(("purge", "expected"), [(False, False), (True, True)])
def test_combined_update_preserves_other_sources_resources_unless_purging(
    tmp_path: Path, purge: bool, expected: bool
) -> None:
    """An Overture run must not delete the OSM run's resources from the shared dataset."""
    from oex.hdx_publisher import CombinedCategory, HdxPublisher

    cfg = RootConfig(
        iso3="VEN",
        key="demo_eq",
        hdx=HdxConfig(combine=True, purge_existing_resources=purge),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[CategoryConfig(name="Buildings")],
    )
    zip_path = tmp_path / "demo_eq_ven_buildings_overture_gpkg.zip"
    zip_path.write_bytes(b"z" * 512)

    existing = MagicMock()
    existing.__getitem__.side_effect = {"id": "abc", "owner_org": "org"}.__getitem__
    existing.get.return_value = "org"

    dataset = MagicMock()
    dataset.get_resources.return_value = []
    update_calls: list[dict] = []
    dataset.update_in_hdx.side_effect = lambda **kw: update_calls.append(kw)

    with (
        patch.object(HdxPublisher, "_build_combined_dataset_object", return_value=dataset),
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=existing),
        patch.object(HdxPublisher, "_make_resource_for_path", return_value=MagicMock(data={})),
    ):
        publisher = HdxPublisher.__new__(HdxPublisher)
        publisher._owner_org = "org"
        publisher._maintainer = "me"
        publisher.publish_combined(
            cfg,
            [CombinedCategory(category=cfg.categories[0], zip_paths=[zip_path])],
            PublishContext(
                dataset_source="OvertureMap",
                snapshot_date=datetime.now(UTC),
                source_name="overture",
            ),
            landing_enabled=False,
        )

    assert len(update_calls) == 1
    assert update_calls[0]["remove_additional_resources"] is expected


def test_landing_describes_every_layer_on_the_dataset_not_just_this_run() -> None:
    """An Overture run must still describe the OSM layers already on the dataset.

    The tileset is merged from every staged layer across sources, so a landing
    built from only the current run's categories would leave the other source's
    layers unlabelled and filtered off the map.
    """
    from oex.hdx_publisher import CombinedCategory, HdxPublisher

    cfg = RootConfig(
        iso3="VEN",
        key="demo_eq",
        hdx=HdxConfig(combine=True, combined=CombinedHdx(name="demo_eq_ven")),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[
            CategoryConfig(name="Buildings"),
            CategoryConfig(name="Roads"),
        ],
    )
    # On HDX already: Buildings from both sources, Roads from OSM only. This run
    # publishes Overture, so `entries` carries Buildings alone.
    resources = [
        {
            "name": "Buildings (OSM), JSON",
            "url": "https://x/demo_eq_ven_buildings_osm_metadata.json",
        },
        {
            "name": "Buildings (Overture), JSON",
            "url": "https://x/demo_eq_ven_buildings_overture_metadata.json",
        },
        {"name": "Roads (OSM), JSON", "url": "https://x/demo_eq_ven_roads_osm_metadata.json"},
    ]
    fresh = MagicMock()
    fresh.get_resources.return_value = resources

    def payload_for(url):
        source = "osm" if "_osm_metadata" in url else "overture"
        return {"source_name": source}

    captured: dict = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        return "<html></html>"

    def fake_metadata(payload):
        meta = MagicMock()
        meta.source_name = payload["source_name"]
        return meta

    publisher = HdxPublisher.__new__(HdxPublisher)
    publisher._owner_org = "org"
    publisher._maintainer = "me"

    ctx = PublishContext(
        dataset_source="OvertureMap",
        snapshot_date=datetime.now(UTC),
        source_name="overture",
        output_dir=Path("/tmp"),
    )
    with (
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=fresh),
        patch("oex.hdx_publisher._download_json", side_effect=payload_for),
        patch("oex.report.SourceMetadata.from_payload", side_effect=fake_metadata),
        patch("oex.report.landing.render_landing", side_effect=fake_render),
        patch.object(HdxPublisher, "_make_resource_for_path", return_value=MagicMock(data={})),
        patch.object(HdxPublisher, "_set_customviz_after_upload"),
        patch("oex.hdx_publisher._hdx_publish_with_retry"),
        patch("pathlib.Path.write_text"),
    ):
        publisher._build_and_publish_landing(
            dt_name="demo_eq_ven",
            cfg=cfg,
            entries=[CombinedCategory(category=cfg.categories[0], zip_paths=[])],
            pmtiles_layer="demo_eq_ven",
            ctx=ctx,
        )

    panels = captured["panels"]
    assert [p.slug for p in panels] == ["buildings", "roads"], "Roads (OSM) must not be dropped"
    # Buildings carries both sources, OSM first.
    assert [s.source_name for s in panels[0].sources] == ["osm", "overture"]
    assert captured["subtitle"] == "3 layers from OSM and Overture for Venezuela"


def test_publish_combined_attaches_all_categories_and_pmtiles_once(tmp_path: Path) -> None:
    """publish_combined() batches every category's resources onto ONE dataset."""
    from oex.hdx_publisher import CombinedCategory

    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, api_key="x", owner_org="org", maintainer="me", combine=True),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[
            CategoryConfig(name="buildings", hdx=CategoryHdx(license="hdx-odc-odbl")),
            CategoryConfig(name="roads", hdx=CategoryHdx(license="hdx-odc-odbl")),
        ],
    )

    b_zip = tmp_path / "hotosm_npl_buildings_osm_gpkg.zip"
    b_zip.write_bytes(b"a" * 2048)
    r_zip = tmp_path / "hotosm_npl_roads_osm_gpkg.zip"
    r_zip.write_bytes(b"b" * 1024)
    pmtiles = tmp_path / "hotosm_npl.pmtiles"
    pmtiles.write_bytes(b"PMTiles" + b"\x00" * 64)

    fake_dataset = MagicMock(name="dataset")
    fake_dataset.get_resources.return_value = []
    added: list[MagicMock] = []
    fake_dataset.add_update_resource.side_effect = lambda r: added.append(r)

    def make_resource(data):
        m = MagicMock()
        m.data = dict(data)
        return m

    fake_resource_class = MagicMock(side_effect=make_resource)

    create_calls: list[dict] = []
    fake_dataset.create_in_hdx.side_effect = lambda **kw: create_calls.append(kw)

    with (
        patch(
            "oex.hdx_publisher.HdxPublisher._build_combined_dataset_object",
            return_value=fake_dataset,
        ),
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=None),
        patch("hdx.data.resource.Resource", fake_resource_class),
    ):
        from oex.hdx_publisher import HdxPublisher

        publisher = HdxPublisher.__new__(HdxPublisher)
        publisher._owner_org = "org"
        publisher._maintainer = "me"

        entries = [
            CombinedCategory(category=cfg.categories[0], zip_paths=[b_zip]),
            CombinedCategory(category=cfg.categories[1], zip_paths=[r_zip]),
        ]
        ctx = PublishContext(
            dataset_source="OpenStreetMap",
            snapshot_date=datetime.now(UTC),
            source_name="osm",
            output_dir=tmp_path,
            s3=cfg.output.s3,
        )
        dt_name = publisher.publish_combined(
            cfg, entries, ctx, pmtiles_path=pmtiles, landing_enabled=False
        )

    assert dt_name == "hotosm_npl"
    assert len(create_calls) == 1, "expected exactly ONE combined create_in_hdx call"
    # pmtiles + one zip per category.
    formats = [r.data.get("format") for r in added]
    assert formats.count("pmtiles") == 1
    assert formats.count("gpkg") == 2
    # pmtiles is added before the category zips.
    assert formats[0] == "pmtiles"
