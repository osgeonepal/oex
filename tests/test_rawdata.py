"""Raw Data API engine tests: filter translation, polling, and row extraction.

Network calls are stubbed. The parquet contract itself is covered by
tests/test_country_parquet.py, which both live engines share.
"""

import json
from pathlib import Path

import pytest

from oex.osm import rawdata
from oex.osm.errors import OsmEngineUnavailableError
from oex.osm.rawdata import _rows, _wait_for, build_filter

SQUARE = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [[[85.0, 27.0], [85.1, 27.0], [85.1, 27.1], [85.0, 27.1], [85.0, 27.0]]],
    }
)


def test_true_becomes_an_empty_value_list() -> None:
    """The API spells "any value for this key" as an empty list."""
    assert build_filter({"building": True}) == {
        "tags": {"all_geometry": {"join_or": {"building": []}}}
    }


def test_a_value_list_is_passed_through() -> None:
    assert build_filter({"amenity": ["police", "bank"]})["tags"]["all_geometry"]["join_or"] == {
        "amenity": ["police", "bank"]
    }


def test_a_bare_string_becomes_a_single_value_list() -> None:
    assert build_filter({"place": "town"})["tags"]["all_geometry"]["join_or"] == {"place": ["town"]}


def test_false_values_are_skipped() -> None:
    assert build_filter({"building": True, "highway": False})["tags"]["all_geometry"][
        "join_or"
    ] == {"building": []}


def test_a_filter_with_nothing_usable_fails_loud() -> None:
    with pytest.raises(ValueError, match="non-empty osm.filter"):
        build_filter({"highway": False})


def test_empty_filter_fails_loud() -> None:
    with pytest.raises(ValueError, match="non-empty osm.filter"):
        build_filter({})


_POINT = {"type": "Point", "coordinates": [85, 27]}


def _feature(osm_id, osm_type, tags=None, geometry=_POINT):
    return {
        "type": "Feature",
        "properties": {"osm_id": osm_id, "osm_type": osm_type, "tags": tags or {}},
        "geometry": geometry,
    }


def test_rows_carry_the_id_prefix_and_tags(tmp_path: Path) -> None:
    path = tmp_path / "export.geojson"
    path.write_text(
        json.dumps(
            {
                "features": [
                    _feature(1, "ways_poly", {"building": "yes"}),
                    _feature(2, "nodes", {"amenity": "police"}),
                    _feature(3, "relations"),
                ]
            }
        ),
        encoding="utf-8",
    )
    rows = _rows(path)
    assert [r[0] for r in rows] == ["way/1", "node/2", "relation/3"]
    assert json.loads(rows[0][1]) == {"building": "yes"}
    assert rows[0][2].startswith("POINT")


def test_rows_skip_features_without_geometry_or_id(tmp_path: Path) -> None:
    path = tmp_path / "export.geojson"
    path.write_text(
        json.dumps(
            {
                "features": [
                    _feature(1, "ways_poly"),
                    _feature(2, "ways_poly", geometry=None),
                    {"type": "Feature", "properties": {"osm_type": "nodes"}, "geometry": None},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert [r[0] for r in _rows(path)] == ["way/1"]


def test_an_unknown_osm_type_fails_loud(tmp_path: Path) -> None:
    """Guessing the primitive would mint a feature id that collides with a real one."""
    path = tmp_path / "export.geojson"
    path.write_text(json.dumps({"features": [_feature(9, "something_new")]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unknown osm_type"):
        _rows(path)


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_wait_for_returns_the_download_url(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter(
        [
            _Response({"status": "STARTED"}),
            _Response({"status": "SUCCESS", "result": {"download_url": "https://x/y.zip"}}),
        ]
    )
    monkeypatch.setattr(rawdata.requests, "get", lambda *a, **k: next(states))
    monkeypatch.setattr(rawdata.time, "sleep", lambda _s: None)
    assert _wait_for("https://api", "abc", 60) == "https://x/y.zip"


def test_wait_for_raises_when_the_job_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rawdata.requests, "get", lambda *a, **k: _Response({"status": "FAILURE"}))
    monkeypatch.setattr(rawdata.time, "sleep", lambda _s: None)
    with pytest.raises(OsmEngineUnavailableError, match="ended as FAILURE"):
        _wait_for("https://api", "abc", 60)


def test_wait_for_raises_on_a_success_without_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rawdata.requests, "get", lambda *a, **k: _Response({"status": "SUCCESS", "result": {}})
    )
    monkeypatch.setattr(rawdata.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError, match="no download url"):
        _wait_for("https://api", "abc", 60)


def test_wait_for_gives_up_rather_than_polling_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadline has to be re-checked between polls, not only on entry."""
    polls = 0

    def never_finishes(*_args, **_kwargs):
        nonlocal polls
        polls += 1
        return _Response({"status": "STARTED"})

    clock = iter([0.0, 1.0, 2.0, 99.0, 99.0])
    monkeypatch.setattr(rawdata.requests, "get", never_finishes)
    monkeypatch.setattr(rawdata.time, "sleep", lambda _s: None)
    monkeypatch.setattr(rawdata.time, "time", lambda: next(clock))
    with pytest.raises(OsmEngineUnavailableError, match="did not finish within"):
        _wait_for("https://api", "abc", 10)
    assert polls > 0


def test_submit_asks_for_intersecting_features(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API defaults to ST_WITHIN, which would drop everything crossing the boundary."""
    sent: dict = {}

    def fake_post(url, json=None, timeout=None):  # noqa: A002 - requests' own kwarg
        sent.update(json or {})
        return _Response({"task_id": "t-1"})

    monkeypatch.setattr(rawdata.requests, "post", fake_post)
    assert rawdata._submit("https://api", SQUARE, {"building": True}, 30) == "t-1"
    assert sent["useStWithin"] is False
    assert sent["outputType"] == "geojson"


def test_submit_fails_loud_on_an_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rawdata.requests, "post", lambda *a, **k: _Response({"detail": "nope"}, status=503)
    )
    with pytest.raises(OsmEngineUnavailableError, match="HTTP 503"):
        rawdata._submit("https://api", SQUARE, {"building": True}, 30)


def test_submit_fails_loud_when_no_task_id_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rawdata.requests, "post", lambda *a, **k: _Response({"queue": 0}))
    with pytest.raises(RuntimeError, match="no task id"):
        rawdata._submit("https://api", SQUARE, {"building": True}, 30)
