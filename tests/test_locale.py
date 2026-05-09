"""Country -> OSM `name:<lang>` resolution via babel."""

from oex.locale import local_osm_languages, primary_osm_language


def test_primary_nepal_resolves_to_nepali() -> None:
    assert primary_osm_language("NPL") == "ne"


def test_primary_sudan_resolves_to_arabic() -> None:
    assert primary_osm_language("SDN") == "ar"


def test_primary_india_resolves_to_hindi() -> None:
    assert primary_osm_language("IND") == "hi"


def test_primary_france_resolves_to_french() -> None:
    assert primary_osm_language("FRA") == "fr"


def test_primary_lowercase_iso3_is_accepted() -> None:
    assert primary_osm_language("npl") == "ne"


def test_primary_english_speaking_countries_return_none() -> None:
    assert primary_osm_language("USA") is None
    assert primary_osm_language("GBR") is None


def test_primary_unknown_iso3_returns_none() -> None:
    assert primary_osm_language("ZZZ") is None


def test_primary_empty_iso3_returns_none() -> None:
    assert primary_osm_language("") is None


def test_local_languages_skips_english() -> None:
    assert "en" not in local_osm_languages("SDN")
    assert "en" not in local_osm_languages("PHL")


def test_local_languages_capped_at_three() -> None:
    assert len(local_osm_languages("CHE")) <= 3


def test_local_languages_multilingual_country_returns_multiple() -> None:
    languages = local_osm_languages("CHE")
    assert "de" in languages
    assert "fr" in languages


def test_local_languages_unknown_iso3_returns_empty() -> None:
    assert local_osm_languages("ZZZ") == []


def test_local_languages_english_only_country_returns_empty() -> None:
    assert local_osm_languages("USA") == []
