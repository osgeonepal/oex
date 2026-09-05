"""`oex-cli metadata --prune` is the only path that deletes published HDX resources."""

from pathlib import Path

from oex.config.loader import load_config
from oex.hdx_publisher import HdxPublisher

CONFIG = """
iso3: NPL
key: hot
hdx:
  push: true
  combine: true
  combined:
    name: hot_npl
categories:
  - name: buildings
    osm:
      enabled: true
      filter:
        building: true
  - name: roads
    osm:
      enabled: false
    overture:
      enabled: true
"""


class FakeResource(dict):
    def __init__(self, url: str, name: str = "n", description: str = "d"):
        super().__init__(url=url, name=name, description=description)
        self.deleted = False
        self.updated = False

    def delete_from_hdx(self):
        self.deleted = True

    def update_in_hdx(self):
        self.updated = True

    def mark_data_updated(self):
        pass


class FakeDataset(dict):
    def __init__(self, resources):
        super().__init__()
        self._resources = resources

    def get_resources(self):
        return self._resources

    def add_tags(self, tags):
        pass

    def set_expected_update_frequency(self, frequency):
        pass

    def update_in_hdx(self, **kwargs):
        pass


def url_for(name: str) -> str:
    return f"https://s3.example/ISO3/NPL/combined/{name}"


def load_config_from(text: str):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "c.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)


def run_prune(monkeypatch, resources, *, prune: bool, dry_run: bool = False):
    for name, value in (("HDX_API_KEY", "k"), ("HDX_OWNER_ORG", "o"), ("HDX_MAINTAINER", "m")):
        monkeypatch.setenv(name, value)
    cfg = load_config_from(CONFIG)
    dataset = FakeDataset(resources)

    import oex.hdx_publisher as hp

    monkeypatch.setattr(hp, "country_name", lambda *a, **k: "Nepal")
    monkeypatch.setattr(hp, "_hdx_publish_with_retry", lambda fn, label: fn())
    monkeypatch.setattr(HdxPublisher, "__init__", lambda self, hdx_cfg: None)
    monkeypatch.setattr(HdxPublisher, "_sort_resources_by_name", lambda self, d, n: None)

    publisher = HdxPublisher(cfg.hdx)

    class FakeDatasetClass:
        @staticmethod
        def read_from_hdx(name):
            return dataset

    monkeypatch.setitem(
        __import__("sys").modules, "hdx.data.dataset", type("m", (), {"Dataset": FakeDatasetClass})
    )
    return publisher.update_metadata(cfg, dry_run=dry_run, prune=prune)


def test_a_disabled_source_is_pruned(monkeypatch):
    """roads has osm.enabled false, so its OSM resource no longer belongs to the dataset."""
    keep = FakeResource(url_for("hot_npl_buildings_osm_gpkg.zip"))
    drop = FakeResource(url_for("hot_npl_roads_osm_gpkg.zip"))
    _, _, pruned = run_prune(monkeypatch, [keep, drop], prune=True)
    assert pruned == 1
    assert drop.deleted is True
    assert keep.deleted is False


def test_an_enabled_source_is_never_pruned(monkeypatch):
    keep = FakeResource(url_for("hot_npl_roads_overture_gpkg.zip"))
    _, _, pruned = run_prune(monkeypatch, [keep], prune=True)
    assert pruned == 0
    assert keep.deleted is False


def test_nothing_is_deleted_without_the_prune_flag(monkeypatch):
    drop = FakeResource(url_for("hot_npl_roads_osm_gpkg.zip"))
    _, _, pruned = run_prune(monkeypatch, [drop], prune=False)
    assert pruned == 0
    assert drop.deleted is False


def test_a_dry_run_reports_the_prune_without_doing_it(monkeypatch):
    drop = FakeResource(url_for("hot_npl_roads_osm_gpkg.zip"))
    _, _, pruned = run_prune(monkeypatch, [drop], prune=True, dry_run=True)
    assert pruned == 1
    assert drop.deleted is False


def test_a_resource_that_is_not_a_layer_is_left_alone(monkeypatch):
    """The AOI, the map preview and hand-added resources must survive a prune."""
    aoi = FakeResource(url_for("hot_npl_aoi.geojson"))
    foreign = FakeResource("https://example.org/somebody-elses-file.csv")
    _, _, pruned = run_prune(monkeypatch, [aoi, foreign], prune=True)
    assert pruned == 0
    assert aoi.deleted is False
    assert foreign.deleted is False
