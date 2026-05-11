"""CLI argument disambiguation."""

from pathlib import Path

from oex.cli import _build_overrides, _resolve_args

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
