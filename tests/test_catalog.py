import pystac
import pystac.validation
import pytest

from oex.catalog import CatalogConflictError, layout, link_collections
from oex.config.schema import RootConfig

ROOT = "https://bucket.s3.amazonaws.com/ISO3/catalog.json"
NPL = "https://bucket.s3.amazonaws.com/ISO3/NPL/catalog.json"
ROADS = "https://bucket.s3.amazonaws.com/ISO3/NPL/roads/hotosm_npl_roads_osm_metadata.json"
BUILDINGS = (
    "https://bucket.s3.amazonaws.com/ISO3/NPL/buildings/hotosm_npl_buildings_osm_metadata.json"
)


class MemoryStore:
    """Versioned like S3; `meddle` runs between a read and the next write, as a rival run would."""

    def __init__(self) -> None:
        self.documents: dict[str, tuple[dict, str]] = {}
        self.meddle = None
        self.writes = 0

    def read(self, key):
        document, version = self.documents.get(key, (None, None))
        return document, version

    def write(self, key, document, version):
        if self.meddle is not None:
            meddle, self.meddle = self.meddle, None
            meddle(self)
        if self.documents.get(key, (None, None))[1] != version:
            raise CatalogConflictError(key)
        self.writes += 1
        self.documents[key] = (document, f"v{self.writes}")


def _link(cfg, store, collections, *, area_title):
    where = layout(cfg)
    assert where is not None
    link_collections(where, cfg.output.stac, store, collections, area_title=area_title)


def _cfg(*, folder: str = "") -> RootConfig:
    cfg = RootConfig(iso3="NPL", key="hotosm")
    cfg.output.stac.catalog = True
    cfg.output.s3.enabled = True
    cfg.output.s3.bucket = "bucket"
    cfg.output.s3.prefix = "ISO3"
    cfg.output.s3.region = "us-east-1"
    cfg.output.s3.folder = folder
    return cfg


def _children(store: MemoryStore, key: str) -> list[tuple[str, str]]:
    document, _ = store.read(key)
    return [(link["title"], link["href"]) for link in document["links"] if link["rel"] == "child"]


def test_the_area_catalog_sits_beside_the_data_it_lists():
    country, event = layout(_cfg()), layout(_cfg(folder="NPL_FLOOD"))
    assert country is not None and event is not None
    assert (country.root.url, country.area.url) == (ROOT, NPL)
    assert event.area.key == "ISO3/NPL_FLOOD/catalog.json"
    disabled = _cfg()
    disabled.output.stac.catalog = False
    assert layout(disabled) is None


def test_a_first_run_creates_the_area_and_root_catalogs():
    store = MemoryStore()
    _link(_cfg(), store, [("Roads of Nepal", ROADS)], area_title="Nepal")
    assert _children(store, "ISO3/NPL/catalog.json") == [("Roads of Nepal", ROADS)]
    assert _children(store, "ISO3/catalog.json") == [("Nepal", NPL)]


def test_a_later_run_adds_its_collection_and_refreshes_a_relinked_one():
    store = MemoryStore()
    _link(_cfg(), store, [("Roads", ROADS)], area_title="Nepal")
    _link(_cfg(), store, [("Buildings of Nepal", BUILDINGS)], area_title="Nepal")
    _link(_cfg(), store, [("Roads of Nepal", ROADS)], area_title="Nepal")
    assert _children(store, "ISO3/NPL/catalog.json") == [
        ("Buildings of Nepal", BUILDINGS),
        ("Roads of Nepal", ROADS),
    ]
    assert _children(store, "ISO3/catalog.json") == [("Nepal", NPL)]


def test_an_unchanged_catalog_is_not_rewritten():
    store = MemoryStore()
    _link(_cfg(), store, [("Roads of Nepal", ROADS)], area_title="Nepal")
    writes = store.writes
    _link(_cfg(), store, [("Roads of Nepal", ROADS)], area_title="Nepal")
    assert store.writes == writes


def test_a_run_writing_in_between_keeps_both_links(monkeypatch):
    """Without the version check the second write would drop the rival's Collection."""
    monkeypatch.setattr("oex.catalog.time.sleep", lambda _: None)
    store = MemoryStore()
    _link(_cfg(), store, [("Roads of Nepal", ROADS)], area_title="Nepal")
    store.meddle = lambda s: _link(
        _cfg(), s, [("Buildings of Nepal", BUILDINGS)], area_title="Nepal"
    )
    _link(
        _cfg(),
        store,
        [("Roads of Nepal", ROADS), ("Waterways of Nepal", ROADS + "?w")],
        area_title="Nepal",
    )
    assert [title for title, _ in _children(store, "ISO3/NPL/catalog.json")] == [
        "Buildings of Nepal",
        "Roads of Nepal",
        "Waterways of Nepal",
    ]


def test_a_catalog_that_keeps_changing_is_reported(monkeypatch):
    monkeypatch.setattr("oex.catalog.time.sleep", lambda _: None)

    class Contended(MemoryStore):
        def write(self, key, document, version):
            raise CatalogConflictError(key)

    with pytest.raises(CatalogConflictError, match="still changing"):
        _link(_cfg(), Contended(), [("Roads", ROADS)], area_title="Nepal")


@pytest.mark.network
def test_the_catalogs_satisfy_the_stac_spec():
    store = MemoryStore()
    _link(_cfg(), store, [("Roads of Nepal", ROADS)], area_title="Nepal")
    for key in ("ISO3/catalog.json", "ISO3/NPL/catalog.json"):
        document, _ = store.read(key)
        pystac.validation.validate_dict(document)


def _stubbed_store():
    from botocore.stub import Stubber

    from oex.catalog import S3CatalogStore

    store = S3CatalogStore(_cfg())
    return store, Stubber(store._client)


def test_s3_writes_only_when_the_catalog_is_unchanged():
    """S3's conditional writes are what keep two runs from overwriting each other's link."""
    from botocore.stub import ANY

    store, stub = _stubbed_store()
    common = {
        "Bucket": "bucket",
        "Key": "k",
        "Body": ANY,
        "ContentType": "application/json",
        "ACL": ANY,
    }
    stub.add_response("put_object", {}, {**common, "IfNoneMatch": "*"})
    stub.add_response("put_object", {}, {**common, "IfMatch": '"v1"'})
    stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
    with stub:
        store.write("k", {"links": []}, None)
        store.write("k", {"links": []}, '"v1"')
        with pytest.raises(CatalogConflictError):
            store.write("k", {"links": []}, '"v1"')


def test_an_activation_registers_under_its_group_and_the_group_under_the_root():
    cfg = _cfg(folder="NPL_FLOOD")
    cfg.output.stac.group = "activations"
    store = MemoryStore()
    _link(cfg, store, [("Roads", ROADS)], area_title="Nepal Flood 2026")
    flood = "https://bucket.s3.amazonaws.com/ISO3/NPL_FLOOD/catalog.json"
    group = "https://bucket.s3.amazonaws.com/ISO3/activations/catalog.json"
    assert _children(store, "ISO3/activations/catalog.json") == [("Nepal Flood 2026", flood)]
    assert _children(store, "ISO3/catalog.json") == [("Activations", group)]
    area, _ = store.read("ISO3/NPL_FLOOD/catalog.json")
    assert [link["href"] for link in area["links"] if link["rel"] == "parent"] == [group]
    _link(_cfg(), store, [("Roads of Nepal", ROADS)], area_title="Nepal")
    assert [title for title, _ in _children(store, "ISO3/catalog.json")] == ["Activations", "Nepal"]
