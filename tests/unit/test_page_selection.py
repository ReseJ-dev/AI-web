"""Tests for same-domain company-page selection."""

import pytest

from app.models import PageCandidate, PageCategory
from app.services import PageSelectionService


def test_page_priority_and_same_domain_filtering() -> None:
    """Preferred page types outrank topic pages and external links are removed."""
    candidates = [
        PageCandidate(url="https://www.example.com/about-us", anchor_text="About"),
        PageCandidate(url="https://example.com/services", title="Our services"),
        PageCandidate(
            url="https://example.com/contact",
            headings=["Contact our team"],
        ),
        PageCandidate(
            url="https://example.com/shopify-plus",
            title="Shopify Plus for fashion brands",
        ),
        PageCandidate(
            url="https://external.example/services",
            title="External services",
        ),
    ]

    ranked = PageSelectionService().select(
        "https://example.com/",
        candidates,
        relevant_terms=["Shopify", "fashion"],
        limit=10,
    )

    assert [page.category for page in ranked] == [
        PageCategory.HOMEPAGE,
        PageCategory.ABOUT,
        PageCategory.SERVICES,
        PageCategory.CONTACT,
        PageCategory.RELEVANT,
    ]
    assert all("external.example" not in str(page.url) for page in ranked)
    assert "platform or industry" in ranked[-1].reasons[0]


def test_path_evidence_wins_and_www_homepage_is_deduplicated() -> None:
    """Explicit routes outrank conflicting text and www is the same homepage."""
    ranked = PageSelectionService().select(
        "https://example.com/?campaign=test",
        [
            PageCandidate(
                url="https://www.example.com/",
                title="Example Company",
            ),
            PageCandidate(
                url="https://example.com/services",
                title="About our approach",
            ),
        ],
        limit=10,
    )

    assert [page.category for page in ranked] == [
        PageCategory.HOMEPAGE,
        PageCategory.SERVICES,
    ]


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("/about", PageCategory.ABOUT),
        ("/about-us", PageCategory.ABOUT),
        ("/company", PageCategory.ABOUT),
        ("/over-ons", PageCategory.ABOUT),
        ("/services", PageCategory.SERVICES),
        ("/solutions", PageCategory.SERVICES),
        ("/expertise", PageCategory.SERVICES),
        ("/diensten", PageCategory.SERVICES),
        ("/oplossingen", PageCategory.SERVICES),
        ("/contact", PageCategory.CONTACT),
        ("/contact-us", PageCategory.CONTACT),
        ("/get-in-touch", PageCategory.CONTACT),
        ("/contacteer", PageCategory.CONTACT),
        ("/contactgegevens", PageCategory.CONTACT),
    ],
)
def test_recognizes_english_and_dutch_paths(
    path: str,
    category: PageCategory,
) -> None:
    """Every requested English and Dutch route maps to its page category."""
    ranked = PageSelectionService().select(
        "https://example.com/",
        [PageCandidate(url=f"https://example.com{path}")],
        limit=10,
    )

    selected = next(page for page in ranked if str(page.url).endswith(path))
    assert selected.category is category
    assert "URL path" in selected.reasons[0]


@pytest.mark.parametrize(
    ("candidate", "category"),
    [
        (
            PageCandidate(
                url="https://example.com/team",
                anchor_text="Over ons",
            ),
            PageCategory.ABOUT,
        ),
        (
            PageCandidate(
                url="https://example.com/capabilities",
                title="Services and solutions",
            ),
            PageCategory.SERVICES,
        ),
        (
            PageCandidate(
                url="https://example.com/reach-us",
                headings=["Get in touch"],
            ),
            PageCategory.CONTACT,
        ),
    ],
)
def test_uses_anchor_title_and_heading_signals(
    candidate: PageCandidate,
    category: PageCategory,
) -> None:
    """Nonstandard paths can be classified using their visible metadata."""
    ranked = PageSelectionService().select(
        "https://example.com/",
        [candidate],
        limit=10,
    )

    selected = next(
        page for page in ranked if page.category is not PageCategory.HOMEPAGE
    )
    assert selected.category is category
