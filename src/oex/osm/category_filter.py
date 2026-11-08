from collections.abc import Iterable

from oex.config.schema import CategoryConfig
from oex.sql import sql_string

OsmTagValue = bool | str | list[str]
OsmTagsFilter = dict[str, OsmTagValue]


def union_tag_filter(categories: Iterable[CategoryConfig]) -> OsmTagsFilter:
    """Merge category osm.filter blocks into one quackosm filter: True wins for a key, and
    values from both sides are unioned."""
    merged: OsmTagsFilter = {}
    for category in categories:
        if not category.osm.enabled:
            continue
        for key, value in (category.osm.filter or {}).items():
            # False only narrows its own category, which category_where_predicate applies.
            if value is False:
                continue
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
    """This category's osm.filter as a parenthesised predicate on `tags`, "TRUE" when the
    filter is empty."""
    if not category.osm.filter:
        return "TRUE"

    clauses: list[str] = []
    absent_clauses: list[str] = []
    for key, value in category.osm.filter.items():
        normalised = _normalise(value)
        if isinstance(normalised, bool):
            if normalised:
                clauses.append(f"tags[{sql_string(key)}] IS NOT NULL")
            else:
                absent_clauses.append(f"tags[{sql_string(key)}] IS NULL")
        elif isinstance(normalised, list):
            values = ", ".join(sql_string(item) for item in normalised)
            clauses.append(f"tags[{sql_string(key)}] IN ({values})")
        else:
            clauses.append(f"tags[{sql_string(key)}] = {sql_string(normalised)}")
    # Same meaning as quackosm: any wanted tag matches, and every False tag must be absent.
    predicate = "(" + " OR ".join(clauses) + ")" if clauses else "TRUE"
    if absent_clauses:
        predicate = "(" + " AND ".join([predicate, *absent_clauses]) + ")"
    return predicate


def _normalise(value: OsmTagValue) -> OsmTagValue:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return sorted({item for item in value if isinstance(item, str)})
    return bool(value)


def _to_set(value: OsmTagValue) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set(value)
    raise ValueError(f"cannot turn {value!r} into a tag-value set")
