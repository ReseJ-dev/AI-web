"""Tests for API-only OpenCorporates company enrichment."""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.models import CompanyRecord, Evidence, ExtractedField
from app.models.entity_resolution import OfficialIdentifierSource
from app.providers import (
    OpenCorporatesAuthenticationError,
    OpenCorporatesConfigurationError,
    OpenCorporatesProvider,
    OpenCorporatesRateLimitError,
    OpenCorporatesResponseError,
)


def _company(
    *,
    extracted_fields: list[ExtractedField] | None = None,
) -> CompanyRecord:
    return CompanyRecord(
        research_run_id=uuid4(),
        name="Example Commerce",
        website_url="https://example.com/",
        extracted_fields=extracted_fields or [],
    )


def _company_payload(
    *,
    name: str = "EXAMPLE COMMERCE B.V.",
    jurisdiction: str = "nl",
    number: str = "12345678",
) -> dict[str, Any]:
    return {
        "company": {
            "name": name,
            "jurisdiction_code": jurisdiction,
            "company_number": number,
            "current_status": "Active",
            "registered_address_in_full": "Keizersgracht 1, Amsterdam",
            "opencorporates_url": (
                f"https://opencorporates.com/companies/{jurisdiction}/{number}"
            ),
            "registry_url": f"https://registry.example/companies/{number}",
            "source": {"publisher": "Netherlands Chamber of Commerce"},
            "officers": [{"name": "Must not be retained"}],
        }
    }


def _response(*companies: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_version": "0.4",
        "results": {
            "companies": list(companies),
            "page": 1,
            "per_page": 10,
            "total_count": len(companies),
        },
    }


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> tuple[OpenCorporatesProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenCorporatesProvider(
        api_key="registry-secret",
        licensed_data_use_allowed=True,
        client=client,
        max_retries=0,
        **kwargs,
    )
    return provider, client


def test_enriches_from_official_api_with_evidence_and_attribution() -> None:
    """One exact registry match adds only allowlisted, attributed fields."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.opencorporates.com"
        assert request.url.path == "/v0.4/companies/search"
        assert request.headers["X-API-TOKEN"] == "registry-secret"
        assert request.url.params["q"] == "Example Commerce"
        assert request.url.params["order"] == "score"
        assert "api_token" not in request.url.params
        assert "registry-secret" not in str(request.url)
        return httpx.Response(
            200,
            request=request,
            json=_response(_company_payload()),
        )

    provider, client = _provider(handler)
    try:
        result = asyncio.run(provider.enrich(_company()))
    finally:
        asyncio.run(client.aclose())

    assert result.company.name == "Example Commerce"
    assert str(result.company.website_url) == "https://example.com/"
    values = {field.name: field.value for field in result.company.extracted_fields}
    assert values == {
        "official_company_name": "EXAMPLE COMMERCE B.V.",
        "jurisdiction": "nl",
        "company_number": "12345678",
        "current_status": "Active",
        "registered_location": "Keizersgracht 1, Amsterdam",
        "official_registry_url": "https://registry.example/companies/12345678",
    }
    for field in result.company.extracted_fields:
        assert len(field.evidence) == 1
        evidence = field.evidence[0]
        assert [str(url) for url in evidence.urls] == [
            "https://opencorporates.com/companies/nl/12345678"
        ]
        assert evidence.source_title == (
            "OpenCorporates — Netherlands Chamber of Commerce"
        )
    assert len(result.official_identifiers) == 1
    assert (
        result.official_identifiers[0].source is OfficialIdentifierSource.OPENCORPORATES
    )
    assert result.official_identifiers[0].value == "nl/12345678"


def test_uses_supported_website_country_to_narrow_verification() -> None:
    """An evidenced country becomes an API filter, never a guessed location."""
    country = ExtractedField(
        name="country",
        value="Netherlands",
        confidence=0.99,
        evidence=[
            Evidence(
                urls=["https://example.com/about"],
                excerpt="Registered in the Netherlands.",
            )
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["country_code"] == "nl"
        return httpx.Response(
            200,
            request=request,
            json=_response(_company_payload()),
        )

    provider, client = _provider(handler)
    try:
        asyncio.run(provider.enrich(_company(extracted_fields=[country])))
    finally:
        asyncio.run(client.aclose())


def test_does_not_overwrite_existing_website_evidence() -> None:
    """Existing fields win even when the registry reports a different value."""
    existing = ExtractedField(
        name="current_status",
        value="Trading",
        confidence=0.99,
        evidence=[
            Evidence(
                urls=["https://example.com/about"],
                excerpt="The company is currently trading.",
            )
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_response(_company_payload()),
        )

    provider, client = _provider(handler)
    try:
        result = asyncio.run(provider.enrich(_company(extracted_fields=[existing])))
    finally:
        asyncio.run(client.aclose())

    status_fields = [
        field
        for field in result.company.extracted_fields
        if field.name == "current_status"
    ]
    assert status_fields == [existing]
    assert result.warnings == [
        "Existing evidence-backed fields were retained instead of "
        "OpenCorporates values."
    ]


def test_ambiguous_exact_matches_require_manual_review() -> None:
    """Name-only ambiguity must not attach the wrong legal entity."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_response(
                _company_payload(),
                _company_payload(jurisdiction="gb", number="99887766"),
            ),
        )

    company = _company()
    provider, client = _provider(handler)
    try:
        result = asyncio.run(provider.enrich(company))
    finally:
        asyncio.run(client.aclose())

    assert result.company == company
    assert result.official_identifiers == []
    assert "manual review" in result.warnings[0].casefold()


def test_retries_rate_limits_and_respects_retry_after() -> None:
    """Rate-limited requests retry with the larger bounded provider delay."""
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "2"},
            )
        return httpx.Response(
            200,
            request=request,
            json=_response(_company_payload()),
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenCorporatesProvider(
        api_key="registry-secret",
        licensed_data_use_allowed=True,
        client=client,
        max_retries=1,
        backoff_seconds=0.25,
        max_retry_after_seconds=5,
        sleep=record_sleep,
    )
    try:
        asyncio.run(provider.enrich(_company()))
    finally:
        asyncio.run(client.aclose())

    assert attempts == 2
    assert delays == [2.0]


def test_rate_limit_and_authentication_errors_are_typed() -> None:
    """Exhausted limits and invalid credentials remain distinguishable."""

    def exercise(
        status: int,
        error_type: type[Exception],
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, request=request)

        provider, client = _provider(handler)
        try:
            with pytest.raises(error_type):
                asyncio.run(provider.enrich(_company()))
        finally:
            asyncio.run(client.aclose())

    exercise(401, OpenCorporatesAuthenticationError)
    exercise(403, OpenCorporatesRateLimitError)


def test_rejects_malformed_success_payload() -> None:
    """Invalid API JSON never becomes partial company evidence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"results": {}})

    provider, client = _provider(handler)
    try:
        with pytest.raises(OpenCorporatesResponseError, match="invalid"):
            asyncio.run(provider.enrich(_company()))
    finally:
        asyncio.run(client.aclose())


def test_requires_credentials_and_explicit_licensed_use_permission() -> None:
    """The provider is disabled unless both deployment conditions are met."""
    with pytest.raises(OpenCorporatesConfigurationError, match="API_KEY"):
        OpenCorporatesProvider(
            api_key=None,
            licensed_data_use_allowed=True,
        )
    with pytest.raises(
        OpenCorporatesConfigurationError,
        match="LICENSED_DATA_USE_ALLOWED",
    ):
        OpenCorporatesProvider(
            api_key="registry-secret",
            licensed_data_use_allowed=False,
        )
