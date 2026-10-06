from collections.abc import Iterable

from oex.config.schema import CategoryConfig

OsmTagValue = bool | str | list[str]
OsmTagsFilter = dict[str, OsmTagValue]


def union_tag_filter(categories: Iterable[CategoryConfig]) -> OsmTagsFilter:
    """Merge category osm.filter blocks into one quackosm filter: True wins for a key, and
    values from both sides are unioned."""
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
    """This category's osm.filter as a parenthesised predicate on `tags`, "TRUE" when the
    filter is empty."""
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
