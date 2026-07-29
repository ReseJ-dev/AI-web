"""Unit tests for research domain validation and serialization."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.models import (
    CompanyRecord,
    ComplianceDecision,
    ComplianceStatus,
    Evidence,
    ExtractedField,
    RelevanceScoreBreakdown,
    RequestedField,
    ResearchRequest,
    ResearchRun,
)


def test_requested_fields_are_normalized() -> None:
    """Field names and descriptions use a stable canonical representation."""
    field = RequestedField(
        name="  Annual Revenue (USD)  ",
        description="  Most recent reported revenue  ",
    )

    assert field.name == "annual_revenue_usd"
    assert field.description == "Most recent reported revenue"


@pytest.mark.parametrize("result_count", [0, 101])
def test_research_request_rejects_invalid_result_count(result_count: int) -> None:
    """Requested result count is constrained to the supported range."""
    with pytest.raises(ValidationError):
        ResearchRequest(
            query="cybersecurity companies",
            requested_fields=[RequestedField(name="website")],
            result_count=result_count,
        )


def test_research_request_rejects_duplicate_normalized_fields() -> None:
    """Different spellings cannot produce duplicate canonical field names."""
    with pytest.raises(
        ValidationError,
        match="requested field names must be unique",
    ):
        ResearchRequest(
            query="cybersecurity companies",
            requested_fields=[
                RequestedField(name="Annual Revenue"),
                RequestedField(name="annual-revenue"),
            ],
        )


def test_research_run_requires_aware_timestamps_and_normalizes_to_utc() -> None:
    """Domain timestamps reject naive values and normalize timezone offsets."""
    request = ResearchRequest(
        query="research agent vendors",
        requested_fields=[RequestedField(name="Website URL")],
    )

    with pytest.raises(ValidationError, match="timezone information"):
        ResearchRun(request=request, created_at=datetime(2026, 1, 1))

    local_time = datetime(
        2026,
        1,
        1,
        12,
        tzinfo=timezone(timedelta(hours=2)),
    )
    run = ResearchRun(
        request=request,
        created_at=local_time,
        updated_at=local_time,
    )

    assert run.created_at == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert run.created_at.tzinfo is UTC


def test_company_record_serializes_json_safe_lists_and_urls() -> None:
    """Company records serialize UUIDs, services, and evidence URLs to JSON."""
    run_id = uuid4()
    company = CompanyRecord(
        research_run_id=run_id,
        name="Example Research Ltd",
        website_url="https://example.com",
        services=["web research", "data extraction"],
        extracted_fields=[
            ExtractedField(
                name="Primary Service",
                value={"category": "research"},
                confidence=0.95,
                evidence=[
                    Evidence(
                        urls=[
                            "https://example.com/services",
                            "https://example.com/about",
                        ],
                        excerpt="Structured research services for business teams.",
                    )
                ],
            )
        ],
    )

    payload = company.model_dump(mode="json")

    assert payload["research_run_id"] == str(run_id)
    assert payload["services"] == ["web research", "data extraction"]
    evidence = payload["extracted_fields"][0]["evidence"][0]
    assert evidence["urls"] == [
        "https://example.com/services",
        "https://example.com/about",
    ]
    assert CompanyRecord.model_validate_json(company.model_dump_json()) == company


def test_compliance_decision_serializes_enum_and_score_breakdown() -> None:
    """Compliance decisions produce JSON-safe status and scoring data."""
    company_id = UUID("b81d4fae-7dec-4e14-92fb-f3ce5f5bd633")
    decision = ComplianceDecision(
        company_record_id=company_id,
        status=ComplianceStatus.APPROVED,
        reasons=["Matches requested services"],
        relevance=RelevanceScoreBreakdown(
            query_match=0.9,
            service_match=0.8,
            evidence_quality=0.7,
            total=0.82,
        ),
    )

    payload = decision.model_dump(mode="json")

    assert payload["status"] == "approved"
    assert payload["relevance"]["total"] == 0.82
    assert payload["company_record_id"] == str(company_id)
