"""Country to OSM `name:<lang>` resolution via babel."""

import pytest

from oex.locale import local_osm_languages, primary_osm_language


@pytest.mark.parametrize(
    ("iso3", "expected"),
    [
        ("NPL", "ne"),
        ("SDN", "ar"),
        ("IND", "hi"),
        ("FRA", "fr"),
        ("npl", "ne"),
        ("USA", None),
        ("GBR", None),
        ("ZZZ", None),
        ("", None),
    ],
)
def test_primary_osm_language_is_the_first_non_english_official_language(
    iso3: str, expected: str | None
) -> None:
    assert primary_osm_language(iso3) == expected


@pytest.mark.parametrize(
    ("iso3", "expected"),
    [
        ("SDN", ["ar"]),
        ("PHL", ["fil"]),
        ("CHE", ["de", "gsw", "fr"]),
        ("USA", []),
        ("ZZZ", []),
    ],
)
def test_local_osm_languages_skip_english_and_stop_at_three(iso3: str, expected: list[str]) -> None:
    """Switzerland has four official languages, so it shows the cap."""
    assert local_osm_languages(iso3) == expected
