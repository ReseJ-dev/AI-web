"""Tests for the Streamlit dashboard client and pure presentation helpers."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from app.api.schemas import (
    CreateResearchRunRequest,
    ResearchResultItem,
    ResearchRunResponse,
)
from app.models import (
    RequestedField,
    ResearchProgressStage,
    ResearchRunStatus,
)
from app.ui.api_client import DashboardApiError, ResearchApiClient
from app.ui.presentation import (
    country_code_from_hint,
    filter_results,
    progress_fraction,
    result_rows,
    rows_to_csv,
)


def _run_payload() -> dict[str, object]:
    timestamp = "2026-07-29T10:00:00Z"
    return {
        "id": str(uuid4()),
        "status": "running",
        "progress_stage": "searching",
        "progress_message": "Searching.",
        "completed_items": 0,
        "total_items": 30,
        "partial_result_count": 0,
        "discovered_candidate_count": 5,
        "approved_candidate_count": 0,
        "skipped_source_count": 0,
        "completed_result_count": 0,
        "warnings": [],
        "error_message": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _result(
    *,
    name: str,
    score: int,
) -> ResearchResultItem:
    return ResearchResultItem(
        id=uuid4(),
        company_name=name,
        website="https://example.com/",
        country="Netherlands",
        services=["Shopify development", "Strategy"],
        contact_page="https://example.com/contact",
        short_summary="Evidence-backed company summary.",
        relevance_score=score,
        relevance_explanation=["Explicit evidence."],
        evidence_urls=["https://example.com/services"],
        retrieved_at=datetime.now(UTC),
    )


def test_api_client_calls_dashboard_endpoints_and_sends_sheet_id() -> None:
    """The UI client validates responses and uses only public API endpoints."""
    calls: list[httpx.Request] = []
    run_payload = _run_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/research-runs" and request.method == "POST":
            return httpx.Response(202, json=run_payload)
        if path.endswith("/results"):
            return httpx.Response(
                200,
                json={
                    "run_id": run_payload["id"],
                    "items": [],
                    "total": 0,
                    "offset": 0,
                    "limit": 100,
                    "partial": True,
                },
            )
        if path.endswith("/skipped-sources"):
            return httpx.Response(
                200,
                json={
                    "run_id": run_payload["id"],
                    "items": [],
                    "total": 0,
                    "offset": 0,
                    "limit": 100,
                },
            )
        if path.endswith("/export/google-sheets"):
            return httpx.Response(
                200,
                json={
                    "run_id": run_payload["id"],
                    "artifact": {
                        "format_name": "google_sheets",
                        "location": "https://docs.google.com/spreadsheets/d/sheet-1",
                        "record_count": 0,
                    },
                },
            )
        return httpx.Response(200, json=run_payload)

    http_client = httpx.Client(
        base_url="http://api.test",
        transport=httpx.MockTransport(handler),
    )
    client = ResearchApiClient("http://api.test", client=http_client)
    request = CreateResearchRunRequest(
        topic="Shopify agencies in the Netherlands",
        requested_fields=[RequestedField(name="services")],
        result_count=30,
        location="Netherlands",
        country="NL",
        language="en",
        country_tld="nl",
    )

    run = client.start_research(request)
    assert client.get_run(run.id).discovered_candidate_count == 5
    assert client.get_results(run.id).items == []
    assert client.get_skipped_sources(run.id).items == []
    artifact = client.export_google_sheets(
        run.id,
        spreadsheet_id="existing-sheet_123",
    )

    assert artifact.artifact.format_name == "google_sheets"
    export_request = calls[-1]
    assert json.loads(export_request.content) == {
        "spreadsheet_id": "existing-sheet_123"
    }
    assert all(
        request.headers["X-Request-ID"].startswith("dashboard-") for request in calls
    )
    assert all("/api/" in request.url.path for request in calls)
    http_client.close()


def test_api_client_returns_safe_structured_errors() -> None:
    """Structured API errors retain the request ID without leaking raw payloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": "provider_unavailable",
                    "message": "The search provider is not configured.",
                    "request_id": "api-request-1",
                    "details": [],
                }
            },
        )

    http_client = httpx.Client(
        base_url="http://api.test",
        transport=httpx.MockTransport(handler),
    )
    client = ResearchApiClient("http://api.test", client=http_client)

    with pytest.raises(DashboardApiError) as captured:
        client.get_run(uuid4())

    assert str(captured.value) == "The search provider is not configured."
    assert captured.value.request_id == "api-request-1"
    http_client.close()


def test_result_filter_table_projection_and_safe_csv() -> None:
    """Filtering and downloads reflect visible fields and neutralize formulas."""
    items = [
        _result(name="=Formula Agency", score=95),
        _result(name="Lower Agency", score=50),
    ]
    filtered = filter_results(items, 80)
    rows = result_rows(
        filtered,
        ["Company name", "Website", "Services", "Relevance score"],
    )
    csv_data = rows_to_csv(rows).decode("utf-8-sig")

    assert len(filtered) == 1
    assert rows[0]["Relevance score"] == 95
    assert "Contact page" not in rows[0]
    assert "'=Formula Agency" in csv_data
    assert "copied" not in csv_data


def test_country_hints_and_progress_are_deterministic() -> None:
    """Friendly country hints and progress stages map to stable API values."""
    assert country_code_from_hint("Netherlands") == "NL"
    assert country_code_from_hint("Netherlands (NL)") == "NL"
    assert country_code_from_hint("gb") == "GB"
    with pytest.raises(ValueError, match="supported country"):
        country_code_from_hint("Atlantis")

    run = ResearchRunResponse(
        **{
            **_run_payload(),
            "progress_stage": ResearchProgressStage.CRAWLING,
        }
    )
    assert 0 < progress_fraction(run) < 1
    completed = run.model_copy(update={"status": ResearchRunStatus.COMPLETED})
    assert progress_fraction(completed) == 1


def test_streamlit_demo_form_is_prepopulated_and_compliance_forward() -> None:
    """The portfolio entry point renders every required demo control."""
    app = AppTest.from_file("app/ui/main.py").run(timeout=10)

    assert not list(app.exception)
    assert app.title[0].value == "AI Web Research & Data Extraction Agent"
    text_inputs = {item.label: item.value for item in app.text_input}
    assert text_inputs["Research topic"] == "Shopify agencies in the Netherlands"
    assert text_inputs["Country hint"] == "Netherlands"
    assert text_inputs["Language hint"] == "en"
    assert "Google Sheet ID" in text_inputs
    assert app.number_input[0].label == "Required number of results"
    assert app.number_input[0].value == 30
    assert app.checkbox[0].label == "Strict compliance mode"
    assert app.checkbox[0].value is True
    assert app.multiselect[0].value == [
        "Company name",
        "Website",
        "Country",
        "Services",
        "Contact page",
        "Short summary",
        "Relevance score",
    ]
    assert app.button[0].label == "Start Research"
    assert "Blocked or ambiguous websites are skipped" in app.info[0].value
    assert "not legal advice" in app.info[0].value
