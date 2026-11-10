"""HDX site names resolve through hdx-python-api's site list."""

import pytest
from hdx.api.configuration import ConfigurationError

import oex.hdx_publisher as hdx_publisher
from oex.hdx_publisher import _preflight_check, hdx_dataset_url, hdx_site_url


@pytest.fixture(autouse=True)
def _site_urls_from_the_base_configuration(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HDX_URL", raising=False)
    hdx_site_url.cache_clear()
    yield
    hdx_site_url.cache_clear()


class _Response:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.status_code = 200
        self.text = ""

    def json(self) -> dict:
        return self._body


def test_the_preflight_check_talks_to_the_hdx_dev_site(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def post(url: str, **_: object) -> _Response:
        requested.append(url)
        if url.endswith("organization_list_for_user"):
            return _Response({"success": True, "result": [{"name": "hot", "id": "1"}]})
        return _Response({"success": True, "result": {"name": "me"}})

    monkeypatch.setattr(hdx_publisher.requests, "post", post)

    _preflight_check("key", "hot", "me", "dev")

    assert requested == [
        "https://dev.data-humdata-org.ahconu.org/api/3/action/organization_list_for_user",
        "https://dev.data-humdata-org.ahconu.org/api/3/action/user_show",
    ]


def test_an_unknown_hdx_site_fails_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []
    monkeypatch.setattr(hdx_publisher.requests, "post", lambda url, **_: requested.append(url))

    with pytest.raises(ConfigurationError, match="hdx_nope_site"):
        _preflight_check("key", "hot", "me", "nope")

    assert requested == []


def test_a_dataset_on_the_dev_site_links_to_its_dev_page() -> None:
    assert (
        hdx_dataset_url("dev", "hotosm_btn_roads")
        == "https://dev.data-humdata-org.ahconu.org/dataset/hotosm_btn_roads"
    )
