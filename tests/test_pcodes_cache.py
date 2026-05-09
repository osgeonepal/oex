"""Tests for fieldmaps admin parquet caching.

Network calls are intercepted with a fake `requests` shim so the suite
stays offline.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from oex.pcodes import cache as cache_mod
from oex.pcodes.cache import PcodeCacheError, ensure_admin_parquets


class _FakeResponse:
    def __init__(
        self,
        *,
        json_payload: Any | None = None,
        body: bytes | None = None,
        status: int = 200,
    ) -> None:
        self._json = json_payload
        self._body = body or b""
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> Any:
        return self._json

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        view = memoryview(self._body)
        for offset in range(0, len(view), chunk_size):
            yield bytes(view[offset : offset + chunk_size])

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@contextmanager
def _patched_requests(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest: list[dict[str, Any]],
    parquet_bytes: bytes,
    download_log: list[str],
) -> Iterator[None]:
    def fake_get(url: str, *args: Any, **kwargs: Any) -> _FakeResponse:
        if url.endswith(".json"):
            return _FakeResponse(json_payload=manifest)
        if url.endswith(".parquet"):
            download_log.append(url)
            return _FakeResponse(body=parquet_bytes)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(cache_mod.requests, "get", fake_get)
    yield


def _manifest(group: str, level: int, date: str) -> dict[str, Any]:
    return {
        "id": f"{group}_intl_adm{level}",
        "grp": group,
        "wld": "intl",
        "adm": level,
        "date": date,
        "a_parquet": (
            f"https://data.fieldmaps.io/edge-matched/{group}/intl/adm{level}_polygons.parquet"
        ),
    }


def test_ensure_admin_parquets_downloads_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = [_manifest("humanitarian", 1, "2025-07-29")]
    log: list[str] = []
    with _patched_requests(monkeypatch, manifest=manifest, parquet_bytes=b"PARQ", download_log=log):
        entries = ensure_admin_parquets(
            cache_dir=tmp_path,
            levels=[1],
            manifest_url="https://x/edge-matched.json",
            parquet_url_template=(
                "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
            ),
            manifest_group="humanitarian",
        )

    assert set(entries) == {1}
    assert entries[1].path == tmp_path / "adm1_polygons.parquet"
    assert entries[1].path.read_bytes() == b"PARQ"
    assert entries[1].upstream_date == "2025-07-29"
    assert log == ["https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm1_polygons.parquet"]

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["levels"]["1"]["date"] == "2025-07-29"


def test_ensure_admin_parquets_skips_when_date_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = [_manifest("humanitarian", 2, "2025-07-29")]
    log: list[str] = []

    def call() -> None:
        ensure_admin_parquets(
            cache_dir=tmp_path,
            levels=[2],
            manifest_url="https://x/edge-matched.json",
            parquet_url_template=(
                "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
            ),
            manifest_group="humanitarian",
        )

    with _patched_requests(monkeypatch, manifest=manifest, parquet_bytes=b"v1", download_log=log):
        call()
        call()

    assert log == ["https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm2_polygons.parquet"]


def test_ensure_admin_parquets_redownloads_when_date_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    with _patched_requests(
        monkeypatch,
        manifest=[_manifest("humanitarian", 1, "2025-07-29")],
        parquet_bytes=b"old",
        download_log=log,
    ):
        ensure_admin_parquets(
            cache_dir=tmp_path,
            levels=[1],
            manifest_url="https://x/edge-matched.json",
            parquet_url_template=(
                "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
            ),
            manifest_group="humanitarian",
        )
    assert (tmp_path / "adm1_polygons.parquet").read_bytes() == b"old"

    with _patched_requests(
        monkeypatch,
        manifest=[_manifest("humanitarian", 1, "2025-08-05")],
        parquet_bytes=b"new",
        download_log=log,
    ):
        ensure_admin_parquets(
            cache_dir=tmp_path,
            levels=[1],
            manifest_url="https://x/edge-matched.json",
            parquet_url_template=(
                "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
            ),
            manifest_group="humanitarian",
        )
    assert (tmp_path / "adm1_polygons.parquet").read_bytes() == b"new"
    assert len(log) == 2


def test_ensure_admin_parquets_raises_when_level_missing_from_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    with _patched_requests(
        monkeypatch,
        manifest=[_manifest("humanitarian", 1, "2025-07-29")],
        parquet_bytes=b"x",
        download_log=log,
    ):
        with pytest.raises(PcodeCacheError, match="adm=4"):
            ensure_admin_parquets(
                cache_dir=tmp_path,
                levels=[1, 4],
                manifest_url="https://x/edge-matched.json",
                parquet_url_template=(
                    "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
                ),
                manifest_group="humanitarian",
            )
