"""Tests for candidate-discovery query planning."""

import pytest

from app.services import QueryPlanner


def test_shopify_netherlands_query_plan() -> None:
    """The example topic produces varied market-specific discovery queries."""
    queries = QueryPlanner().plan(
        "Shopify agency",
        location="Netherlands",
        city="Amsterdam",
        country_tld=".nl",
    )

    assert queries == [
        "Shopify agency Netherlands",
        "Shopify Plus agency Netherlands",
        "Shopify development company Netherlands",
        "Shopify ecommerce agency Amsterdam",
        "site:.nl Shopify agency",
    ]


def test_complete_demo_topic_does_not_duplicate_location() -> None:
    """The dashboard topic remains a valid subject instead of repeating its market."""
    queries = QueryPlanner().plan(
        "Shopify agencies in the Netherlands",
        location="Netherlands",
        city="Amsterdam",
        country_tld="nl",
    )

    assert queries == [
        "Shopify agencies in the Netherlands",
        "Shopify Plus agency Netherlands",
        "Shopify development company Netherlands",
        "Shopify ecommerce agency Amsterdam",
        "site:.nl Shopify agency",
    ]


def test_query_plan_rejects_invalid_country_tld() -> None:
    """A site filter cannot inject arbitrary search syntax."""
    with pytest.raises(ValueError, match="country_tld"):
        QueryPlanner().plan(
            "research agency",
            location="Netherlands",
            country_tld="nl OR site:example.com",
        )
