"""Fixture-based tests for structured clean HTML extraction."""

from pathlib import Path

from app.models import TextBlockKind
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
    assert content.text_blocks
    assert all(
        str(block.source_url) == "https://example.com/?campaign=test"
        for block in content.text_blocks
    )
    assert content.service_sections[0].heading == "Our expertise"
    assert all(
        block.source_url == content.source_url
        for section in content.service_sections
        for block in section.text_blocks
    )


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
    assert "\n".join(block.text for block in content.text_blocks) == content.main_text
    assert all(block.source_url == content.source_url for block in content.text_blocks)


def test_extracts_service_sections_from_realistic_company_fixture() -> None:
    """Service groups retain clean blocks and exact page-level provenance."""
    html = (FIXTURES / "company_services_realistic.html").read_text(encoding="utf-8")

    content = HtmlContentExtractor().extract(
        "https://northstar.example/services?ref=research",
        html,
    )

    assert str(content.canonical_url) == "https://northstar.example/services"
    assert content.title == "Commerce services | Northstar Digital"
    assert content.open_graph["og:site_name"] == "Northstar Digital"
    assert content.organization_data[0]["name"] == "Northstar Digital"
    assert [section.heading for section in content.service_sections] == [
        "Commerce services that help brands grow",
        "Our services",
        "Solutions",
    ]
    assert all(
        str(section.source_url) == "https://northstar.example/services?ref=research"
        for section in content.service_sections
    )
    service_text = "\n".join(
        block.text
        for section in content.service_sections
        for block in section.text_blocks
    )
    assert (
        service_text.count(
            "Accessible Shopify themes and custom commerce integrations."
        )
        == 1
    )
    assert "This hidden draft" not in service_text
    assert "Accept all" not in content.main_text
    assert "Copyright Northstar Digital" not in content.main_text
    assert content.main_text.count("Commerce services that help brands grow") == 1
    assert [str(link.url) for link in content.contact_page_candidates] == [
        "https://northstar.example/contact",
        "https://northstar.example/contact-us",
    ]


def test_trafilatura_fallback_cleans_realistic_page_without_main_element() -> None:
    """Body-only layouts use precision extraction after deterministic cleanup."""
    html = (FIXTURES / "company_about_realistic.html").read_text(encoding="utf-8")

    content = HtmlContentExtractor().extract(
        "https://delta.example/over-ons?utm_source=test",
        html,
    )

    assert str(content.canonical_url) == "https://delta.example/over-ons"
    assert content.headings == [
        "Wij zijn Delta Commerce",
        "Onze aanpak",
        "Newsletter signup",
    ]
    assert "Vanuit Utrecht" in content.main_text
    assert (
        content.main_text.count(
            "Strategie, ontwerp en techniek werken samen in één ervaren team."
        )
        == 1
    )
    for removed in (
        "Private staging notes",
        "Privacy · Cookies",
        "Home Diensten Contactgegevens",
    ):
        assert removed not in content.main_text
    assert all(block.kind is TextBlockKind.OTHER for block in content.text_blocks)
    assert all(
        str(block.source_url) == "https://delta.example/over-ons?utm_source=test"
        for block in content.text_blocks
    )
