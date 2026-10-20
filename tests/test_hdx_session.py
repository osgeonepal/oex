"""Setting up the HDX client, which hdx-python-api holds as one global configuration."""

import pytest
from hdx.api.configuration import Configuration

import oex.hdx_publisher as hdx_publisher
from oex.config.schema import HdxConfig
from oex.hdx_publisher import HdxPublisher


@pytest.fixture
def hdx_session_without_preflight(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hdx_publisher, "_preflight_check", lambda *args, **kwargs: None)
    yield
    Configuration.delete()


def _hdx_config(site: str) -> HdxConfig:
    return HdxConfig(
        push=True, site=site, api_key="key", owner_org="org", maintainer="me", user_agent="test"
    )


def test_a_second_publisher_in_one_process_takes_over_the_session(
    hdx_session_without_preflight,
) -> None:
    """`oex-cli all` publishes OSM then Overture from one process."""
    HdxPublisher(_hdx_config("stage"))
    HdxPublisher(_hdx_config("prod"))
    assert Configuration.read().get_hdx_site_url() == "https://data.humdata.org"
