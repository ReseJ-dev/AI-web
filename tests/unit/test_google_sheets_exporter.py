"""Mocked tests for the official Google Sheets API exporter."""

import json

import httpx
import pytest

from app.exporters.google_sheets import (
    GoogleSheetsAuthenticationError,
    GoogleSheetsConfigurationError,
    GoogleSheetsExporter,
    GoogleSheetsQuotaError,
)
from app.models import (
    CompanyRecord,
    ComponentScore,
    Evidence,
    ExportContext,
    ExtractedField,
    RankedCompanyRecord,
    RelevanceComponent,
    RelevanceScoreResult,
    RequestedField,
    ResearchRequest,
    ResearchRun,
    SkippedSource,
)

SHEET_IDS = {
    "Research Results": 10,
    "Skipped Sources": 11,
    "Run Metadata": 12,
}


class _TokenProvider:
    async def get_token(self) -> str:
        return "private-test-token"


def _run() -> ResearchRun:
    return ResearchRun(
        request=ResearchRequest(
            query="Shopify agencies in the Netherlands",
            requested_fields=[
                RequestedField(name="country"),
                RequestedField(name="contact page"),
            ],
            result_count=30,
        )
    )


def _ranked(run: ResearchRun) -> RankedCompanyRecord:
    evidence = Evidence(
        urls=["https://example.nl/about", "https://example.nl/contact"],
        excerpt="This sensitive copied fragment must never be exported.",
    )
    company = CompanyRecord(
        research_run_id=run.id,
        name="=Example Commerce",
        website_url="https://example.nl/",
        description="A" * 700,
        services=["Shopify Plus", "Ecommerce development"],
        extracted_fields=[
            ExtractedField(
                name="country",
                value="Netherlands",
                confidence=0.95,
                evidence=[evidence],
            ),
            ExtractedField(
                name="contact_page",
                value="https://example.nl/contact",
                confidence=0.9,
                evidence=[evidence],
            ),
        ],
    )
    components = {
        component: ComponentScore(
            score=maximum,
            maximum=maximum,
            explanation=f"{component.value} is fully supported.",
        )
        for component, maximum in (
            (RelevanceComponent.TOPIC_MATCH, 30),
            (RelevanceComponent.COUNTRY_MATCH, 20),
            (RelevanceComponent.RELEVANT_SERVICES, 15),
            (RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE, 10),
            (RelevanceComponent.CONTACT_PAGE, 10),
            (RelevanceComponent.EVIDENCE_QUALITY, 10),
            (RelevanceComponent.REQUESTED_FIELD_COMPLETENESS, 5),
        )
    }
    return RankedCompanyRecord(
        company=company,
        relevance=RelevanceScoreResult(
            total_score=100,
            components=components,
            explanation=["Every criterion has explicit evidence."],
            missing_evidence_penalties=[],
        ),
    )


def _context(run: ResearchRun) -> ExportContext:
    return ExportContext(
        skipped_sources=[
            SkippedSource(
                research_run_id=run.id,
                url="https://blocked.example/private",
                reason="Blocked by robots policy.",
            )
        ],
        generated_queries=["Shopify agency Netherlands", "site:.nl Shopify agency"],
        providers=["brave", "deterministic", "wikidata"],
        strict_compliance_mode=True,
        warnings=["=formula-like warning"],
    )


def _metadata_payload() -> dict[str, object]:
    return {
        "sheets": [
            {"properties": {"title": title, "sheetId": sheet_id}}
            for title, sheet_id in SHEET_IDS.items()
        ]
    }


@pytest.mark.anyio
async def test_exports_allowlisted_data_and_formats_existing_spreadsheet() -> None:
    """All three sheets are batched, formatted, and free of copied evidence text."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_metadata_payload())
        return httpx.Response(200, json={})

    run = _run()
    exporter = GoogleSheetsExporter(
        spreadsheet_id="existing_sheet_123",
        token_provider=_TokenProvider(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    artifact = await exporter.export(run, [_ranked(run)], context=_context(run))

    assert artifact.location.endswith("/existing_sheet_123")
    assert artifact.record_count == 1
    assert len(calls) == 4
    assert all(
        request.headers["Authorization"] == "Bearer private-test-token"
        for request in calls
    )
    assert all("private-test-token" not in str(request.url) for request in calls)

    clear_payload = json.loads(calls[1].content)
    assert clear_payload["ranges"] == [
        "'Research Results'!A:ZZ",
        "'Skipped Sources'!A:ZZ",
        "'Run Metadata'!A:ZZ",
    ]
    values_payload = json.loads(calls[2].content)
    assert values_payload["valueInputOption"] == "RAW"
    ranges = {item["range"]: item["values"] for item in values_payload["data"]}
    assert set(ranges) == {
        "'Research Results'!A1",
        "'Skipped Sources'!A1",
        "'Run Metadata'!A1",
    }
    research = ranges["'Research Results'!A1"]
    assert research[0] == [
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
    ]
    assert research[1][0] == "'=Example Commerce"
    assert research[1][2] == "Netherlands"
    assert len(research[1][5]) == 500
    serialized_values = json.dumps(values_payload)
    assert "sensitive copied fragment" not in serialized_values
    assert "raw search" not in serialized_values

    formatting_payload = json.loads(calls[3].content)
    formatting = formatting_payload["requests"]
    assert any("updateSheetProperties" in item for item in formatting)
    assert any("setBasicFilter" in item for item in formatting)
    assert any("updateDimensionProperties" in item for item in formatting)
    assert any(
        item.get("repeatCell", {})
        .get("cell", {})
        .get("userEnteredFormat", {})
        .get("wrapStrategy")
        == "WRAP"
        for item in formatting
    )
    await exporter.aclose()


@pytest.mark.anyio
async def test_creates_spreadsheet_only_when_explicitly_permitted() -> None:
    """Creation uses exact required sheet names and the configured title."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v4/spreadsheets":
            return httpx.Response(
                200,
                json={"spreadsheetId": "created-id", **_metadata_payload()},
            )
        return httpx.Response(200, json={})

    exporter = GoogleSheetsExporter(
        create_allowed=True,
        spreadsheet_title="Research delivery",
        token_provider=_TokenProvider(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    artifact = await exporter.export(_run(), [])

    create_payload = json.loads(calls[0].content)
    assert create_payload["properties"]["title"] == "Research delivery"
    assert [item["properties"]["title"] for item in create_payload["sheets"]] == [
        "Research Results",
        "Skipped Sources",
        "Run Metadata",
    ]
    assert artifact.location.endswith("/created-id")
    await exporter.aclose()


def test_requires_existing_id_or_explicit_creation_permission() -> None:
    """The exporter never creates a spreadsheet through an implicit default."""
    with pytest.raises(GoogleSheetsConfigurationError, match="explicitly permit"):
        GoogleSheetsExporter(
            spreadsheet_id=None,
            create_allowed=False,
            token_provider=_TokenProvider(),
        )


@pytest.mark.anyio
async def test_adds_missing_sheets_to_existing_spreadsheet() -> None:
    """Required tabs absent from an existing spreadsheet are added in one batch."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sheets": [
                        {
                            "properties": {
                                "title": "Research Results",
                                "sheetId": 10,
                            }
                        }
                    ]
                },
            )
        if len(calls) == 2:
            return httpx.Response(
                200,
                json={
                    "replies": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": "Skipped Sources",
                                    "sheetId": 11,
                                }
                            }
                        },
                        {
                            "addSheet": {
                                "properties": {
                                    "title": "Run Metadata",
                                    "sheetId": 12,
                                }
                            }
                        },
                    ]
                },
            )
        return httpx.Response(200, json={})

    exporter = GoogleSheetsExporter(
        spreadsheet_id="existing",
        token_provider=_TokenProvider(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await exporter.export(_run(), [])

    add_payload = json.loads(calls[1].content)
    assert len(add_payload["requests"]) == 2
    assert all("addSheet" in request for request in add_payload["requests"])
    await exporter.aclose()


@pytest.mark.anyio
async def test_retries_quota_errors_with_retry_after() -> None:
    """Quota responses use bounded Retry-After delays before succeeding."""
    attempts = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "GET":
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "2"},
                    json={"error": {"message": "quota"}},
                )
            return httpx.Response(200, json=_metadata_payload())
        return httpx.Response(200, json={})

    exporter = GoogleSheetsExporter(
        spreadsheet_id="existing",
        token_provider=_TokenProvider(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=1,
        sleep=sleep,
    )
    await exporter.export(_run(), [])

    assert attempts == 2
    assert delays == [2.0]
    await exporter.aclose()


@pytest.mark.anyio
async def test_retries_server_errors_with_exponential_backoff() -> None:
    """Transient server failures use deterministic exponential delays."""
    attempts = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "GET":
            attempts += 1
            if attempts < 3:
                return httpx.Response(503, json={"error": {"message": "busy"}})
            return httpx.Response(200, json=_metadata_payload())
        return httpx.Response(200, json={})

    exporter = GoogleSheetsExporter(
        spreadsheet_id="existing",
        token_provider=_TokenProvider(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=2,
        backoff_seconds=0.25,
        sleep=sleep,
    )
    await exporter.export(_run(), [])

    assert attempts == 3
    assert delays == [0.25, 0.5]
    await exporter.aclose()


@pytest.mark.anyio
async def test_exhausted_quota_and_permission_errors_are_typed() -> None:
    """Quota exhaustion differs from a non-quota permission rejection."""

    def quota_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "errors": [{"reason": "rateLimitExceeded"}],
                }
            },
        )

    quota_exporter = GoogleSheetsExporter(
        spreadsheet_id="existing",
        token_provider=_TokenProvider(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(quota_handler)),
        max_retries=0,
    )
    with pytest.raises(GoogleSheetsQuotaError):
        await quota_exporter.export(_run(), [])
    await quota_exporter.aclose()

    def permission_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "forbidden"}})

    permission_exporter = GoogleSheetsExporter(
        spreadsheet_id="existing",
        token_provider=_TokenProvider(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(permission_handler)),
    )
    with pytest.raises(GoogleSheetsAuthenticationError):
        await permission_exporter.export(_run(), [])
    await permission_exporter.aclose()
