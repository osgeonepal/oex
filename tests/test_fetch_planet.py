from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from oex.osm.fetch_planet import download_pbf

URL = "https://example.com/nepal-latest.osm.pbf"
BODY = b"0123456789"


class _Head:
    headers = {"Content-Length": str(len(BODY)), "Last-Modified": "Mon, 05 Oct 2026"}

    def raise_for_status(self) -> None:
        pass


class _Stream:
    def __init__(self, chunks: list[bytes], fail_after: bool = False) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int):
        yield from self._chunks
        if self._fail_after:
            raise requests.ConnectionError("connection dropped")


def test_an_interrupted_download_leaves_nothing_at_the_target(tmp_path: Path) -> None:
    """A run that finds the target reuses it as a complete extract."""
    with (
        patch("oex.http.session.head", return_value=_Head()),
        patch("oex.osm.fetch_planet.requests.get", return_value=_Stream([BODY[:4]], True)),
        pytest.raises(requests.ConnectionError),
    ):
        download_pbf(URL, tmp_path)
    assert not (tmp_path / "nepal-latest.osm.pbf").exists()


def test_a_partial_download_resumes_into_the_target(tmp_path: Path) -> None:
    (tmp_path / "nepal-latest.osm.pbf.part").write_bytes(BODY[:4])
    with (
        patch("oex.http.session.head", return_value=_Head()),
        patch("oex.osm.fetch_planet.requests.get", return_value=_Stream([BODY[4:]])) as get,
    ):
        result = download_pbf(URL, tmp_path)
    assert get.call_args.kwargs["headers"] == {"Range": "bytes=4-"}
    assert result.path.read_bytes() == BODY
    assert not (tmp_path / "nepal-latest.osm.pbf.part").exists()


class _Sidecar:
    text = "0" * 32 + "  nepal-latest.osm.pbf\n"

    def raise_for_status(self) -> None:
        pass


def test_a_download_that_fails_its_md5_is_discarded(tmp_path: Path) -> None:
    """A resume can join two versions of the extract; keeping it would fail every later run."""
    with (
        patch("oex.http.session.head", return_value=_Head()),
        patch("oex.osm.fetch_planet.requests.get", return_value=_Stream([BODY])),
        patch("oex.http.session.get", return_value=_Sidecar()),
        pytest.raises(RuntimeError, match="md5 mismatch"),
    ):
        download_pbf(URL, tmp_path, md5_url=URL + ".md5")
    assert list(tmp_path.iterdir()) == []
