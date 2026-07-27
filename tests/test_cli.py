"""CLI argument disambiguation."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import oex.cli as cli
from oex.cli import _build_overrides, _enabled_sources, _resolve_args, app
from oex.config.schema import RootConfig

CFG = Path("dummy.yaml")


def _cfg(osm: bool, overture: bool) -> RootConfig:
    cfg = RootConfig(iso3="COD", key="hot")
    cfg.source["osm"].enabled = osm
    cfg.source["overture"].enabled = overture
    return cfg


def _fake_result(source_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        iso3="COD",
        source_name=source_name,
        succeeded=1,
        empty=0,
        skipped=0,
        failed=0,
        total_duration_s=1.0,
    )


def test_resolve_no_config_passes_through() -> None:
    assert _resolve_args("NPL", None, None, None) == ("NPL", None)
    assert _resolve_args("NPL", "buildings", None, None) == ("NPL", "buildings")
    assert _resolve_args(None, None, None, None) == (None, None)


def test_resolve_with_config_treats_uppercase_iso3_as_iso3() -> None:
    assert _resolve_args("NPL", None, None, CFG) == ("NPL", None)
    assert _resolve_args("VNM", None, None, CFG) == ("VNM", None)


def test_resolve_with_config_treats_non_iso3_as_theme() -> None:
    assert _resolve_args("buildings", None, None, CFG) == (None, "buildings")
    assert _resolve_args("hot", None, None, CFG) == (None, "hot")
    assert _resolve_args("npl", None, None, CFG) == (None, "npl")


def test_resolve_with_config_keeps_both_when_passed() -> None:
    assert _resolve_args("NPL", "buildings", None, CFG) == ("NPL", "buildings")


def test_resolve_with_configs_dir_behaves_like_config() -> None:
    assert _resolve_args("NPL", None, Path("configs/"), None) == ("NPL", None)
    assert _resolve_args("buildings", None, Path("configs/"), None) == (None, "buildings")


def test_overrides_download_if_missing_true_sets_auto_download() -> None:
    overrides = _build_overrides("NPL", None, None, download_if_missing=True)
    assert overrides["source.osm.auto_download_planet"] is True


def test_overrides_download_if_missing_false_disables_auto_download() -> None:
    overrides = _build_overrides("NPL", None, None, download_if_missing=False)
    assert overrides["source.osm.auto_download_planet"] is False


def test_overrides_download_if_missing_unset_does_not_appear() -> None:
    overrides = _build_overrides("NPL", None, None)
    assert "source.osm.auto_download_planet" not in overrides


def test_overrides_explicit_iso3_flag_overrides_positional() -> None:
    overrides = _build_overrides("WRONG", None, None, iso3="COD")
    assert overrides["iso3"] == "COD"


def test_overrides_explicit_iso3_lowercase_is_normalised() -> None:
    overrides = _build_overrides(None, None, None, iso3="npl")
    assert overrides["iso3"] == "NPL"


def test_overrides_dataset_name_flag_sets_field() -> None:
    overrides = _build_overrides("COD", None, None, dataset_name="Democratic Republic of the Congo")
    assert overrides["dataset_name"] == "Democratic Republic of the Congo"


def test_overrides_dataset_name_unset_does_not_appear() -> None:
    overrides = _build_overrides("NPL", None, None)
    assert "dataset_name" not in overrides


def test_overrides_dataset_name_empty_string_appears() -> None:
    # Empty string is a meaningful "clear it" signal, distinct from omission.
    overrides = _build_overrides("NPL", None, None, dataset_name="")
    assert "dataset_name" in overrides
    assert overrides["dataset_name"] == ""


def test_enabled_sources_returns_both_in_run_order(monkeypatch) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _p: _cfg(osm=True, overture=True))
    assert _enabled_sources(None, {}) == ["osm", "overture"]


def test_enabled_sources_skips_disabled_source(monkeypatch) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _p: _cfg(osm=False, overture=True))
    assert _enabled_sources(None, {}) == ["overture"]


def test_enabled_sources_empty_when_none_enabled(monkeypatch) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _p: _cfg(osm=False, overture=False))
    assert _enabled_sources(None, {}) == []


def test_all_runs_each_enabled_source_in_order(monkeypatch) -> None:
    factories: list[object] = []
    monkeypatch.setattr(cli, "_enabled_sources", lambda _y, _o: ["osm", "overture"])
    monkeypatch.setattr(
        cli,
        "_run_one",
        lambda _y, _o, _t, factory: factories.append(factory) or _fake_result("s"),
    )
    result = CliRunner().invoke(app, ["all", "COD", "--config", "x.yaml"])
    assert result.exit_code == 0
    assert factories == [cli.OsmRunner, cli.OvertureRunner]


def test_all_errors_when_no_source_enabled(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_enabled_sources", lambda _y, _o: [])
    monkeypatch.setattr(cli, "_run_one", lambda *a: pytest.fail("must not run any source"))
    result = CliRunner().invoke(app, ["all", "COD", "--config", "x.yaml"])
    assert result.exit_code != 0


def test_all_propagates_overrides_to_each_run(monkeypatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(cli, "_enabled_sources", lambda _y, _o: ["osm"])
    monkeypatch.setattr(
        cli,
        "_run_one",
        lambda _y, overrides, _t, _f: seen.append(overrides) or _fake_result("osm"),
    )
    result = CliRunner().invoke(app, ["all", "COD", "--config", "x.yaml", "--no-hdx-push"])
    assert result.exit_code == 0
    assert seen[0]["hdx.push"] is False
