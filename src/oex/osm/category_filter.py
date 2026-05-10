"""Translate oex CategoryConfig.osm.filter into quackosm filters and SQL.

Two consumers:
- planet engine prep: build a single union OsmTagsFilter from N categories
  for the one-pass quackosm call.
- planet engine query_for: build the per-category SQL WHERE predicate that
  picks just one category's features from the unified country.parquet.
"""

from collections.abc import Iterable

from oex.config.schema import CategoryConfig

OsmTagValue = bool | str | list[str]
OsmTagsFilter = dict[str, OsmTagValue]


def union_tag_filter(categories: Iterable[CategoryConfig]) -> OsmTagsFilter:
    """Merge N category osm.filter blocks into one quackosm OsmTagsFilter.

    Rules:
    - Any True wins for a key (any-value match).
    - list+list -> sorted union; list+str -> list with str added; str+str -> list of both.
    """
    merged: OsmTagsFilter = {}
    for cat in categories:
        if not cat.osm.enabled:
            continue
        for key, value in (cat.osm.filter or {}).items():
            if key not in merged:
                merged[key] = _normalise(value)
                continue
            existing = merged[key]
            if existing is True or value is True:
                merged[key] = True
                continue
            existing_set = _to_set(existing)
            new_set = _to_set(value)
            merged[key] = sorted(existing_set | new_set)
    return merged


def category_where_predicate(category: CategoryConfig) -> str:
    """SQL WHERE clause matching this category's osm.filter on `tags MAP`.

    Returns a parenthesised expression suitable for AND'ing into a larger
    WHERE. Empty filter -> "TRUE" (matches all).
    """
    if not category.osm.filter:
        return "TRUE"

    clauses: list[str] = []
    for key, value in category.osm.filter.items():
        normalised = _normalise(value)
        if isinstance(normalised, bool):
            if normalised:
                clauses.append(f"tags['{_sql_escape(key)}'] IS NOT NULL")
        elif isinstance(normalised, list):
            values = ", ".join(f"'{_sql_escape(v)}'" for v in normalised)
            clauses.append(f"tags['{_sql_escape(key)}'] IN ({values})")
        else:
            clauses.append(f"tags['{_sql_escape(key)}'] = '{_sql_escape(normalised)}'")
    return "(" + " OR ".join(clauses) + ")"


def _normalise(value: OsmTagValue) -> OsmTagValue:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return sorted({v for v in value if isinstance(v, str)})
    return bool(value)


def _to_set(value: OsmTagValue) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set(value)
    raise ValueError(f"cannot turn {value!r} into a tag-value set")


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")
