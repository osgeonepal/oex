"""Unit tests for the planet-engine category filter helpers."""

from oex.config.schema import CategoryConfig, CategoryOsm
from oex.osm.category_filter import category_where_predicate, union_tag_filter


def _cat(name: str, osm_filter: dict, *, enabled: bool = True) -> CategoryConfig:
    cat = CategoryConfig(name=name)
    cat.osm = CategoryOsm(enabled=enabled, filter=osm_filter)
    return cat


def test_union_disjoint_keys() -> None:
    cats = [_cat("buildings", {"building": True}), _cat("roads", {"highway": True})]
    assert union_tag_filter(cats) == {"building": True, "highway": True}


def test_union_true_wins_over_list() -> None:
    cats = [
        _cat("a", {"amenity": ["bank", "atm"]}),
        _cat("b", {"amenity": True}),
    ]
    assert union_tag_filter(cats) == {"amenity": True}


def test_union_list_plus_list_is_sorted_union() -> None:
    cats = [
        _cat("a", {"amenity": ["school", "college"]}),
        _cat("b", {"amenity": ["college", "university"]}),
    ]
    assert union_tag_filter(cats) == {"amenity": ["college", "school", "university"]}


def test_union_list_plus_str_is_list() -> None:
    cats = [
        _cat("a", {"port": "ferry_terminal"}),
        _cat("b", {"port": ["ferry_terminal", "harbour"]}),
    ]
    assert union_tag_filter(cats) == {"port": ["ferry_terminal", "harbour"]}


def test_union_str_plus_str_collapses_to_list() -> None:
    cats = [_cat("a", {"k": "x"}), _cat("b", {"k": "y"})]
    assert union_tag_filter(cats) == {"k": ["x", "y"]}


def test_union_skips_disabled_categories() -> None:
    cats = [
        _cat("a", {"building": True}, enabled=False),
        _cat("b", {"highway": True}),
    ]
    assert union_tag_filter(cats) == {"highway": True}


def test_union_handles_empty_input() -> None:
    assert union_tag_filter([]) == {}


def test_predicate_true_for_match_any() -> None:
    cat = _cat("buildings", {"building": True})
    assert category_where_predicate(cat) == "(tags['building'] IS NOT NULL)"


def test_predicate_in_clause_for_list() -> None:
    cat = _cat("railways", {"railway": ["rail", "station"]})
    assert category_where_predicate(cat) == "(tags['railway'] IN ('rail', 'station'))"


def test_predicate_eq_clause_for_str() -> None:
    cat = _cat("port", {"port": "ferry_terminal"})
    assert category_where_predicate(cat) == "(tags['port'] = 'ferry_terminal')"


def test_predicate_combines_keys_with_or() -> None:
    cat = _cat(
        "education",
        {
            "amenity": ["school", "university"],
            "building": ["school", "university"],
        },
    )
    pred = category_where_predicate(cat)
    assert pred.startswith("(") and pred.endswith(")")
    assert "amenity" in pred and "building" in pred
    assert pred.count(" OR ") == 1


def test_predicate_returns_true_when_filter_empty() -> None:
    cat = _cat("anything", {})
    assert category_where_predicate(cat) == "TRUE"


def test_predicate_escapes_single_quotes_in_values() -> None:
    cat = _cat("k", {"k": "o'reilly"})
    assert category_where_predicate(cat) == "(tags['k'] = 'o''reilly')"
