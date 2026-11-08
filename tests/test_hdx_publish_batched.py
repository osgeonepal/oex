"""HdxPublisher.publish: batched resource pattern + 429 backoff."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from hdx.data.dataset import Dataset
from hdx.data.hdxobject import HDXError
from hdx.data.resource import Resource

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
    CombinedCategory,
    ExtraResource,
    HdxPublisher,
    PublishContext,
    _apply_custom_viz,
    _hdx_publish_with_retry,
    _is_transient_hdx_error,
    _reuse_resource_ids,
    related_dataset_links,
)
from oex.report import SourceMetadata
from oex.stac import LayerAssets, build_collection, build_item, item_href, to_payload


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


def test_apply_custom_viz_turns_the_resource_preview_off(hdx_demo_configuration: None) -> None:
    """HDX ignores customviz while dataset_preview is 'first_resource', which is what
    set_custom_viz alone leaves behind."""
    dataset = Dataset({"name": "demo_npl", "dataset_preview": "first_resource"})
    _apply_custom_viz(dataset, "https://example.org/overview.html")

    assert dataset["customviz"] == [{"url": "https://example.org/overview.html"}]
    assert dataset["dataset_preview"] == "no_preview"


def test_retry_propagates_non_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oex.hdx_publisher._HDX_PUBLISH_BACKOFF_SECONDS", (0, 0))

    def call():
        raise HDXError("auth failed")

    with pytest.raises(HDXError, match="auth failed"):
        _hdx_publish_with_retry(call, label="test")


def test_retry_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oex.hdx_publisher._HDX_PUBLISH_BACKOFF_SECONDS", (0, 0, 0))

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


def _publish_buildings(
    publisher: HdxPublisher,
    tmp_path: Path,
    *,
    extra_resources: list[ExtraResource] | None = None,
    metadata_json_path: Path | None = None,
) -> tuple[list[MagicMock], list[dict]]:
    """Publish a buildings layer against a stubbed HDX, returning the resources and creates."""
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
    fake_resources_added: list[MagicMock] = []
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
        patch.object(HdxPublisher, "_build_dataset_object", return_value=fake_dataset),
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=None),
        patch("hdx.data.resource.Resource", fake_resource_class),
    ):
        ctx = PublishContext(
            snapshot_date=datetime.now(UTC),
            source_name="osm",
            s3=cfg.output.s3,
            metadata_json_path=metadata_json_path,
        )
        publisher.publish(
            cfg, cfg.categories[0], [zip1, zip2], ctx, extra_resources=extra_resources
        )
    return fake_resources_added, create_calls


def test_publish_attaches_resources_via_dataset_not_per_resource(
    bare_publisher: HdxPublisher, tmp_path: Path
) -> None:
    """publish() should call dataset.create_in_hdx ONCE, not Resource.create_in_hdx per zip."""
    fake_resources_added, create_calls = _publish_buildings(bare_publisher, tmp_path)

    assert len(fake_resources_added) == 2, "expected one add_update_resource per zip"
    assert len(create_calls) == 1, "expected exactly ONE batched create_in_hdx call"
    assert create_calls[0]["allow_no_resources"] is False
    assert create_calls[0]["hxl_update"] is False


def test_every_resource_on_a_category_dataset_has_a_readable_title(
    bare_publisher: HdxPublisher, tmp_path: Path
) -> None:
    """A filename tells a reader nothing about the file, so every resource gets a readable title."""
    tiles = tmp_path / "hotosm_npl_buildings_osm.pmtiles"
    tiles.write_bytes(b"t")
    metadata = tmp_path / "hotosm_npl_buildings_osm_metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    added, _ = _publish_buildings(
        bare_publisher,
        tmp_path,
        extra_resources=[ExtraResource(path=tiles, format_name="pmtiles", description="tiles")],
        metadata_json_path=metadata,
    )
    assert [r.data["name"] for r in added] == [
        "Buildings (OSM), GeoPackage",
        "Buildings (OSM), Shapefile",
        "Buildings (OSM), Vector Tiles (PMTiles)",
        "Buildings (OSM), STAC Collection",
    ]


def _build_combined_dataset(publisher: HdxPublisher, cfg: RootConfig) -> MagicMock:
    def make_dataset(args):
        dataset = MagicMock()
        dataset.data = dict(args)
        dataset.tags = []
        dataset.add_tag.side_effect = dataset.tags.append
        return dataset

    entries = [CombinedCategory(category=c, zip_paths=[]) for c in cfg.categories]
    ctx = PublishContext(
        snapshot_date=datetime.now(UTC),
        source_name="osm",
    )
    with patch("hdx.data.dataset.Dataset", MagicMock(side_effect=make_dataset)):
        return publisher._build_combined_dataset_object(cfg, entries, "eq_ven", ctx)


def test_combined_dataset_metadata_overrides_are_used(bare_publisher: HdxPublisher) -> None:
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
    dataset = _build_combined_dataset(bare_publisher, cfg)

    assert dataset.data["notes"] == "Magnitude 7.5 earthquake, GLIDE EQ-2026-000093-VEN."
    assert dataset.data["caveats"] == "Overture footprints are partly AI-generated."
    assert dataset.data["dataset_source"] == "OpenStreetMap contributors; Overture Maps"
    # Dataset tags lead so the crisis tag is the one HDX shows first.
    assert dataset.tags == ["crisis-venezuela-earthquake", "natural disasters", "geodata"]


def test_combined_dataset_metadata_falls_back_to_the_category_values_when_unset(
    bare_publisher: HdxPublisher,
) -> None:
    cfg = RootConfig(
        iso3="VEN",
        key="eq",
        hdx=HdxConfig(combine=True),
        categories=[
            CategoryConfig(name="buildings", hdx=CategoryHdx(caveats="Community verified."))
        ],
    )
    dataset = _build_combined_dataset(bare_publisher, cfg)

    assert "Layers: Buildings" in str(dataset.data["notes"])
    assert dataset.data["caveats"] == "Community verified."
    assert dataset.data["dataset_source"] == "OpenStreetMap"


def _existing_dataset(owner_org: str) -> MagicMock:
    existing = MagicMock()
    existing.__getitem__.side_effect = {"id": "abc", "owner_org": owner_org}.__getitem__
    existing.get.side_effect = {"owner_org": owner_org}.get
    existing.get_resources.return_value = []
    return existing


def _publish_one_category(publisher: HdxPublisher, cfg: RootConfig, zip_path: Path) -> None:
    with patch.object(HdxPublisher, "_make_resource_for_zip", return_value=MagicMock(data={})):
        publisher.publish(
            cfg,
            cfg.categories[0],
            [zip_path],
            PublishContext(snapshot_date=datetime.now(UTC), source_name="osm"),
        )


def _publish_combined_category(publisher: HdxPublisher, cfg: RootConfig, zip_path: Path) -> None:
    with patch.object(HdxPublisher, "_make_resource_for_path", return_value=MagicMock(data={})):
        publisher.publish_combined(
            cfg,
            [CombinedCategory(category=cfg.categories[0], zip_paths=[zip_path])],
            PublishContext(snapshot_date=datetime.now(UTC), source_name="overture"),
            page_enabled=False,
        )


_ENTRY_POINTS = pytest.mark.parametrize(
    ("combine", "build_object", "publish_entry"),
    [
        (False, "_build_dataset_object", _publish_one_category),
        (True, "_build_combined_dataset_object", _publish_combined_category),
    ],
    ids=["per category", "combined"],
)


@_ENTRY_POINTS
@pytest.mark.parametrize(("purge", "expected"), [(False, False), (True, True)])
def test_an_update_preserves_other_sources_resources_unless_purging(
    bare_publisher: HdxPublisher,
    tmp_path: Path,
    combine: bool,
    build_object: str,
    publish_entry: Callable[[HdxPublisher, RootConfig, Path], None],
    purge: bool,
    expected: bool,
) -> None:
    """A dataset holds every source, so an OSM run must not wipe Overture's resources: the page
    and its map read the other source's resources straight off the dataset."""
    cfg = RootConfig(
        iso3="NPL",
        key="demo_dq",
        hdx=HdxConfig(combine=combine, purge_existing_resources=purge),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[CategoryConfig(name="Buildings")],
    )
    zip_path = tmp_path / "demo_dq_npl_buildings_osm_gpkg.zip"
    zip_path.write_bytes(b"z" * 512)
    dataset = MagicMock()
    dataset.get_resources.return_value = []
    update_calls: list[dict] = []
    dataset.update_in_hdx.side_effect = lambda **kw: update_calls.append(kw)

    with (
        patch.object(HdxPublisher, build_object, return_value=dataset),
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=_existing_dataset("org")),
    ):
        publish_entry(bare_publisher, cfg, zip_path)

    assert len(update_calls) == 1
    assert update_calls[0]["remove_additional_resources"] is expected


@_ENTRY_POINTS
def test_a_dataset_owned_by_another_organisation_is_never_updated(
    bare_publisher: HdxPublisher,
    tmp_path: Path,
    combine: bool,
    build_object: str,
    publish_entry: Callable[[HdxPublisher, RootConfig, Path], None],
) -> None:
    """Two organisations can pick the same `key`; publishing over the other's dataset would
    replace their data under their name."""
    cfg = RootConfig(
        iso3="NPL",
        key="demo_dq",
        hdx=HdxConfig(combine=combine),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[CategoryConfig(name="Buildings")],
    )
    zip_path = tmp_path / "demo_dq_npl_buildings_osm_gpkg.zip"
    zip_path.write_bytes(b"z" * 512)
    dataset = MagicMock()
    dataset.get_resources.return_value = []

    with (
        patch.object(HdxPublisher, build_object, return_value=dataset),
        patch(
            "hdx.data.dataset.Dataset.read_from_hdx",
            return_value=_existing_dataset("other-org"),
        ),
        pytest.raises(RuntimeError, match="different organisation"),
    ):
        publish_entry(bare_publisher, cfg, zip_path)

    dataset.update_in_hdx.assert_not_called()
    dataset.create_in_hdx.assert_not_called()


def test_refreshing_the_date_of_a_dataset_owned_by_another_organisation_is_refused(
    bare_publisher: HdxPublisher,
) -> None:
    cfg = RootConfig(iso3="NPL", key="demo_dq", categories=[CategoryConfig(name="Buildings")])
    existing = _existing_dataset("other-org")

    with (
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=existing),
        pytest.raises(RuntimeError, match="different organisation"),
    ):
        bare_publisher.refresh_metadata(
            cfg,
            cfg.categories[0],
            PublishContext(snapshot_date=datetime.now(UTC), source_name="osm"),
        )

    existing.update_in_hdx.assert_not_called()


def _catalog(
    cfg: RootConfig,
    source: str,
    categories: list[str],
    make_source_metadata: Callable[..., SourceMetadata],
) -> dict:
    """One source's Collection and per-layer Items, keyed by the URL a reader fetches each from."""
    by_name = {c.name: c for c in cfg.categories}
    layers = [
        LayerAssets(
            category=by_name[name],
            source_metadata=make_source_metadata(source),
            dataset_name=f"demo_eq_ven_{name.lower()}",
            # Real Items list what the layer published; without assets the page would
            # offer no downloads and the test would not notice.
            artifacts=[
                Path(f"demo_eq_ven_{name.lower()}_{source}_gpkg.zip"),
                Path(f"demo_eq_ven_{name.lower()}_{source}.geojson"),
            ],
        )
        for name in categories
    ]
    collection = build_collection(
        dataset_name="demo_eq_ven",
        title="Venezuela earthquake",
        description="Vector data for the affected area.",
        layers=layers,
        cfg=cfg,
    )
    documents = {f"https://x/demo_eq_ven_{source}_metadata.json": to_payload(collection)}
    for layer in layers:
        # Keyed on the href the Collection actually links, so a wrong key fails the test.
        documents[item_href(cfg, layer)] = to_payload(build_item(layer, cfg))
    return documents


def test_the_dataset_page_describes_every_layer_on_the_dataset_not_just_this_run(
    bare_publisher: HdxPublisher,
    tmp_path: Path,
    make_source_metadata: Callable[..., SourceMetadata],
) -> None:
    """An Overture run must still describe the OSM layers on the dataset, or the merged tileset
    would carry layers the page leaves unlabelled and filters off the map."""
    cfg = RootConfig(
        iso3="VEN",
        key="demo_eq",
        hdx=HdxConfig(combine=True, combined=CombinedHdx(name="demo_eq_ven")),
        output=OutputConfig(s3=S3Config(enabled=True, bucket="example-bucket", region="us-east-1")),
        categories=[
            CategoryConfig(name="Buildings"),
            CategoryConfig(name="Roads"),
        ],
    )
    # On HDX already: Buildings from both sources, Roads from OSM only. This run
    # publishes Overture, so `entries` carries Buildings alone.
    resources = [
        {
            "id": "osm-metadata",
            "name": "Layer Metadata (OSM), JSON",
            "url": "https://x/demo_eq_ven_osm_metadata.json",
        },
        {
            "id": "overture-metadata",
            "name": "Layer Metadata (Overture), JSON",
            "url": "https://x/demo_eq_ven_overture_metadata.json",
        },
    ]
    fetched = {
        **_catalog(cfg, "osm", ["Buildings", "Roads"], make_source_metadata),
        **_catalog(cfg, "overture", ["Buildings"], make_source_metadata),
    }
    fresh = MagicMock()
    fresh.get_resources.return_value = resources

    captured: dict = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        return "<html></html>"

    ctx = PublishContext(
        snapshot_date=datetime.now(UTC),
        source_name="overture",
        output_dir=tmp_path,
    )
    with (
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=fresh),
        patch("oex.hdx_publisher._download_json", side_effect=lambda url: fetched[url]),
        patch("oex.report.page.render_dataset_page", side_effect=fake_render),
        patch.object(HdxPublisher, "_make_resource_for_path", return_value=MagicMock(data={})),
        patch.object(HdxPublisher, "_set_customviz_after_upload"),
        patch("oex.hdx_publisher._hdx_publish_with_retry"),
    ):
        bare_publisher._build_and_publish_page(
            hdx_dataset_name="demo_eq_ven",
            cfg=cfg,
            entries=[CombinedCategory(category=cfg.categories[0], zip_paths=[])],
            pmtiles_layer="demo_eq_ven",
            publish_context=ctx,
        )

    layers = captured["layers"]
    assert [layer.key for layer in layers] == [
        "buildings-osm",
        "buildings-overture",
        "roads-osm",
    ], "Roads (OSM) must not be dropped"
    assert [layer.label for layer in layers[:2]] == [
        "Buildings (OSM)",
        "Buildings (Overture)",
    ]
    # The published page offers what each Item lists, not a guess at the filenames.
    assert [d.format for d in layers[0].downloads] == ["gpkg", "geojson"]
    assert layers[0].metadata_href.endswith("_buildings_osm_item.json"), layers[0].metadata_href


def test_publish_combined_attaches_all_categories_and_pmtiles_once(
    bare_publisher: HdxPublisher, tmp_path: Path
) -> None:
    """publish_combined() batches every category's resources onto ONE dataset."""
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
        entries = [
            CombinedCategory(category=cfg.categories[0], zip_paths=[b_zip]),
            CombinedCategory(category=cfg.categories[1], zip_paths=[r_zip]),
        ]
        ctx = PublishContext(
            snapshot_date=datetime.now(UTC),
            source_name="osm",
            output_dir=tmp_path,
            s3=cfg.output.s3,
        )
        dt_name = bare_publisher.publish_combined(
            cfg, entries, ctx, pmtiles_path=pmtiles, page_enabled=False
        )

    assert dt_name == "hotosm_npl"
    assert len(create_calls) == 1, "expected exactly ONE combined create_in_hdx call"
    formats = [r.data.get("format") for r in added]
    assert formats.count("pmtiles") == 1
    assert formats.count("gpkg") == 2
    assert formats[0] == "pmtiles"


def test_a_combined_run_orders_by_every_configured_category_not_only_those_it_built(
    bare_publisher: HdxPublisher, tmp_path: Path
) -> None:
    """The OSM and Overture runs build different categories; both must give HDX one order."""
    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, combine=True),
        output=OutputConfig(s3=S3Config(enabled=False)),
        categories=[CategoryConfig(name="buildings"), CategoryConfig(name="roads")],
    )
    roads_only = [CombinedCategory(category=cfg.categories[1], zip_paths=[])]
    context = PublishContext(snapshot_date=datetime.now(UTC), source_name="overture")
    with (
        patch.object(HdxPublisher, "_build_combined_dataset_object", return_value=MagicMock()),
        patch.object(HdxPublisher, "_create_or_update") as create_or_update,
    ):
        bare_publisher.publish_combined(cfg, roads_only, context, page_enabled=False)
    assert create_or_update.call_args.kwargs["category_labels"] == ["Buildings", "Roads"]


def test_a_retitled_file_keeps_its_hdx_resource_instead_of_gaining_a_second() -> None:
    """HDX matches by id then name, so the id is what stops a rename from duplicating."""
    base = "https://bucket.s3.amazonaws.com/ISO3/NPL/buildings/"
    existing = [
        {
            "id": "old",
            "name": "hotosm_npl_buildings_osm_gpkg.zip",
            "url": f"{base}hotosm_npl_buildings_osm_gpkg.zip",
        },
        {
            "id": "titled",
            "name": "Buildings (OSM), GeoPackage",
            "url": f"{base}hotosm_npl_buildings_osm_gpkg.zip",
        },
        {
            "id": "tiles",
            "name": "hotosm_npl_buildings_osm.pmtiles",
            "url": f"{base}hotosm_npl_buildings_osm.pmtiles",
        },
    ]
    new = [
        {"name": "Buildings (OSM), GeoPackage", "url": f"{base}hotosm_npl_buildings_osm_gpkg.zip"},
        {
            "name": "Buildings (OSM), Vector Tiles (PMTiles)",
            "url": f"{base}hotosm_npl_buildings_osm.pmtiles",
        },
        {"name": "Buildings (OSM), GeoParquet", "url": f"{base}buildings.parquet"},
    ]
    _reuse_resource_ids(new, existing)
    # The entry already carrying the title wins over the filename-named one.
    assert [r.get("id") for r in new] == ["titled", "tiles", None]


def test_updating_a_dataset_carries_ids_over_before_hdx_matches_by_name(
    bare_publisher: HdxPublisher,
) -> None:
    url = "https://bucket.s3.amazonaws.com/ISO3/NPL/buildings/hotosm_npl_buildings_osm.pmtiles"
    new = [{"name": "Buildings (OSM), Vector Tiles (PMTiles)", "url": url}]
    dataset = MagicMock()
    dataset.get_resources.return_value = new
    existing = MagicMock()
    existing.get.return_value = "org"
    existing.__getitem__.return_value = "dataset-id"
    existing.get_resources.return_value = [
        {"id": "tiles", "name": "hotosm_npl_buildings_osm.pmtiles", "url": url}
    ]
    seen: list[str | None] = []
    dataset.update_in_hdx.side_effect = lambda **_: seen.append(new[0].get("id"))
    with patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=existing):
        bare_publisher._create_or_update(dataset, "hotosm_npl_buildings", remove_additional=False)
    assert seen == ["tiles"]


def test_each_source_keeps_its_own_geoparquet_resource() -> None:
    """Both sources stage `buildings.parquet`, in different folders."""
    layers = "https://bucket.s3.amazonaws.com/ISO3/NPL/_layers/"
    existing = [
        {
            "id": "osm",
            "name": "Buildings (OSM), GeoParquet",
            "url": f"{layers}osm/buildings.parquet",
        }
    ]
    overture = [
        {"name": "Buildings (Overture), GeoParquet", "url": f"{layers}overture/buildings.parquet"}
    ]
    _reuse_resource_ids(overture, existing)
    assert overture[0].get("id") is None
    renamed = [{"name": "Structures (OSM), GeoParquet", "url": f"{layers}osm/buildings.parquet"}]
    _reuse_resource_ids(renamed, existing)
    assert renamed[0]["id"] == "osm"


def test_a_file_uploaded_to_hdx_is_matched_by_its_name_before_it_has_a_url() -> None:
    """Without S3 the file goes to HDX's own store, and the URL only exists after upload."""

    class Pending(dict):
        def get_file_to_upload(self) -> str:
            return "/tmp/out/hotosm_npl_buildings_osm.pmtiles"

    existing = [
        {
            "id": "tiles",
            "name": "hotosm_npl_buildings_osm.pmtiles",
            "url": "https://data.humdata.org/dataset/x/resource/y/download/hotosm_npl_buildings_osm.pmtiles",
        }
    ]
    new = [Pending(name="Buildings (OSM), Vector Tiles (PMTiles)")]
    _reuse_resource_ids(new, existing)
    assert new[0]["id"] == "tiles"


def _grid_cfg(*, push: bool, s3: bool, related: bool = True) -> RootConfig:
    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=push, site="prod"),
        output=OutputConfig(
            s3=S3Config(enabled=s3, bucket="bucket", prefix="ISO3", region="us-east-1")
        ),
        categories=[
            CategoryConfig(name="buildings"),
            CategoryConfig(name="roads"),
            CategoryConfig(name="health_facilities"),
        ],
    )
    cfg.output.report.related_datasets = related
    return cfg


def test_related_datasets_link_hdx_and_only_when_published_there() -> None:
    buildings = CategoryConfig(name="buildings")
    assert related_dataset_links(_grid_cfg(push=True, s3=True), buildings) == [
        ("Roads", "https://data.humdata.org/dataset/hotosm_npl_roads"),
        ("Health Facilities", "https://data.humdata.org/dataset/hotosm_npl_health_facilities"),
    ]
    # Without HDX no dataset page is published anywhere, so a link would be a dead end.
    assert related_dataset_links(_grid_cfg(push=False, s3=True), buildings) == []
    assert related_dataset_links(_grid_cfg(push=True, s3=True, related=False), buildings) == []


def test_a_purging_update_keeps_the_report_the_same_publish_rebuilds(
    hdx_demo_configuration: None, bare_publisher: HdxPublisher
) -> None:
    """Purged and re-added, the report would get a new HDX id and break links to it."""
    base = "https://bucket.s3.amazonaws.com/ISO3/NPL/buildings/"
    existing = Dataset({"id": "dataset-id", "name": "hotosm_npl_buildings", "owner_org": "org"})
    existing.add_update_resources(
        [
            Resource(
                {
                    "id": "report",
                    "package_id": "dataset-id",
                    "name": "Buildings, Data Quality Report",
                    "url": f"{base}hotosm_npl_buildings_report.html",
                    "format": "HTML",
                }
            ),
            Resource(
                {
                    "id": "stale",
                    "package_id": "dataset-id",
                    "name": "old",
                    "url": f"{base}old.geojson",
                    "format": "GeoJSON",
                }
            ),
        ],
        ignore_datasetid=True,
    )
    dataset = Dataset({"name": "hotosm_npl_buildings"})
    dataset.add_update_resource(
        Resource(
            {
                "name": "Buildings (OSM), GeoPackage",
                "url": f"{base}hotosm_npl_buildings_osm_gpkg.zip",
                "format": "GeoPackage",
            }
        )
    )
    sent: list[list[str | None]] = []

    def update_in_hdx(**_: object) -> None:
        sent.append([r.get("id") for r in dataset.get_resources()])
        for number, resource in enumerate(dataset.get_resources()):
            if not resource.get("id"):
                resource["id"] = f"assigned-{number}"

    with (
        patch("hdx.data.dataset.Dataset.read_from_hdx", return_value=existing),
        patch.object(dataset, "update_in_hdx", side_effect=update_in_hdx),
        patch.object(dataset, "reorder_resources"),
    ):
        bare_publisher._create_or_update(
            dataset,
            "hotosm_npl_buildings",
            remove_additional=True,
            keep_files=frozenset({"hotosm_npl_buildings_report.html"}),
        )
    assert sent == [[None, "report"]]


def test_a_renamed_page_keeps_its_hdx_resource(
    hdx_demo_configuration: None, bare_publisher: HdxPublisher, tmp_path: Path
) -> None:
    """HDX matches by id then name, so only the URL ties a retitled page to its resource."""
    url = "https://bucket.s3.amazonaws.com/ISO3/NPL/buildings/hotosm_npl_buildings_report.html"
    dataset = Dataset({"id": "dataset-id", "name": "hotosm_npl_buildings"})
    dataset.add_update_resource(
        Resource({"id": "report", "name": "Old title", "url": url, "format": "HTML"})
    )
    renamed = Resource({"name": "Buildings, Data Quality Report", "url": url, "format": "HTML"})
    sent: list[list[tuple[str | None, str]]] = []
    with (
        patch.object(bare_publisher, "_make_resource_for_path", return_value=renamed),
        patch.object(
            dataset,
            "update_in_hdx",
            side_effect=lambda **_: sent.append(
                [(r.get("id"), r["name"]) for r in dataset.get_resources()]
            ),
        ),
    ):
        bare_publisher._publish_html_viz(
            dataset=dataset,
            hdx_dataset_name="hotosm_npl_buildings",
            html_path=tmp_path / "hotosm_npl_buildings_report.html",
            description="report",
            iso3="NPL",
            category_slug="buildings",
            s3_config=None,
        )
    assert sent == [[("report", "Buildings, Data Quality Report")]]
