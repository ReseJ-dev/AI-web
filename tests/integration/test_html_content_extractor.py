"""Fixture-based tests for structured clean HTML extraction."""

from pathlib import Path

from app.services import HtmlContentExtractor

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_extracts_metadata_and_removes_non_content_fixture() -> None:
    """A noisy local company page becomes structured, deduplicated clean text."""
    html = (FIXTURES / "company_homepage_noisy.html").read_text(encoding="utf-8")

    content = HtmlContentExtractor().extract(
        "https://example.com/?campaign=test",
        html,
    )

    assert str(content.canonical_url) == "https://example.com/"
    assert content.title == "Example Commerce — Shopify Experts"
    assert (
        content.meta_description
        == "Shopify development and ecommerce strategy in Amsterdam."
    )
    assert content.open_graph == {
        "og:title": "Example Commerce",
        "og:type": "website",
        "og:url": "https://example.com/",
    }
    assert len(content.organization_data) == 1
    assert content.organization_data[0]["@type"] == "Organization"
    assert content.organization_data[0]["name"] == "Example Commerce"
    assert content.headings == [
        "Digital commerce specialists",
        "Our expertise",
        "Talk to our team",
    ]
    assert [link.text for link in content.navigation_links] == [
        "Home",
        "Over ons",
        "Diensten",
        "Contact",
    ]
    assert [str(link.url) for link in content.contact_page_candidates] == [
        "https://example.com/contactgegevens",
        "https://example.com/contact-us",
    ]
    assert (
        content.main_text.count(
            "Our services include strategy, development, and optimization."
        )
        == 1
    )
    assert "Certified Shopify Plus delivery partner." in content.main_text
    for removed_text in (
        "Accept all cookies",
        "must-not-appear",
        "Decorative hidden copy",
        "trackingSecret",
        "Legal",
    ):
        assert removed_text not in content.main_text
    assert content.truncated is False
    assert content.extracted_text_length == len(content.main_text)


def test_bounds_clean_text_before_future_llm_use() -> None:
    """Only cleaned visible text is truncated at the configured limit."""
    html = "<main><h1>Services</h1><p>" + ("commerce platform " * 200) + "</p></main>"

    content = HtmlContentExtractor(max_text_chars=1_000).extract(
        "https://example.com/services",
        html,
    )

    assert content.truncated is True
    assert len(content.main_text) <= 1_000
    assert content.extracted_text_length == len(content.main_text)
    assert content.main_text.startswith("Services")
