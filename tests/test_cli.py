"""CLI argument disambiguation."""

from pathlib import Path

from oex.cli import _resolve_args

CFG = Path("dummy.yaml")


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
