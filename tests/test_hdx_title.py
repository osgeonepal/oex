"""Title-resolution helpers for HDX dataset publication."""

import pytest

from oex.config.schema import CategoryConfig, CategoryHdx, HdxConfig, RootConfig
from oex.hdx_publisher import country_name, dataset_title
from oex.naming import _title_case_category

TEMPLATE = "{country} {category} (OpenStreetMap Export)"


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("buildings", "Buildings"),
        ("education_facilities", "Education Facilities"),
        ("populated_places", "Populated Places"),
        ("points_of_interest", "Points of Interest"),
    ],
)
def test_a_category_slug_title_cases_with_filler_words_kept_lowercase(
    slug: str, expected: str
) -> None:
    assert _title_case_category(slug) == expected


@pytest.mark.parametrize(
    ("iso3", "expected"),
    [
        ("NPL", "Nepal"),
        ("npl", "Nepal"),
        ("VNM", "Vietnam"),
        ("IRN", "Iran"),
        ("KOR", "South Korea"),
        ("TZA", "Tanzania"),
        ("ZZZ", "ZZZ"),
    ],
)
def test_country_name_is_the_common_name_or_the_iso3_when_unknown(iso3: str, expected: str) -> None:
    """ISO inverts names such as "Korea, Republic of"; a title needs the common one."""
    assert country_name(iso3) == expected


def test_country_name_uses_dataset_name_when_provided() -> None:
    """dataset_name is how users fix names pycountry renders badly, and name sub-national areas."""
    assert country_name("NPL", dataset_name="Pokhara Valley") == "Pokhara Valley"


@pytest.mark.parametrize(
    ("iso3", "template", "dataset_name", "category", "expected"),
    [
        (
            "NPL",
            TEMPLATE,
            None,
            CategoryConfig(name="buildings"),
            "Nepal Buildings (OpenStreetMap Export)",
        ),
        (
            "NPL",
            TEMPLATE,
            None,
            CategoryConfig(name="points_of_interest"),
            "Nepal Points of Interest (OpenStreetMap Export)",
        ),
        (
            "COD",
            TEMPLATE,
            "Democratic Republic of the Congo",
            CategoryConfig(name="waterways"),
            "Democratic Republic of the Congo Waterways (OpenStreetMap Export)",
        ),
        (
            "NPL",
            TEMPLATE,
            None,
            CategoryConfig(
                name="health_facilities", hdx=CategoryHdx(title="Hospitals and Clinics")
            ),
            "Nepal Hospitals and Clinics (OpenStreetMap Export)",
        ),
        (
            "NPL",
            "",
            "Pokhara Valley",
            CategoryConfig(name="buildings"),
            "Buildings of Pokhara Valley",
        ),
        ("NPL", "", None, CategoryConfig(name="buildings"), "Buildings of Nepal"),
        ("ZZZ", "", None, CategoryConfig(name="buildings"), "Buildings of ZZZ"),
        (
            "NPL",
            "",
            None,
            CategoryConfig(
                name="health_facilities", hdx=CategoryHdx(title="Hospitals and Clinics")
            ),
            "Hospitals and Clinics of Nepal",
        ),
    ],
    ids=[
        "template",
        "template lowercases filler",
        "template with dataset name",
        "template with category title",
        "no template with dataset name",
        "no template from pycountry",
        "no template unknown iso3",
        "no template with category title",
    ],
)
def test_dataset_title_fills_the_template_or_reads_category_of_country(
    iso3: str, template: str, dataset_name: str | None, category: CategoryConfig, expected: str
) -> None:
    cfg = RootConfig(iso3=iso3, dataset_name=dataset_name, hdx=HdxConfig(title_template=template))
    assert dataset_title(cfg, category) == expected
