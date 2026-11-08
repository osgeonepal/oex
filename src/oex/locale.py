"""Resolve a country's OSM `name:<lang>` tags from its ISO3 code via babel."""

import pycountry
from babel.languages import get_official_languages

_MAX_LOCAL_LANGUAGES = 3


def primary_osm_language(iso3: str) -> str | None:
    """First non-English official language for the country, or None."""
    languages = local_osm_languages(iso3)
    return languages[0] if languages else None


def local_osm_languages(iso3: str) -> list[str]:
    """Non-English official languages, at most _MAX_LOCAL_LANGUAGES. Babel lists English first for some
    multilingual countries, and `name_en` is already in the schema's select."""
    if not iso3:
        return []

    country = pycountry.countries.get(alpha_3=iso3.upper())
    if country is None:
        return []

    languages = get_official_languages(country.alpha_2, regional=False, de_facto=True)
    selected: list[str] = []
    for language in languages:
        if language == "en" or language in selected:
            continue
        selected.append(language)
        if len(selected) >= _MAX_LOCAL_LANGUAGES:
            break
    return selected
