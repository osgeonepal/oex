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


def test_country_name_prefers_common_name_over_iso_inversion() -> None:
    # ISO name is "Viet Nam"; common usage is "Vietnam".
    assert _country_name("VNM") == "Vietnam"
    # ISO name is "Iran, Islamic Republic of"; common is "Iran".
    assert _country_name("IRN") == "Iran"
    # ISO name is "Korea, Republic of"; common is "South Korea".
    assert _country_name("KOR") == "South Korea"
    # ISO name is "Tanzania, United Republic of"; common is "Tanzania".
    assert _country_name("TZA") == "Tanzania"


def test_country_name_uses_dataset_name_when_provided() -> None:
    # dataset_name wins over both pycountry common_name and ISO inversion.
    # This is the configurable knob users set in YAML or via CLI to handle
    # countries pycountry mangles (DRC, etc.) and sub-national exports.
    assert (
        _country_name("COD", dataset_name="Democratic Republic of the Congo")
        == "Democratic Republic of the Congo"
    )
    assert _country_name("NPL", dataset_name="Pokhara Valley") == "Pokhara Valley"


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


def test_resolve_title_legacy_uses_pycountry_when_dataset_name_unset() -> None:
    # With no title_template and no dataset_name, the legacy fallback now uses
    # the pycountry-derived country name, not raw iso3. Cleaner default.
    cfg = _cfg(iso3="NPL")
    cat = CategoryConfig(name="Buildings")
    assert _resolve_title(cfg, cat) == "Buildings of Nepal"


def test_resolve_title_legacy_uses_iso3_when_pycountry_has_no_record() -> None:
    cfg = _cfg(iso3="ZZZ")
    cat = CategoryConfig(name="Buildings")
    assert _resolve_title(cfg, cat) == "Buildings of ZZZ"


def test_resolve_title_uses_category_title_override_in_template() -> None:
    cfg = _cfg(iso3="NPL", template="{country} {category} (OpenStreetMap Export)")
    cat = CategoryConfig(
        name="health_facilities",
        hdx=CategoryHdx(title="Hospitals and Clinics"),
    )
    assert _resolve_title(cfg, cat) == "Nepal Hospitals and Clinics (OpenStreetMap Export)"


def test_resolve_title_uses_category_title_override_in_legacy_fallback() -> None:
    cfg = _cfg(iso3="NPL")
    cat = CategoryConfig(
        name="health_facilities",
        hdx=CategoryHdx(title="Hospitals and Clinics"),
    )
    assert _resolve_title(cfg, cat) == "Hospitals and Clinics of Nepal"


def test_resolve_title_uses_dataset_name_for_drc() -> None:
    # No hardcoded override map any more. Users set dataset_name in YAML
    # (or via --dataset-name on the CLI) to fix ugly ISO 3166 inversions.
    cfg = _cfg(
        iso3="COD",
        template="{country} {category} (OpenStreetMap Export)",
        dataset_name="Democratic Republic of the Congo",
    )
    cat = CategoryConfig(name="waterways")
    assert (
        _resolve_title(cfg, cat)
        == "Democratic Republic of the Congo Waterways (OpenStreetMap Export)"
    )


def test_resolve_title_dataset_name_supports_subnational_exports() -> None:
    # Sub-national use case: still pass iso3 for boundary/HDX location wiring
    # (until full sub-national mode lands), but title reads as the named area.
    cfg = _cfg(
        iso3="NPL",
        template="{country} {category} (OpenStreetMap Export)",
        dataset_name="Pokhara",
    )
    cat = CategoryConfig(name="health_facilities")
    assert _resolve_title(cfg, cat) == "Pokhara Health Facilities (OpenStreetMap Export)"
