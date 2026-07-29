"""Tests for same-domain company-page selection."""

from pathlib import Path

import pytest

from app.models import PageCandidate, PageCategory
from app.services import PageSelectionService

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_page_priority_and_same_domain_filtering() -> None:
    """Preferred page types outrank topic pages and external links are removed."""
    candidates = [
        PageCandidate(url="https://www.example.com/about-us", anchor_text="About"),
        PageCandidate(url="https://example.com/services", title="Our services"),
        PageCandidate(url="https://example.com/solutions", title="Our solutions"),
        PageCandidate(url="https://example.com/expertise", title="Our expertise"),
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
        PageCategory.SOLUTIONS,
        PageCategory.EXPERTISE,
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
        ("/solutions", PageCategory.SOLUTIONS),
        ("/expertise", PageCategory.EXPERTISE),
        ("/diensten", PageCategory.SERVICES),
        ("/oplossingen", PageCategory.SOLUTIONS),
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
                title="Our services",
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


def test_discovers_and_prioritizes_pages_from_english_html_fixture() -> None:
    """HTML discovery uses links and navigation order but preserves type priority."""
    html = (FIXTURES / "page_discovery_en.html").read_text(encoding="utf-8")

    ranked = PageSelectionService().discover(
        "https://example.com/",
        html,
        relevant_terms=["Shopify", "ecommerce"],
        limit=10,
    )

    assert [page.category for page in ranked] == [
        PageCategory.HOMEPAGE,
        PageCategory.ABOUT,
        PageCategory.ABOUT,
        PageCategory.SERVICES,
        PageCategory.SOLUTIONS,
        PageCategory.EXPERTISE,
        PageCategory.CONTACT,
        PageCategory.RELEVANT,
    ]
    assert [str(page.url) for page in ranked] == [
        "https://example.com/",
        "https://example.com/about-us",
        "https://example.com/company",
        "https://example.com/services",
        "https://example.com/solutions",
        "https://example.com/expertise",
        "https://example.com/contact-us",
        "https://example.com/shopify-plus-development",
    ]
    assert ranked[1].navigation_position == 1
    assert any("site navigation" in reason for reason in ranked[1].reasons)
    assert all(page.url.host == "example.com" for page in ranked)
    assert all("submit" not in str(page.url) for page in ranked)


def test_discovers_dutch_routes_and_ignores_form_actions() -> None:
    """Dutch page names are found without treating forms as navigation."""
    html = (FIXTURES / "page_discovery_nl.html").read_text(encoding="utf-8")

    ranked = PageSelectionService().discover(
        "https://voorbeeld.nl/",
        html,
        relevant_terms=["Shopify"],
        limit=10,
    )

    assert [(str(page.url), page.category) for page in ranked] == [
        ("https://voorbeeld.nl/", PageCategory.HOMEPAGE),
        ("https://voorbeeld.nl/over-ons", PageCategory.ABOUT),
        ("https://voorbeeld.nl/diensten", PageCategory.SERVICES),
        ("https://voorbeeld.nl/oplossingen", PageCategory.SOLUTIONS),
        ("https://voorbeeld.nl/expertise", PageCategory.EXPERTISE),
        ("https://voorbeeld.nl/contacteer", PageCategory.CONTACT),
        ("https://voorbeeld.nl/shopify", PageCategory.RELEVANT),
    ]
    assert all("/verstuur" not in str(page.url) for page in ranked)


def test_supplied_page_html_enriches_title_and_heading_signals() -> None:
    """Already-approved page documents enrich nonstandard linked routes."""
    homepage = """
    <html><nav><a href="/capabilities">What we do</a></nav></html>
    """
    capabilities = """
    <html>
      <head><title>Services for commerce teams</title></head>
      <body><h1>Our services</h1></body>
    </html>
    """

    ranked = PageSelectionService().discover(
        "https://example.com/",
        {
            "https://example.com/": homepage,
            "https://example.com/capabilities": capabilities,
            "https://unapproved.example/services": (
                "<html><a href='https://example.com/injected'>Injected</a></html>"
            ),
        },
        limit=10,
    )

    capabilities_page = next(
        page for page in ranked if str(page.url).endswith("/capabilities")
    )
    assert capabilities_page.category is PageCategory.SERVICES
    assert capabilities_page.title == "Services for commerce teams"
    assert capabilities_page.headings == ["Our services"]
    assert all("injected" not in str(page.url) for page in ranked)
