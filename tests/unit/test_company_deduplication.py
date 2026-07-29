"""Comprehensive tests for company deduplication and entity resolution."""

from uuid import uuid4

import pytest

from app.models import (
    CompanyEntity,
    CompanyRecord,
    EntityResolutionOutcome,
    Evidence,
    ExtractedField,
    OfficialIdentifier,
    OfficialIdentifierSource,
)
from app.services import (
    CompanyDeduplicationService,
    normalize_company_name,
    normalize_company_url,
    normalize_official_identifier,
    registrable_domain,
)


def _company(
    name: str,
    website_url: str | None,
    *,
    description: str | None = None,
    services: list[str] | None = None,
    fields: list[ExtractedField] | None = None,
) -> CompanyRecord:
    """Build an isolated company record for pairwise resolution."""
    return CompanyRecord(
        research_run_id=uuid4(),
        name=name,
        website_url=website_url,
        description=description,
        services=services or [],
        extracted_fields=fields or [],
    )


def _field(
    name: str,
    value: object,
    confidence: float,
    evidence_url: str,
) -> ExtractedField:
    """Build one confidence-bearing field with source evidence."""
    return ExtractedField(
        name=name,
        value=value,
        confidence=confidence,
        evidence=[
            Evidence(
                urls=[evidence_url],
                excerpt=f"Evidence for {name}",
            )
        ],
    )


def test_normalizes_company_urls_and_internationalized_domains() -> None:
    """Scheme, www, ports, tracking, fragments, and IDNs normalize together."""
    normalized = normalize_company_url(
        "HTTP://WWW.BÜCHER.DE:80/services/?utm_source=newsletter"
        "&b=2&fbclid=secret&a=1&srsltid=search&hsCtaTracking=cta"
        "&mkt_tok=mail#team"
    )

    assert normalized == "https://xn--bcher-kva.de/services?a=1&b=2"
    assert normalize_company_url("example.com/") == "https://example.com"


def test_uses_public_suffix_rules_for_registrable_domains_offline() -> None:
    """Multi-label and private suffixes avoid naive last-two-label matching."""
    assert registrable_domain("https://shop.example.co.uk/path") == "example.co.uk"
    assert registrable_domain("https://tenant.github.io/") == "tenant.github.io"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  ACME, B.V. ", "acme"),
        ("Acme Incorporated", "acme"),
        ("ACME GmbH", "acme"),
        ("A&B Solutions, LLC", "a and b solutions"),
        ("Société Élan S.A.R.L.", "société élan"),
        ("Voorbeeld B.V.B.A.", "voorbeeld"),
        ("Nordic Studio A/S", "nordic studio"),
        ("Tallinn Digital OÜ", "tallinn digital"),
        ("Comercio S.L.U.", "comercio"),
    ],
)
def test_normalizes_company_names_and_legal_suffixes(
    raw: str,
    expected: str,
) -> None:
    """Punctuation, spacing, casing, Unicode, and legal suffixes are stable."""
    assert normalize_company_name(raw) == expected


def test_exact_registrable_domain_match_merges_first() -> None:
    """Different schemes, subdomains, paths, and trackers share one entity."""
    left = _company("Acme Europe B.V.", "http://www.acme.co.uk/about/")
    right = _company(
        "Acme Commerce",
        "https://shop.acme.co.uk/?utm_campaign=spring",
    )

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MERGE
    assert result.confidence == 0.99
    assert "registrable-domain match" in result.explanation[0]
    assert result.merged_company is not None
    assert result.merge_metadata is not None


def test_redirect_canonical_domain_match_merges_distinct_original_domains() -> None:
    """Redirect destinations can establish identity after original URLs differ."""
    left = CompanyEntity(
        record=_company("Old Brand", "https://old-brand.example/"),
        canonical_url="https://www.new-brand.com/home",
    )
    right = CompanyEntity(
        record=_company("New Brand", "https://new-brand.example/"),
        canonical_url="http://new-brand.com/",
    )

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MERGE
    assert result.confidence == 0.98
    assert any("canonical domains match" in reason for reason in result.explanation)


def test_exact_normalized_legal_name_match_requires_review_without_support() -> None:
    """Even exact normalized names can be namesakes when domains are absent."""
    left = _company("Northstar Commerce B.V.", None)
    right = _company(" NORTHSTAR COMMERCE, LTD. ", None)

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED
    assert result.confidence == 0.94
    assert result.merged_company is None
    assert any("normalized company-name match" in item for item in result.explanation)
    assert any("namesake" in item for item in result.explanation)


def test_high_fuzzy_name_similarity_requires_manual_review() -> None:
    """A highly similar name never causes an automatic merge by itself."""
    left = _company("Northstar Commerce", "https://northstar.example/")
    right = _company("North Star Commerce", "https://north-star.example/")

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED
    assert result.fuzzy_name_score is not None
    assert result.fuzzy_name_score >= 92
    assert result.merged_company is None
    assert any("alone are insufficient" in item for item in result.explanation)


def test_low_name_similarity_keeps_records_separate() -> None:
    """No domain, exact-name, fuzzy, or identifier match keeps both records."""
    left = _company("Acme Solar", "https://acme-solar.example/")
    right = _company("Acme Software", "https://acme-software.example/")

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.KEEP_SEPARATE
    assert result.fuzzy_name_score is not None
    assert result.fuzzy_name_score < 92
    assert result.merge_metadata is None


@pytest.mark.parametrize(
    "identifier",
    [
        OfficialIdentifier(
            source=OfficialIdentifierSource.WIKIDATA,
            value="https://www.wikidata.org/wiki/q12345",
        ),
        OfficialIdentifier(
            source=OfficialIdentifierSource.OPENCORPORATES,
            value="https://opencorporates.com/companies/nl/12345678",
        ),
    ],
)
def test_shared_official_identifier_merges(
    identifier: OfficialIdentifier,
) -> None:
    """Shared authoritative IDs resolve records despite unrelated names."""
    normalized = normalize_official_identifier(identifier)
    left = CompanyEntity(
        record=_company("Trading Brand", "https://brand.example/"),
        official_identifiers=[identifier],
    )
    right = CompanyEntity(
        record=_company("Registered Parent", "https://parent.example/"),
        official_identifiers=[normalized],
    )

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MERGE
    assert any("Shared official identifier" in item for item in result.explanation)


def test_normalizes_official_opencorporates_api_identifier_url() -> None:
    """The official versioned API URL normalizes to jurisdiction and number."""
    normalized = normalize_official_identifier(
        OfficialIdentifier(
            source=OfficialIdentifierSource.OPENCORPORATES,
            value="https://api.opencorporates.com/v0.4/companies/NL/12345678",
        )
    )

    assert normalized.value == "nl/12345678"


@pytest.mark.parametrize(
    "identifier",
    [
        OfficialIdentifier(
            source=OfficialIdentifierSource.WIKIDATA,
            value="https://example.com/wiki/Q123",
        ),
        OfficialIdentifier(
            source=OfficialIdentifierSource.OPENCORPORATES,
            value="https://example.com/companies/nl/12345678",
        ),
        OfficialIdentifier(
            source=OfficialIdentifierSource.OPENCORPORATES,
            value="nl/",
        ),
        OfficialIdentifier(
            source=OfficialIdentifierSource.OPENCORPORATES,
            value="nl/123/extra",
        ),
    ],
)
def test_official_identifiers_require_authoritative_shape_and_host(
    identifier: OfficialIdentifier,
) -> None:
    """Lookalike URLs and incomplete registry keys cannot establish identity."""
    with pytest.raises(ValueError):
        normalize_official_identifier(identifier)


def test_conflicting_official_identifiers_prevent_domain_merge() -> None:
    """Different authoritative IDs override an otherwise matching domain."""
    left = CompanyEntity(
        record=_company("Example One", "https://example.com/one"),
        official_identifiers=[
            OfficialIdentifier(
                source=OfficialIdentifierSource.WIKIDATA,
                value="Q100",
            )
        ],
    )
    right = CompanyEntity(
        record=_company("Example Two", "https://example.com/two"),
        official_identifiers=[
            OfficialIdentifier(
                source=OfficialIdentifierSource.WIKIDATA,
                value="Q200",
            )
        ],
    )

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.KEEP_SEPARATE
    assert result.confidence == 0.99
    assert "separate official entities" in result.explanation[0]


def test_multiple_identifiers_from_one_source_require_manual_review() -> None:
    """Internally contradictory authority data fails closed before domain matching."""
    left = CompanyEntity(
        record=_company("Example", "https://example.com/"),
        official_identifiers=[
            OfficialIdentifier(
                source=OfficialIdentifierSource.WIKIDATA,
                value="Q100",
            ),
            OfficialIdentifier(
                source=OfficialIdentifierSource.WIKIDATA,
                value="Q200",
            ),
        ],
    )
    right = _company("Example", "https://www.example.com/")

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED
    assert result.merged_company is None
    assert "Multiple wikidata identifiers" in result.explanation[0]


def test_identifiers_can_be_read_from_extracted_fields() -> None:
    """Existing structured fields participate without a separate context model."""
    left = _company(
        "Brand One",
        "https://one.example/",
        fields=[
            _field(
                "opencorporates_id",
                "NL/12345678",
                0.9,
                "https://opencorporates.com/companies/nl/12345678",
            )
        ],
    )
    right = _company(
        "Brand Two",
        "https://two.example/",
        fields=[
            _field(
                "opencorporates_id",
                "https://opencorporates.com/companies/nl/12345678",
                0.9,
                "https://opencorporates.com/companies/nl/12345678",
            )
        ],
    )

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MERGE


def test_merge_keeps_highest_confidence_and_all_provenance() -> None:
    """Losing values, source URLs, and explanations survive a confidence merge."""
    left = _company(
        "Acme Labs Ltd",
        "https://www.acme.com/?utm_source=search",
        description="Older company description.",
        services=["Legacy consulting"],
        fields=[
            _field("company_name", "Acme Labs Ltd", 0.70, "https://acme.com/about"),
            _field("website_url", "https://acme.com", 0.95, "https://acme.com/"),
            _field(
                "description", "Older company description.", 0.60, "https://acme.com/"
            ),
            _field(
                "services", ["Legacy consulting"], 0.40, "https://acme.com/services"
            ),
            _field("annual_revenue", 100, 0.60, "https://acme.com/financials"),
            _field("category", "Commerce", 0.80, "https://acme.com/about"),
        ],
    )
    right = _company(
        "Acme Commerce B.V.",
        "https://acme.com/",
        description="Current evidence-backed description.",
        services=["Commerce strategy", "Development"],
        fields=[
            _field("company_name", "Acme Commerce B.V.", 0.98, "https://acme.com/"),
            _field("website_url", "https://acme.com/", 0.80, "https://acme.com/"),
            _field(
                "description",
                "Current evidence-backed description.",
                0.90,
                "https://acme.com/about",
            ),
            _field(
                "services",
                ["Commerce strategy", "Development"],
                0.95,
                "https://acme.com/services",
            ),
            _field("annual_revenue", 120, 0.96, "https://acme.com/report"),
            _field("category", "Commerce", 0.85, "https://acme.com/services"),
        ],
    )

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MERGE
    merged = result.merged_company
    metadata = result.merge_metadata
    assert merged is not None
    assert metadata is not None
    assert merged.id == right.id
    assert merged.name == "Acme Commerce B.V."
    assert str(merged.website_url) == "https://acme.com/"
    assert merged.description == "Current evidence-backed description."
    assert merged.services == ["Commerce strategy", "Development"]
    revenue = next(
        field for field in merged.extracted_fields if field.name == "annual_revenue"
    )
    assert revenue.value == 120
    assert metadata.alternative_values["name"] == ["Acme Labs Ltd"]
    assert metadata.alternative_values["description"] == ["Older company description."]
    assert metadata.alternative_values["services"] == [["Legacy consulting"]]
    assert metadata.alternative_values["annual_revenue"] == [100]
    assert set(metadata.source_record_ids) == {left.id, right.id}
    normalized_evidence = {
        normalize_company_url(str(url)) for url in metadata.evidence_urls
    }
    assert {
        "https://acme.com",
        "https://acme.com/about",
        "https://acme.com/services",
        "https://acme.com/financials",
        "https://acme.com/report",
    }.issubset(normalized_evidence)
    category = next(
        field for field in merged.extracted_fields if field.name == "category"
    )
    assert len(category.evidence) == 2
    assert metadata.explanation[-1].startswith("Merged record")
    assert result.explanation[0] in metadata.explanation


def test_merge_metadata_preserves_distinct_evidence_url_variants() -> None:
    """Fragments and tracking-bearing citations remain available for audit."""
    left = _company(
        "Example Commerce",
        "https://example.com/",
        fields=[
            _field(
                "services",
                ["Commerce"],
                0.8,
                "https://example.com/services?utm_source=search#commerce",
            )
        ],
    )
    right = _company(
        "Example Commerce B.V.",
        "https://www.example.com/",
        fields=[
            _field(
                "services",
                ["Commerce"],
                0.9,
                "https://example.com/services?utm_source=partner#platform",
            )
        ],
    )

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MERGE
    assert result.merge_metadata is not None
    evidence_urls = {str(url) for url in result.merge_metadata.evidence_urls}
    assert {
        "https://example.com/services?utm_source=search#commerce",
        "https://example.com/services?utm_source=partner#platform",
    } <= evidence_urls
    assert result.merged_company is not None
    services = next(
        field
        for field in result.merged_company.extracted_fields
        if field.name == "services"
    )
    assert len(services.evidence) == 2


def test_rejects_unsafe_thresholds_and_malformed_identifiers() -> None:
    """Configuration and official identifiers fail closed."""
    with pytest.raises(ValueError, match="between 80 and 100"):
        CompanyDeduplicationService(fuzzy_name_threshold=70)
    with pytest.raises(ValueError, match="invalid Wikidata"):
        normalize_official_identifier(
            OfficialIdentifier(
                source=OfficialIdentifierSource.WIKIDATA,
                value="not-a-qid",
            )
        )


def test_malformed_record_identifier_requires_manual_review() -> None:
    """Invalid authoritative data blocks lower-order automatic matching."""
    left = CompanyEntity(
        record=_company("Example", "https://example.com/"),
        official_identifiers=[
            OfficialIdentifier(
                source=OfficialIdentifierSource.WIKIDATA,
                value="not-a-qid",
            )
        ],
    )
    right = _company("Example", "https://www.example.com/")

    result = CompanyDeduplicationService().resolve(left, right)

    assert result.outcome is EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED
    assert "Malformed official identifier" in result.explanation[0]
