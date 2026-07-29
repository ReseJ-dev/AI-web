"""Tests for deterministic, evidence-based relevance scoring."""

from uuid import uuid4

from pydantic import JsonValue

from app.models import (
    CompanyExtraction,
    CompanyRecord,
    Evidence,
    ExtractedField,
    ExtractionStatus,
    FactBasis,
    RelevanceComponent,
    RequestedField,
    SupportedField,
)
from app.services import RelevanceScoringService

TOPIC = "Shopify agencies in the Netherlands"
LOCATION = "Netherlands"


def _supported(
    name: str,
    value: JsonValue,
    url: str,
    *,
    basis: FactBasis = FactBasis.EXPLICIT,
) -> SupportedField:
    """Build one strict extraction field with evidence."""
    return SupportedField(
        name=name,
        value=value,
        evidence_urls=[url],
        basis=basis,
    )


def _extraction(fields: list[SupportedField]) -> CompanyExtraction:
    """Build an accepted extraction for direct scoring."""
    return CompanyExtraction(
        status=ExtractionStatus.ACCEPTED,
        fields=fields,
    )


def _company(
    *,
    name: str = "Example Commerce",
    website_url: str = "https://example.com/",
    services: list[str] | None = None,
    description: str | None = None,
    extracted_fields: list[ExtractedField] | None = None,
) -> CompanyRecord:
    """Build a company record; raw values alone are deliberately untrusted."""
    return CompanyRecord(
        research_run_id=uuid4(),
        name=name,
        website_url=website_url,
        services=services or [],
        description=description,
        extracted_fields=extracted_fields or [],
    )


def _requested() -> list[RequestedField]:
    """Return the example topic's required structured fields."""
    return [
        RequestedField(name="services"),
        RequestedField(name="country"),
        RequestedField(name="contact page URL"),
    ]


def test_high_candidate_scores_full_points() -> None:
    """Explicit Shopify, Netherlands, website, contact, and field evidence score 100."""
    company = _company()
    extraction = _extraction(
        [
            _supported("company_name", "Example Commerce", "https://example.com/"),
            _supported("website_url", "https://example.com/", "https://example.com/"),
            _supported(
                "services",
                ["Shopify Plus development", "Shopify strategy"],
                "https://example.com/services",
            ),
            _supported("country", "Netherlands", "https://example.com/about"),
            _supported(
                "contact_page_url",
                "https://example.com/contact",
                "https://example.com/contact",
            ),
        ]
    )

    result = RelevanceScoringService().score(
        company,
        extraction,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=_requested(),
    )

    assert result.total_score == 100
    assert result.missing_evidence_penalties == []
    assert result.components[RelevanceComponent.TOPIC_MATCH].score == 30
    assert result.components[RelevanceComponent.LOCATION_MATCH].score == 20
    assert result.components[RelevanceComponent.RELEVANT_SERVICES].score == 15
    assert result.components[RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE].score == 10
    assert result.components[RelevanceComponent.CONTACT_PAGE].score == 10
    assert result.components[RelevanceComponent.EVIDENCE_QUALITY].score == 10
    assert result.components[RelevanceComponent.REQUESTED_FIELD_COMPLETENESS].score == 5
    assert result.explanation[-1] == "Total deterministic relevance score: 100/100."


def test_medium_candidate_gets_partial_inference_and_completeness_points() -> None:
    """Inferred location and a missing contact page produce explicit deductions."""
    extraction = _extraction(
        [
            _supported("company_name", "Medium Commerce", "https://medium.com/"),
            _supported("website_url", "https://medium.com/", "https://medium.com/"),
            _supported(
                "services",
                ["Shopify implementation"],
                "https://medium.com/services",
            ),
            _supported(
                "country",
                "Netherlands",
                "https://medium.com/about",
                basis=FactBasis.INFERENCE,
            ),
            SupportedField(name="contact_page_url", value=None),
        ]
    )

    result = RelevanceScoringService().score(
        _company(name="Medium Commerce", website_url="https://medium.com/"),
        extraction,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=_requested(),
    )

    assert result.total_score == 71
    assert result.components[RelevanceComponent.LOCATION_MATCH].score == 5
    assert result.components[RelevanceComponent.CONTACT_PAGE].score == 0
    assert result.components[RelevanceComponent.EVIDENCE_QUALITY].score == 8
    assert result.components[RelevanceComponent.REQUESTED_FIELD_COMPLETENESS].score == 3
    penalty_components = {
        penalty.component for penalty in result.missing_evidence_penalties
    }
    assert RelevanceComponent.LOCATION_MATCH in penalty_components
    assert RelevanceComponent.CONTACT_PAGE in penalty_components
    assert RelevanceComponent.REQUESTED_FIELD_COMPLETENESS in penalty_components


def test_low_candidate_returns_zero_for_unsupported_topic_criteria() -> None:
    """Cited but irrelevant services and location earn no topic/location points."""
    extraction = _extraction(
        [
            _supported("company_name", "General Studio", "https://studio.com/"),
            _supported("website_url", "https://studio.com/", "https://studio.com/"),
            _supported("services", ["Web design"], "https://studio.com/services"),
            _supported("country", "Belgium", "https://studio.com/about"),
        ]
    )

    result = RelevanceScoringService().score(
        _company(name="General Studio", website_url="https://studio.com/"),
        extraction,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=[
            RequestedField(name="services"),
            RequestedField(name="country"),
        ],
    )

    assert result.total_score == 25
    assert result.components[RelevanceComponent.TOPIC_MATCH].score == 0
    assert result.components[RelevanceComponent.LOCATION_MATCH].score == 0
    assert result.components[RelevanceComponent.RELEVANT_SERVICES].score == 0
    assert result.components[RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE].score == 10


def test_misleading_nl_domain_and_raw_services_do_not_count_as_evidence() -> None:
    """An .nl URL and unsupported raw Shopify labels cannot manufacture relevance."""
    company = _company(
        name="Misleading Candidate",
        website_url="https://misleading.nl/",
        services=["Shopify development"],
        description="A Netherlands Shopify agency.",
    )
    extraction = _extraction(
        [
            _supported(
                "company_name",
                "Misleading Candidate",
                "https://misleading.nl/",
            ),
            SupportedField(name="services", value=None),
            SupportedField(name="country", value=None),
            SupportedField(name="website_url", value=None),
            SupportedField(name="contact_page_url", value=None),
        ]
    )
    service = RelevanceScoringService()

    first = service.score(
        company,
        extraction,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=_requested(),
    )
    second = service.score(
        company,
        extraction,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=_requested(),
    )

    assert first == second
    assert first.total_score == 8
    assert first.components[RelevanceComponent.TOPIC_MATCH].score == 0
    assert first.components[RelevanceComponent.LOCATION_MATCH].score == 0
    assert first.components[RelevanceComponent.RELEVANT_SERVICES].score == 0
    assert first.components[RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE].score == 0
    assert (
        "domain suffixes are not location evidence"
        in first.components[RelevanceComponent.LOCATION_MATCH].explanation
    )


def test_contradictory_and_weak_fields_reduce_evidence_quality() -> None:
    """Contradictions and uncited low-confidence fields have fixed deductions."""
    country_nl = ExtractedField(
        name="country",
        value="Netherlands",
        confidence=0.9,
        evidence=[
            Evidence(
                urls=["https://example.com/about"],
                excerpt="Located in the Netherlands.",
            )
        ],
    )
    country_be = ExtractedField(
        name="country",
        value="Belgium",
        confidence=0.8,
        evidence=[
            Evidence(
                urls=["https://example.com/legal"],
                excerpt="Registered in Belgium.",
            )
        ],
    )
    weak = ExtractedField(
        name="employee_count",
        value=50,
        confidence=0.3,
        evidence=[],
    )
    company = _company(extracted_fields=[country_nl, country_be, weak])
    extraction = _extraction(
        [
            _supported("company_name", "Example Commerce", "https://example.com/"),
            _supported(
                "services",
                ["Shopify development"],
                "https://example.com/services",
            ),
            _supported("country", "Netherlands", "https://example.com/about"),
        ]
    )

    result = RelevanceScoringService().score(
        company,
        extraction,
        topic=TOPIC,
        location=LOCATION,
    )

    evidence = result.components[RelevanceComponent.EVIDENCE_QUALITY]
    assert evidence.score == 4
    assert result.components[RelevanceComponent.LOCATION_MATCH].score == 10
    assert "Contradictory values" in evidence.explanation
    assert "Weak or uncited" in evidence.explanation
    evidence_penalty = next(
        penalty
        for penalty in result.missing_evidence_penalties
        if penalty.component is RelevanceComponent.EVIDENCE_QUALITY
    )
    assert evidence_penalty.points == 6


def test_model_proposed_numerical_score_is_ignored() -> None:
    """An evidence field containing a proposed score never affects the result."""
    company = _company()
    ordinary = _extraction(
        [_supported("company_name", "Example Commerce", "https://example.com/")]
    )
    proposed = _extraction(
        [
            *ordinary.fields,
            _supported(
                "proposed_relevance_score",
                100,
                "https://example.com/",
                basis=FactBasis.INFERENCE,
            ),
        ]
    )
    service = RelevanceScoringService()

    ordinary_result = service.score(
        company,
        ordinary,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=_requested(),
    )
    proposed_result = service.score(
        company,
        proposed,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=_requested(),
    )

    assert proposed_result.total_score == ordinary_result.total_score
    assert proposed_result.components == ordinary_result.components


def test_scores_and_penalties_are_integer_points() -> None:
    """Partial completeness uses deterministic half-up integer allocation."""
    extraction = _extraction(
        [
            _supported("company_name", "Integer Co", "https://integer.example/"),
            _supported("services", ["Shopify"], "https://integer.example/services"),
            SupportedField(name="country", value=None),
        ]
    )

    result = RelevanceScoringService().score(
        _company(name="Integer Co", website_url="https://integer.example/"),
        extraction,
        topic=TOPIC,
        location=LOCATION,
        requested_fields=[
            RequestedField(name="services"),
            RequestedField(name="country"),
        ],
    )

    assert isinstance(result.total_score, int)
    assert all(
        isinstance(component.score, int) for component in result.components.values()
    )
    assert all(
        isinstance(penalty.points, int) for penalty in result.missing_evidence_penalties
    )
    assert (
        sum(penalty.points for penalty in result.missing_evidence_penalties)
        == 100 - result.total_score
    )
    payload = result.model_dump(mode="json")
    assert "country_match" in payload["components"]
    assert "location_match" not in payload["components"]
    assert result.components[RelevanceComponent.REQUESTED_FIELD_COMPLETENESS].score == 3


def test_evidence_quality_collapses_tracking_and_fragment_variants() -> None:
    """Variants of one document cannot manufacture source diversity points."""
    extraction = _extraction(
        [
            _supported(
                "company_name",
                "One Source",
                "https://example.com/about?utm_source=search#identity",
            ),
            _supported(
                "summary",
                "A supported company.",
                "https://example.com/about?fbclid=campaign#summary",
            ),
        ]
    )

    result = RelevanceScoringService().score(
        _company(name="One Source"),
        extraction,
        topic=TOPIC,
        location=LOCATION,
    )

    evidence = result.components[RelevanceComponent.EVIDENCE_QUALITY]
    assert evidence.score == 8
    assert "1 unique URL(s)" in evidence.explanation


def test_semantically_equivalent_values_are_not_contradictions() -> None:
    """Casing, whitespace, and list order do not create false conflicts."""
    company = _company(
        extracted_fields=[
            ExtractedField(
                name="country",
                value="Netherlands",
                confidence=0.9,
                evidence=[
                    Evidence(
                        urls=["https://example.com/about"],
                        excerpt="Netherlands",
                    )
                ],
            ),
            ExtractedField(
                name="country",
                value="  NETHERLANDS ",
                confidence=0.9,
                evidence=[
                    Evidence(
                        urls=["https://example.com/legal"],
                        excerpt="NETHERLANDS",
                    )
                ],
            ),
            ExtractedField(
                name="services",
                value=["Shopify", "Strategy"],
                confidence=0.9,
                evidence=[
                    Evidence(
                        urls=["https://example.com/services"],
                        excerpt="Shopify and strategy",
                    )
                ],
            ),
            ExtractedField(
                name="services",
                value=["strategy", "shopify"],
                confidence=0.9,
                evidence=[
                    Evidence(
                        urls=["https://example.com/work"],
                        excerpt="Strategy and Shopify",
                    )
                ],
            ),
        ]
    )
    extraction = _extraction(
        [
            _supported(
                "services",
                ["Shopify development"],
                "https://example.com/services",
            ),
            _supported("country", "Netherlands", "https://example.com/about"),
        ]
    )

    result = RelevanceScoringService().score(
        company,
        extraction,
        topic=TOPIC,
        location=LOCATION,
    )

    assert result.components[RelevanceComponent.LOCATION_MATCH].score == 20
    evidence = result.components[RelevanceComponent.EVIDENCE_QUALITY]
    assert evidence.score == 10
    assert "Contradictory" not in evidence.explanation
