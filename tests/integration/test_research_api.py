"""Integration tests for asynchronous FastAPI research endpoints."""

import asyncio
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.dependencies import _google_exporter_factory
from app.api.main import create_app
from app.core.database import create_database_engine
from app.core.settings import Settings, reload_settings
from app.exporters import ResultExporter
from app.models import (
    CompanyRecord,
    ComponentScore,
    Evidence,
    ExportArtifact,
    ExportContext,
    ExtractedField,
    RankedCompanyRecord,
    RelevanceComponent,
    RelevanceScoreResult,
    ResearchOrchestrationResult,
    ResearchProgressEvent,
    ResearchProgressStage,
    ResearchRequest,
    ResearchRun,
    ResearchRunStatus,
    SkippedSource,
)
from app.models.domain import utc_now
from app.models.persistence import Base
from app.repositories import (
    SqlAlchemyCompanyRecordRepository,
    SqlAlchemyResearchRunRepository,
    SqlAlchemySkippedSourceRepository,
)
from app.services.research_api import ProgressCallback, ResearchRunApplicationService

REQUEST_BODY = {
    "topic": "Shopify agencies in the Netherlands",
    "requested_fields": [
        {"name": "country"},
        {"name": "services"},
        {"name": "contact page"},
    ],
    "result_count": 3,
    "location": "Netherlands",
    "country": "NL",
    "language": "en",
    "country_tld": "nl",
}


def _relevance(score: int = 100) -> RelevanceScoreResult:
    maxima = {
        RelevanceComponent.TOPIC_MATCH: 30,
        RelevanceComponent.COUNTRY_MATCH: 20,
        RelevanceComponent.RELEVANT_SERVICES: 15,
        RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE: 10,
        RelevanceComponent.CONTACT_PAGE: 10,
        RelevanceComponent.EVIDENCE_QUALITY: 10,
        RelevanceComponent.REQUESTED_FIELD_COMPLETENESS: 5,
    }
    assert score == 100
    return RelevanceScoreResult(
        total_score=score,
        components={
            component: ComponentScore(
                score=maximum,
                maximum=maximum,
                explanation="Explicit evidence supports this component.",
            )
            for component, maximum in maxima.items()
        },
        explanation=["All criteria have explicit supporting evidence."],
        missing_evidence_penalties=[],
    )


def _records(run_id: UUID, count: int = 3) -> list[RankedCompanyRecord]:
    records: list[RankedCompanyRecord] = []
    for index in range(count):
        root = f"https://agency-{index}.example/"
        evidence = Evidence(
            urls=[f"{root}about", f"{root}contact"],
            excerpt="This copied source fragment is private to extraction.",
        )
        company = CompanyRecord(
            research_run_id=run_id,
            name=f"Agency {index}",
            website_url=root,
            description=f"Agency {index} builds evidenced Shopify stores.",
            services=["Shopify development"],
            extracted_fields=[
                ExtractedField(
                    name="country",
                    value="Netherlands",
                    confidence=0.95,
                    evidence=[evidence],
                ),
                ExtractedField(
                    name="contact_page",
                    value=f"{root}contact",
                    confidence=0.9,
                    evidence=[evidence],
                ),
            ],
        )
        records.append(RankedCompanyRecord(company=company, relevance=_relevance()))
    return records


async def _notify(
    callback: ProgressCallback | None,
    event: ResearchProgressEvent,
) -> None:
    if callback is None:
        return
    outcome = callback(event)
    if asyncio.iscoroutine(outcome):
        await outcome


class _FakeWorkflow:
    def __init__(
        self,
        *,
        delay: float = 0,
        failed: bool = False,
        warning: str | None = None,
    ) -> None:
        self.delay = delay
        self.failed = failed
        self.warning = warning

    async def run(
        self,
        request: ResearchRequest | Mapping[str, object],
        *,
        location: str,
        country: str = "US",
        language: str = "en",
        city: str | None = None,
        country_tld: str | None = None,
        export_formats: Sequence[str] = (),
        on_progress: ProgressCallback | None = None,
        run_id: UUID | None = None,
    ) -> ResearchOrchestrationResult:
        validated = ResearchRequest.model_validate(request)
        assert run_id is not None
        started = utc_now()
        await _notify(
            on_progress,
            ResearchProgressEvent(
                sequence=1,
                stage=ResearchProgressStage.PLANNING,
                message="Planning deterministic queries.",
                completed_items=0,
                total_items=validated.result_count,
            ),
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        records = _records(run_id, 2 if self.failed else 3)
        if self.failed:
            final_stage = ResearchProgressStage.FAILED
        elif self.warning:
            final_stage = ResearchProgressStage.COMPLETED_WITH_WARNINGS
        else:
            final_stage = ResearchProgressStage.COMPLETED
        status = (
            ResearchRunStatus.FAILED if self.failed else ResearchRunStatus.COMPLETED
        )
        warnings = ["Search budget ended with partial results."] if self.failed else []
        if self.warning:
            warnings.append(self.warning)
        completed = ResearchProgressEvent(
            sequence=2,
            stage=final_stage,
            message="Research reached a terminal state.",
            completed_items=len(records),
            total_items=validated.result_count,
            warning=self.failed,
        )
        await _notify(on_progress, completed)
        run = ResearchRun(
            id=run_id,
            request=validated,
            status=status,
            error_message="Search provider stopped early." if self.failed else None,
            created_at=started,
            updated_at=completed.occurred_at,
        )
        skipped = SkippedSource(
            research_run_id=run_id,
            url="https://blocked.example/private",
            reason="Blocked by source policy.",
        )
        return ResearchOrchestrationResult(
            run=run,
            final_stage=final_stage,
            records=records,
            skipped_sources=[skipped],
            events=[
                ResearchProgressEvent(
                    sequence=1,
                    stage=ResearchProgressStage.PLANNING,
                    message="Planning deterministic queries.",
                    completed_items=0,
                    total_items=validated.result_count,
                    occurred_at=started,
                ),
                completed,
            ],
            warnings=warnings,
            search_requests_used=1,
            candidates_discovered=len(records),
        )


class _FakeSheetsExporter(ResultExporter):
    format_name = "google_sheets"

    def __init__(self) -> None:
        self.context: ExportContext | None = None
        self.requested_spreadsheet_id: str | None = None

    async def export(
        self,
        run: ResearchRun,
        records: Sequence[RankedCompanyRecord],
        *,
        context: ExportContext | None = None,
    ) -> ExportArtifact:
        self.context = context
        return ExportArtifact(
            format_name=self.format_name,
            location=f"https://docs.google.com/spreadsheets/d/{run.id}",
            record_count=len(records),
        )


def _client(
    workflow: _FakeWorkflow,
    *,
    exporter: _FakeSheetsExporter | None = None,
    settings: Settings | None = None,
) -> TestClient:
    def exporter_factory(
        spreadsheet_id: str | None,
    ) -> ResultExporter:
        assert exporter is not None
        exporter.requested_spreadsheet_id = spreadsheet_id
        return exporter

    service = ResearchRunApplicationService(
        workflow=workflow,
        google_sheets_exporter_factory=(
            exporter_factory if exporter is not None else None
        ),
        settings=settings or Settings(_env_file=None),
    )
    return TestClient(create_app(service))


def _wait_for_terminal(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/api/research-runs/{run_id}")
        payload = cast(dict[str, Any], response.json())
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("research run did not finish")


def test_run_polling_paginated_results_and_skipped_sources() -> None:
    """Submission returns 202 and exposes progress, pagination, and source audits."""
    with _client(_FakeWorkflow(delay=0.05)) as client:
        created = client.post(
            "/api/research-runs",
            json=REQUEST_BODY,
            headers={"X-Request-ID": "research-request-123"},
        )
        assert created.status_code == 202
        assert created.headers["X-Request-ID"] == "research-request-123"
        run_id = created.json()["id"]

        progress = client.get(f"/api/research-runs/{run_id}")
        assert progress.status_code == 200
        assert progress.json()["progress_stage"] == "planning"

        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "completed"
        assert terminal["partial_result_count"] == 3
        assert terminal["discovered_candidate_count"] == 3
        assert terminal["approved_candidate_count"] == 3
        assert terminal["skipped_source_count"] == 1
        assert terminal["completed_result_count"] == 3

        results = client.get(
            f"/api/research-runs/{run_id}/results",
            params={"offset": 1, "limit": 1},
        )
        assert results.status_code == 200
        body = results.json()
        assert body["total"] == 3
        assert body["offset"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["company_name"] == "Agency 1"
        assert body["items"][0]["relevance_score"] == 100
        assert body["items"][0]["country"] == "Netherlands"
        assert "copied source fragment" not in results.text

        skipped = client.get(f"/api/research-runs/{run_id}/skipped-sources")
        assert skipped.status_code == 200
        assert skipped.json()["items"][0]["domain"] == "blocked.example"


def test_failed_run_retains_partial_results() -> None:
    """A failed workflow remains pollable and serves verified partial records."""
    with _client(_FakeWorkflow(failed=True)) as client:
        run_id = client.post("/api/research-runs", json=REQUEST_BODY).json()["id"]
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "failed"
        assert terminal["partial_result_count"] == 2
        assert terminal["warnings"]

        response = client.get(f"/api/research-runs/{run_id}/results")
        assert response.status_code == 200
        assert response.json()["partial"] is True
        assert response.json()["total"] == 2


def test_google_sheets_export_uses_terminal_run_context() -> None:
    """The export action delegates outside the route and returns an artifact."""
    exporter = _FakeSheetsExporter()
    with _client(_FakeWorkflow(), exporter=exporter) as client:
        run_id = client.post("/api/research-runs", json=REQUEST_BODY).json()["id"]
        _wait_for_terminal(client, run_id)

        response = client.post(
            f"/api/research-runs/{run_id}/export/google-sheets",
            json={"spreadsheet_id": "portfolio-sheet-123"},
        )
        assert response.status_code == 200
        assert response.json()["artifact"]["record_count"] == 3
        assert response.json()["artifact"]["format_name"] == "google_sheets"
        assert exporter.context is not None
        assert exporter.context.generated_queries
        assert exporter.context.skipped_sources
        assert exporter.requested_spreadsheet_id == "portfolio-sheet-123"


def test_google_factory_allows_per_request_target_without_server_default() -> None:
    """Credentials alone make the user-supplied spreadsheet path available."""
    settings = Settings(
        _env_file=None,
        api_access_token="portfolio-api-secret",
        google_service_account_json='{"type":"service_account"}',
        google_sheets_spreadsheet_id=None,
        google_sheets_create_allowed=False,
    )

    assert _google_exporter_factory(settings) is not None


def test_structured_errors_request_ids_and_export_conflict() -> None:
    """Validation, missing resources, and lifecycle conflicts share one envelope."""
    with _client(_FakeWorkflow(delay=0.1), exporter=_FakeSheetsExporter()) as client:
        invalid = client.post(
            "/api/research-runs",
            json={**REQUEST_BODY, "result_count": 101},
        )
        assert invalid.status_code == 422
        assert invalid.headers["X-Request-ID"]
        error = invalid.json()["error"]
        assert error["code"] == "validation_error"
        assert error["request_id"] == invalid.headers["X-Request-ID"]
        assert error["details"][0]["location"][-1] == "result_count"
        assert "input" not in error["details"][0]

        duplicate_fields = client.post(
            "/api/research-runs",
            json={
                **REQUEST_BODY,
                "requested_fields": [
                    {"name": "Contact Page"},
                    {"name": "contact-page"},
                ],
            },
        )
        assert duplicate_fields.status_code == 422
        assert duplicate_fields.json()["error"]["code"] == "validation_error"

        missing = client.get("/api/research-runs/00000000-0000-0000-0000-000000000000")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "research_run_not_found"

        unknown = client.get("/api/not-a-route")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "http_error"

        run_id = client.post("/api/research-runs", json=REQUEST_BODY).json()["id"]
        conflict = client.post(f"/api/research-runs/{run_id}/export/google-sheets")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "research_run_conflict"


def test_unconfigured_workflow_returns_structured_service_unavailable() -> None:
    """A missing search provider never starts a misleading empty run."""
    service = ResearchRunApplicationService(
        workflow=None,
        settings=Settings(_env_file=None),
    )
    with TestClient(create_app(service)) as client:
        response = client.post("/api/research-runs", json=REQUEST_BODY)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert response.headers["X-Request-ID"]


def test_mutating_endpoints_require_configured_bearer_token(
    monkeypatch: Any,
) -> None:
    """Paid or externally mutating operations are protected when configured."""
    monkeypatch.setenv("API_ACCESS_TOKEN", "portfolio-api-secret")
    reload_settings()
    try:
        with _client(_FakeWorkflow()) as client:
            unauthorized = client.post("/api/research-runs", json=REQUEST_BODY)
            authorized = client.post(
                "/api/research-runs",
                json=REQUEST_BODY,
                headers={"Authorization": "Bearer portfolio-api-secret"},
            )
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "http_error"
        assert authorized.status_code == 202
    finally:
        monkeypatch.delenv("API_ACCESS_TOKEN")
        reload_settings()


def test_restart_marks_orphaned_run_failed_and_keeps_persisted_results(
    tmp_path: Path,
) -> None:
    """A process restart cannot leave a durable run permanently in progress."""
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'restart.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    runs = SqlAlchemyResearchRunRepository(sessions)
    companies = SqlAlchemyCompanyRecordRepository(sessions)
    skipped = SqlAlchemySkippedSourceRepository(sessions)
    run = ResearchRun(
        request=ResearchRequest(
            query="Shopify agencies",
            requested_fields=[{"name": "company_name"}],
            result_count=1,
        ),
        status=ResearchRunStatus.RUNNING,
    )
    runs.add(run)
    companies.add(
        CompanyRecord(
            research_run_id=run.id,
            name="Persisted Studio",
            website_url="https://persisted.example/",
        )
    )
    service = ResearchRunApplicationService(
        workflow=None,
        run_repository=runs,
        company_repository=companies,
        skipped_source_repository=skipped,
        settings=Settings(_env_file=None),
    )

    with TestClient(create_app(service)) as client:
        status_response = client.get(f"/api/research-runs/{run.id}")
        results_response = client.get(f"/api/research-runs/{run.id}/results")

    stored = runs.get(run.id)
    assert status_response.json()["status"] == "failed"
    assert status_response.json()["completed_result_count"] == 1
    assert results_response.json()["items"][0]["company_name"] == "Persisted Studio"
    assert stored is not None
    assert stored.status is ResearchRunStatus.FAILED


def test_provider_config_health_and_openapi_never_expose_keys(
    monkeypatch: Any,
) -> None:
    """Configuration is boolean-only and OpenAPI documents all required routes."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-private")
    monkeypatch.setenv("API_ACCESS_TOKEN", "portfolio-api-secret")
    monkeypatch.setenv("LLM_PROVIDER", "http")
    monkeypatch.setenv("LLM_API_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "llm-private")
    settings = Settings(_env_file=None)
    with _client(
        _FakeWorkflow(warning="Provider brave-private returned a warning."),
        settings=settings,
    ) as client:
        providers = client.get("/api/config/providers")
        assert providers.status_code == 200
        assert "brave-private" not in providers.text
        assert "llm-private" not in providers.text
        assert all("api_key" not in item for item in providers.json()["providers"])

        run_id = client.post(
            "/api/research-runs",
            json=REQUEST_BODY,
            headers={"Authorization": "Bearer portfolio-api-secret"},
        ).json()["id"]
        terminal = _wait_for_terminal(client, run_id)
        assert "brave-private" not in str(terminal)
        assert "[REDACTED]" in str(terminal)

        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/health").json() == {"status": "ok"}

        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert "/api/research-runs" in paths
        assert "/api/research-runs/{run_id}" in paths
        assert "/api/research-runs/{run_id}/results" in paths
        assert "/api/research-runs/{run_id}/skipped-sources" in paths
        assert "/api/research-runs/{run_id}/export/google-sheets" in paths
        assert "/api/config/providers" in paths
        assert "/api/health" in paths
        request_schema = schema["components"]["schemas"]["CreateResearchRunRequest"]
        assert request_schema["examples"][0]["topic"].startswith("Shopify")
