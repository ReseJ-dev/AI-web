"""Offline safeguards for the synthetic Shopify agencies demo."""

import csv
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.demo_scenario import DemoCompanyResult, load_demo_bundle

PROJECT_ROOT = Path(__file__).parents[2]
SCENARIO_PATH = PROJECT_ROOT / "config" / "demo_shopify_agencies.yaml"
RESULTS_PATH = PROJECT_ROOT / "demo" / "shopify_agencies_fake_results.json"

EXPECTED_FIELDS = (
    "Company name",
    "Website",
    "Country",
    "Services",
    "Contact page",
    "Short summary",
    "Relevance score",
)
EXPECTED_QUERIES = (
    "Shopify agency Netherlands",
    "Shopify Plus agency Netherlands",
    "Shopify development company Netherlands",
    "Shopify ecommerce agency Amsterdam",
    "Shopify ecommerce agency Rotterdam",
    "Shopify ecommerce agency Utrecht",
    "Shopify migration agency Netherlands",
    "Dutch Shopify development agency",
    "site:.nl Shopify agency",
    "site:.nl Shopify Plus partner",
)
EXPECTED_SHEETS = {
    "Research Results": (
        "Company name",
        "Website",
        "Country",
        "Services",
        "Contact page",
        "Short summary",
        "Relevance score",
        "Relevance explanation",
        "Evidence URLs",
        "Compliance status",
        "Validation warnings",
        "Retrieved at",
    ),
    "Skipped Sources": (
        "Domain",
        "URL",
        "Decision",
        "Reason",
        "Robots status",
        "Terms status",
        "Checked at",
    ),
    "Run Metadata": ("Field", "Value"),
}


def test_demo_bundle_matches_requested_portfolio_scenario() -> None:
    scenario, results = load_demo_bundle(SCENARIO_PATH, RESULTS_PATH)

    assert scenario.demo_data is True
    assert scenario.research_request.topic == "Shopify agencies in the Netherlands"
    assert scenario.research_request.required_result_count == 30
    assert scenario.research_request.required_fields == EXPECTED_FIELDS
    assert scenario.example_search_queries == EXPECTED_QUERIES
    assert {
        sheet.name: sheet.columns for sheet in scenario.google_sheets_output
    } == EXPECTED_SHEETS
    assert results.demo_data is True
    assert results.total == 30
    assert len(results.items) == 30


def test_all_demo_results_are_explicitly_fictional_and_reserved() -> None:
    _, results = load_demo_bundle(SCENARIO_PATH, RESULTS_PATH)

    assert len({item.company_name for item in results.items}) == 30
    assert len({item.website.host for item in results.items}) == 30
    for item in results.items:
        assert item.demo_data is True
        assert item.company_name.startswith("DEMO — ")
        assert item.website.host is not None
        assert item.website.host.endswith(".example")
        assert item.contact_page.host is not None
        assert item.contact_page.host.endswith(".example")
        assert all(
            url.host and url.host.endswith(".example") for url in item.evidence_urls
        )
        assert "fictional" in " ".join(item.validation_warnings).casefold()


def test_demo_model_rejects_real_world_domains() -> None:
    _, results = load_demo_bundle(SCENARIO_PATH, RESULTS_PATH)
    payload = results.items[0].model_dump(mode="json")
    payload["website"] = "https://unverified-company.com/"

    with pytest.raises(
        ValidationError,
        match=r"reserved \.example domains",
    ):
        DemoCompanyResult.model_validate(payload)


def test_google_sheets_previews_match_configured_columns() -> None:
    scenario, _ = load_demo_bundle(SCENARIO_PATH, RESULTS_PATH)

    for sheet, preview_path in zip(
        scenario.google_sheets_output,
        scenario.offline_files.google_sheets_previews,
        strict=True,
    ):
        with (PROJECT_ROOT / preview_path).open(
            encoding="utf-8",
            newline="",
        ) as preview:
            rows = list(csv.reader(preview))
        assert tuple(rows[0]) == sheet.columns
        assert len(rows) > 1
        if sheet.name == "Research Results":
            for row in rows[1:]:
                assert row[0].startswith("DEMO — ")
                assert ".example/" in row[1]
                assert "DEMO DATA" in row[10]
        elif sheet.name == "Skipped Sources":
            for row in rows[1:]:
                assert row[0].endswith(".example")
                assert "DEMO DATA" in row[3]
        else:
            assert rows[1][0] == "demo data"
            assert "synthetic" in rows[1][1]

    for path in (
        scenario.offline_files.fake_results,
        *scenario.offline_files.google_sheets_previews,
    ):
        assert (PROJECT_ROOT / path).is_file()
