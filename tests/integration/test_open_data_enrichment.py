"""Mocked integration tests for Wikidata and GeoNames enrichment."""

from collections import Counter
from uuid import uuid4

import httpx
import pytest

from app.models import CompanyRecord, Evidence, ExtractedField
from app.models.entity_resolution import OfficialIdentifierSource
from app.providers import GeoNamesProvider, WikidataProvider


def _field(
    name: str, value: str, url: str = "https://example.com/about"
) -> ExtractedField:
    return ExtractedField(
        name=name,
        value=value,
        confidence=0.99,
        evidence=[Evidence(urls=[url], excerpt=f"Website reports {name}: {value}")],
    )


def _company(*fields: ExtractedField) -> CompanyRecord:
    return CompanyRecord(
        research_run_id=uuid4(),
        name="Example Commerce",
        website_url="https://example.com/",
        extracted_fields=list(fields),
    )


def _binding(
    *,
    country: str = "Netherlands",
    headquarters: str = "Amsterdam",
    industry: str = "e-commerce",
) -> dict[str, object]:
    return {
        "item": {
            "type": "uri",
            "value": "http://www.wikidata.org/entity/Q12345",
        },
        "itemLabel": {"type": "literal", "value": "Example Commerce"},
        "website": {"type": "uri", "value": "https://example.com"},
        "countryLabel": {"type": "literal", "value": country},
        "headquartersLabel": {"type": "literal", "value": headquarters},
        "industryLabel": {"type": "literal", "value": industry},
    }


def _wikidata_response(*bindings: dict[str, object]) -> dict[str, object]:
    return {
        "head": {"vars": ["item", "itemLabel", "website"]},
        "results": {"bindings": list(bindings)},
    }


def _countries() -> dict[str, object]:
    return {
        "geonames": [
            {
                "countryCode": "NL",
                "countryName": "Netherlands",
                "isoAlpha3": "NLD",
                "geonameId": 2750405,
            },
            {
                "countryCode": "BE",
                "countryName": "Belgium",
                "isoAlpha3": "BEL",
                "geonameId": 2802361,
            },
        ]
    }


def _places(
    *places: tuple[int, str, str, str],
) -> dict[str, object]:
    return {
        "totalResultsCount": len(places),
        "geonames": [
            {
                "geonameId": identifier,
                "name": name,
                "toponymName": name,
                "countryCode": country_code,
                "countryName": country_name,
            }
            for identifier, name, country_code, country_name in places
        ],
    }


@pytest.mark.anyio
async def test_wikidata_then_geonames_preserves_attribution_and_cache() -> None:
    """Validated Wikidata geography flows into cached GeoNames normalization."""
    wikidata_requests = 0

    def wikidata_handler(request: httpx.Request) -> httpx.Response:
        nonlocal wikidata_requests
        wikidata_requests += 1
        assert request.url.host == "query.wikidata.org"
        assert request.url.path == "/sparql"
        assert "Example Commerce" in request.url.params["query"]
        assert request.headers["User-Agent"] == "ResearchFixture/1.0"
        return httpx.Response(
            200,
            request=request,
            json=_wikidata_response(
                _binding(industry="e-commerce"),
                _binding(industry="software industry"),
            ),
        )

    wikidata_client = httpx.AsyncClient(transport=httpx.MockTransport(wikidata_handler))
    wikidata = WikidataProvider(
        enabled=True,
        client=wikidata_client,
        max_retries=0,
        user_agent="ResearchFixture/1.0",
    )
    try:
        wikidata_result = await wikidata.enrich(
            _company(_field("country", "Netherlands"))
        )
    finally:
        await wikidata_client.aclose()

    assert wikidata_requests == 1
    assert wikidata_result.company.name == "Example Commerce"
    assert str(wikidata_result.company.website_url) == "https://example.com/"
    assert wikidata_result.official_identifiers[0].source is (
        OfficialIdentifierSource.WIKIDATA
    )
    assert wikidata_result.official_identifiers[0].value == "Q12345"
    wikidata_fields = {
        item.name: item for item in wikidata_result.company.extracted_fields
    }
    assert wikidata_fields["headquarters_location"].value == "Amsterdam"
    assert wikidata_fields["industry"].value == [
        "e-commerce",
        "software industry",
    ]
    assert wikidata_fields["wikidata_official_website"].value == ("https://example.com")
    assert wikidata_fields["industry"].evidence[0].source_title == "Wikidata"
    assert [str(url) for url in wikidata_fields["industry"].evidence[0].urls] == [
        "https://www.wikidata.org/wiki/Q12345"
    ]

    calls: Counter[str] = Counter()

    def geonames_handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        assert request.url.host == "secure.geonames.org"
        assert request.url.params["username"] == "fixture-user"
        if request.url.path == "/countryInfoJSON":
            return httpx.Response(200, request=request, json=_countries())
        assert request.url.params["name_equals"] == "Amsterdam"
        assert request.url.params["country"] == "NL"
        return httpx.Response(
            200,
            request=request,
            json=_places((2759794, "Amsterdam", "NL", "Netherlands")),
        )

    geonames_client = httpx.AsyncClient(transport=httpx.MockTransport(geonames_handler))
    geonames = GeoNamesProvider(
        username="fixture-user",
        client=geonames_client,
        max_retries=0,
        cache_ttl_seconds=3600,
    )
    try:
        first = await geonames.enrich(wikidata_result.company)
        second = await geonames.enrich(wikidata_result.company)
    finally:
        await geonames_client.aclose()

    assert calls == Counter({"/countryInfoJSON": 1, "/searchJSON": 1})
    assert first == second
    geographic_fields = {item.name: item for item in first.company.extracted_fields}
    assert geographic_fields["normalized_country"].value == "Netherlands"
    assert geographic_fields["country_code"].value == "NL"
    assert geographic_fields["normalized_city"].value == "Amsterdam"
    assert geographic_fields["geonames_city_id"].value == "2759794"
    assert geographic_fields["normalized_city"].evidence[0].source_title == "GeoNames"
    assert [
        str(url) for url in geographic_fields["normalized_city"].evidence[0].urls
    ] == ["https://www.geonames.org/2759794"]


@pytest.mark.anyio
async def test_open_data_contradictions_warn_without_overwriting() -> None:
    """Both sources retain stronger site evidence and explain contradictions."""

    def wikidata_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_wikidata_response(
                _binding(country="Netherlands", headquarters="Rotterdam")
            ),
        )

    company = _company(
        _field("country", "Belgium"),
        _field("headquarters_location", "Amsterdam"),
        _field("normalized_country", "Belgium"),
    )
    wikidata_client = httpx.AsyncClient(transport=httpx.MockTransport(wikidata_handler))
    wikidata = WikidataProvider(
        enabled=True,
        client=wikidata_client,
        max_retries=0,
    )
    try:
        wikidata_result = await wikidata.enrich(company)
    finally:
        await wikidata_client.aclose()

    assert wikidata_result.company.extracted_fields[:3] == company.extracted_fields
    assert any("'country' contradicts" in item for item in wikidata_result.warnings)
    assert any(
        "'headquarters_location' contradicts" in item
        for item in wikidata_result.warnings
    )

    def geonames_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/countryInfoJSON":
            return httpx.Response(200, request=request, json=_countries())
        if "country" in request.url.params:
            assert request.url.params["country"] == "BE"
            return httpx.Response(200, request=request, json=_places())
        return httpx.Response(
            200,
            request=request,
            json=_places((2759794, "Amsterdam", "NL", "Netherlands")),
        )

    geonames_client = httpx.AsyncClient(transport=httpx.MockTransport(geonames_handler))
    geonames = GeoNamesProvider(
        username="fixture-user",
        client=geonames_client,
        max_retries=0,
    )
    try:
        geonames_result = await geonames.enrich(wikidata_result.company)
    finally:
        await geonames_client.aclose()

    fields = {
        item.name: item.value for item in geonames_result.company.extracted_fields
    }
    assert fields["country"] == "Belgium"
    assert fields["headquarters_location"] == "Amsterdam"
    assert fields["normalized_country"] == "Belgium"
    assert "normalized_city" not in fields
    assert any(
        "Amsterdam" in warning and "NL" in warning
        for warning in geonames_result.warnings
    )


@pytest.mark.anyio
async def test_open_data_providers_retry_transient_limits() -> None:
    """Wikidata HTTP throttling and GeoNames API credits use bounded retries."""
    delays: list[float] = []
    wikidata_attempts = 0

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    def wikidata_handler(request: httpx.Request) -> httpx.Response:
        nonlocal wikidata_attempts
        wikidata_attempts += 1
        if wikidata_attempts == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(
            200,
            request=request,
            json=_wikidata_response(_binding()),
        )

    wikidata_client = httpx.AsyncClient(transport=httpx.MockTransport(wikidata_handler))
    wikidata = WikidataProvider(
        enabled=True,
        client=wikidata_client,
        max_retries=1,
        backoff_seconds=0.25,
        sleep=record_sleep,
    )
    try:
        await wikidata.enrich(_company(_field("country", "Netherlands")))
    finally:
        await wikidata_client.aclose()

    geonames_attempts = 0

    def geonames_handler(request: httpx.Request) -> httpx.Response:
        nonlocal geonames_attempts
        geonames_attempts += 1
        if geonames_attempts == 1:
            return httpx.Response(
                200,
                request=request,
                json={"status": {"value": 19, "message": "hourly limit"}},
            )
        return httpx.Response(200, request=request, json=_countries())

    geonames_client = httpx.AsyncClient(transport=httpx.MockTransport(geonames_handler))
    geonames = GeoNamesProvider(
        username="fixture-user",
        client=geonames_client,
        max_retries=1,
        backoff_seconds=0.5,
        sleep=record_sleep,
    )
    try:
        await geonames.enrich(_company(_field("country", "Netherlands")))
    finally:
        await geonames_client.aclose()

    assert wikidata_attempts == 2
    assert geonames_attempts == 2
    assert delays == [0.25, 0.5]
