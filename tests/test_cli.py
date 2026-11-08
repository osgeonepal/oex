"""CLI argument disambiguation."""

import logging
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


def _fake_result(source_name: str, failed: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        iso3="COD",
        source_name=source_name,
        succeeded=1,
        unchanged=0,
        empty=0,
        skipped=0,
        failed=failed,
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


@pytest.mark.parametrize("download_if_missing", [True, False])
def test_overrides_download_if_missing_sets_auto_download_to_the_flag(
    download_if_missing: bool,
) -> None:
    overrides = _build_overrides("NPL", None, None, download_if_missing=download_if_missing)
    assert overrides["source.osm.auto_download_planet"] is download_if_missing


def test_overrides_download_if_missing_unset_does_not_appear() -> None:
    overrides = _build_overrides("NPL", None, None)
    assert "source.osm.auto_download_planet" not in overrides


def test_overrides_explicit_iso3_flag_overrides_positional() -> None:
    overrides = _build_overrides("WRONG", None, None, iso3="COD")
    assert overrides["iso3"] == "COD"


def test_overrides_explicit_iso3_lowercase_is_normalised() -> None:
    overrides = _build_overrides(None, None, None, iso3="npl")
    assert overrides["iso3"] == "NPL"


def test_overrides_dataset_name_unset_does_not_appear() -> None:
    overrides = _build_overrides("NPL", None, None)
    assert "dataset_name" not in overrides


def test_overrides_dataset_name_empty_string_appears() -> None:
    # Empty string is a meaningful "clear it" signal, distinct from omission.
    overrides = _build_overrides("NPL", None, None, dataset_name="")
    assert "dataset_name" in overrides
    assert overrides["dataset_name"] == ""


@pytest.mark.parametrize(
    ("osm", "overture", "expected"),
    [(True, True, ["osm", "overture"]), (False, True, ["overture"]), (False, False, [])],
)
def test_enabled_sources_lists_only_enabled_sources_in_run_order(
    monkeypatch, osm: bool, overture: bool, expected: list[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _p, _o: _cfg(osm=osm, overture=overture))
    assert _enabled_sources(None, {}) == expected


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


def test_iso3_from_the_command_line_satisfies_checks_on_a_config_without_one(tmp_path) -> None:
    """The sweep passes the country as --iso3 over a base config that names none."""
    config = tmp_path / "base.yaml"
    config.write_text("key: t\nsource:\n  pcodes:\n    enabled: true\n", encoding="utf-8")
    overrides = _build_overrides(None, None, None, iso3="npl")
    assert cli._load_with_overrides(config, overrides).iso3 == "NPL"


def test_metadata_applies_iso3_before_checking_the_config(tmp_path) -> None:
    config = tmp_path / "base.yaml"
    config.write_text("key: t\nsource:\n  pcodes:\n    enabled: true\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["metadata", "--config", str(config), "--iso3", "NPL"])
    assert result.exit_code == 0, result.output
    assert "no hdx.combined.name, skipping" in result.output


def test_log_level_option_sets_the_root_logger_level(monkeypatch) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)
    result = CliRunner().invoke(app, ["--log-level", "DEBUG", "metadata", "--help"])
    assert result.exit_code == 0, result.output
    assert root.level == logging.DEBUG


@pytest.mark.parametrize("dataset_name", ["Nepal: Koshi", "[Region] A", "No"])
def test_dataset_name_flag_is_kept_as_the_literal_string(dataset_name: str) -> None:
    cfg = cli._load_with_overrides(
        None, _build_overrides(None, None, None, dataset_name=dataset_name)
    )
    assert cfg.dataset_name == dataset_name


@pytest.mark.parametrize(
    ("args", "failed_per_run", "expected_exit"),
    [
        (["osm", "COD", "--config", "x.yaml"], [0], 0),
        (["osm", "COD", "--config", "x.yaml"], [1], 1),
        (["overture", "COD", "--config", "x.yaml"], [2], 1),
        (["file", "--config", "x.yaml"], [1], 1),
        (["all", "COD", "--config", "x.yaml"], [0, 0], 0),
        (["all", "COD", "--config", "x.yaml"], [0, 1], 1),
    ],
    ids=[
        "osm clean",
        "osm failure",
        "overture failure",
        "file failure",
        "all clean",
        "all with the second source failing",
    ],
)
def test_an_export_command_exits_non_zero_when_any_category_failed(
    monkeypatch, args: list[str], failed_per_run: list[int], expected_exit: int
) -> None:
    """Unattended sweeps read only the exit code, so a failed category must not exit 0."""
    failures = iter(failed_per_run)
    monkeypatch.setattr(cli, "_enabled_sources", lambda _y, _o: ["osm", "overture"])
    monkeypatch.setattr(cli, "_run_one", lambda *_: _fake_result("s", failed=next(failures)))
    result = CliRunner().invoke(app, args)
    assert result.exit_code == expected_exit, result.output
    assert next(failures, None) is None


def test_metadata_exits_non_zero_when_a_dataset_update_fails(monkeypatch, tmp_path) -> None:
    config = tmp_path / "base.yaml"
    config.write_text("key: t\nhdx:\n  combined:\n    name: hot_npl\n", encoding="utf-8")

    class _RefusingPublisher:
        def __init__(self, hdx_config: object) -> None:
            pass

        def update_metadata(self, cfg: object, *, dry_run: bool, prune: bool) -> None:
            raise RuntimeError("dataset owned by another organisation")

    monkeypatch.setattr("oex.hdx_publisher.HdxPublisher", _RefusingPublisher)
    result = CliRunner().invoke(app, ["metadata", "--config", str(config), "--iso3", "NPL"])
    assert result.exit_code == 1
    assert "dataset owned by another organisation" in result.output
