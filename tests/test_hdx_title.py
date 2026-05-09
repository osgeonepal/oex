"""Title-resolution helpers for HDX dataset publication."""

from oex.config.schema import CategoryConfig, CategoryHdx, HdxConfig, RootConfig
from oex.hdx_publisher import _country_name, _resolve_title, _title_case_category


def test_title_case_capitalises_simple_categories() -> None:
    assert _title_case_category("buildings") == "Buildings"
    assert _title_case_category("railways") == "Railways"


def test_title_case_handles_multi_word() -> None:
    assert _title_case_category("education_facilities") == "Education Facilities"
    assert _title_case_category("health_facilities") == "Health Facilities"
    assert _title_case_category("populated_places") == "Populated Places"


def test_title_case_keeps_filler_words_lowercase() -> None:
    assert _title_case_category("points_of_interest") == "Points of Interest"


def test_country_name_resolves_known_iso3() -> None:
    assert _country_name("NPL") == "Nepal"
    assert _country_name("npl") == "Nepal"
    assert _country_name("VNM") == "Viet Nam"


def test_country_name_falls_back_to_iso3_for_unknown() -> None:
    assert _country_name("ZZZ") == "ZZZ"


def _cfg(*, iso3: str, template: str = "", dataset_name: str | None = None) -> RootConfig:
    return RootConfig(
        iso3=iso3,
        dataset_name=dataset_name,
        hdx=HdxConfig(title_template=template),
    )


def test_resolve_title_uses_template_when_set() -> None:
    cfg = _cfg(iso3="NPL", template="{country} {category} (OpenStreetMap Export)")
    cat = CategoryConfig(name="buildings", hdx=CategoryHdx(title=None))
    assert _resolve_title(cfg, cat) == "Nepal Buildings (OpenStreetMap Export)"


def test_resolve_title_template_lowercases_filler_in_category() -> None:
    cfg = _cfg(iso3="NPL", template="{country} {category} (OpenStreetMap Export)")
    cat = CategoryConfig(name="points_of_interest")
    assert _resolve_title(cfg, cat) == "Nepal Points of Interest (OpenStreetMap Export)"


def test_resolve_title_falls_back_to_legacy_format_when_no_template() -> None:
    cfg = _cfg(iso3="NPL", dataset_name="Nepal")
    cat = CategoryConfig(name="Buildings")
    assert _resolve_title(cfg, cat) == "Buildings of Nepal"


def test_resolve_title_legacy_uses_iso3_when_dataset_name_unset() -> None:
    cfg = _cfg(iso3="NPL")
    cat = CategoryConfig(name="Buildings")
    assert _resolve_title(cfg, cat) == "Buildings of NPL"
