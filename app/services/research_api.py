"""Application service backing asynchronous research API endpoints."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import HttpUrl, ValidationError

from app.api.schemas import (
    CreateResearchRunRequest,
    GoogleSheetsExportResponse,
    ProvidersResponse,
    ProviderStatus,
    ResearchResultItem,
    ResearchResultsResponse,
    ResearchRunResponse,
    SkippedSourceItem,
    SkippedSourcesResponse,
)
from app.core.settings import Settings, get_settings
from app.exporters.google_sheets import GoogleSheetsExporterError
from app.exporters.interfaces import ResultExporter
from app.models import (
    CompanyRecord,
    ExportContext,
    RankedCompanyRecord,
    ResearchOrchestrationResult,
    ResearchProgressEvent,
    ResearchProgressStage,
    ResearchRequest,
    ResearchRun,
    ResearchRunStatus,
    SkippedSource,
)
from app.models.domain import utc_now
from app.repositories import (
    CompanyRecordRepository,
    ResearchRunRepository,
    SkippedSourceRepository,
)
from app.services.query_planner import QueryPlanner

ProgressCallback = Callable[[ResearchProgressEvent], Awaitable[None] | None]


class ResearchWorkflow(Protocol):
    """Minimal orchestrator contract needed by the API application service."""

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
        """Execute one research workflow."""
        ...


class ResearchApiError(RuntimeError):
    """Base expected error raised by the research API service."""


class ResearchRunNotFoundError(ResearchApiError):
    """Raised when a run identifier is unknown."""


class ResearchRunConflictError(ResearchApiError):
    """Raised when an operation conflicts with the run lifecycle."""


class ResearchProviderUnavailableError(ResearchApiError):
    """Raised when a required provider has not been configured."""


@dataclass(slots=True)
class _RunState:
    """Mutable, process-local view used for progress polling."""

    run: ResearchRun
    input: CreateResearchRunRequest
    latest_progress: ResearchProgressEvent | None = None
    result: ResearchOrchestrationResult | None = None
    warnings: list[str] = field(default_factory=list)


ExporterFactory = Callable[[str | None], ResultExporter]


class ResearchRunApplicationService:
    """Coordinate background workflows and shape safe API responses."""

    def __init__(
        self,
        *,
        workflow: ResearchWorkflow | None,
        google_sheets_exporter_factory: ExporterFactory | None = None,
        run_repository: ResearchRunRepository | None = None,
        company_repository: CompanyRecordRepository | None = None,
        skipped_source_repository: SkippedSourceRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._workflow = workflow
        self._google_sheets_exporter_factory = google_sheets_exporter_factory
        self._run_repository = run_repository
        self._company_repository = company_repository
        self._skipped_source_repository = skipped_source_repository
        self._settings = settings or get_settings()
        self._secret_values = tuple(
            secret
            for secret in (
                self._secret_value(self._settings.brave_search_api_key),
                self._secret_value(self._settings.llm_api_key),
                self._secret_value(self._settings.opencorporates_api_key),
                self._secret_value(self._settings.geonames_username),
                self._secret_value(self._settings.google_service_account_json),
            )
            if secret
        )
        self._states: dict[UUID, _RunState] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    async def submit(self, request: CreateResearchRunRequest) -> ResearchRunResponse:
        """Start a background run and return immediately for progress polling."""
        if self._workflow is None:
            raise ResearchProviderUnavailableError(
                "The research workflow is unavailable until a search provider "
                "is configured."
            )
        run = ResearchRun(
            id=uuid4(),
            request=self._domain_request(request),
            status=ResearchRunStatus.RUNNING,
        )
        state = _RunState(run=run, input=request)
        self._states[run.id] = state
        task = asyncio.create_task(
            self._execute(state),
            name=f"research-run-{run.id}",
        )
        self._tasks[run.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run.id, None))
        await asyncio.sleep(0)
        return self._run_response(state)

    def get_run(self, run_id: UUID) -> ResearchRunResponse:
        """Return the latest run and progress state."""
        return self._run_response(self._get_state(run_id))

    def get_results(
        self,
        run_id: UUID,
        *,
        offset: int,
        limit: int,
    ) -> ResearchResultsResponse:
        """Return a deterministic paginated view, including partial results."""
        state = self._get_state(run_id)
        ranked = state.result.records if state.result is not None else []
        if ranked:
            all_items = [self._result_item(item, state.warnings) for item in ranked]
        else:
            companies = (
                self._company_repository.list_for_run(run_id)
                if self._company_repository is not None
                else []
            )
            all_items = [
                self._company_item(company, state.warnings) for company in companies
            ]
        total = len(all_items)
        partial = (
            state.run.status is not ResearchRunStatus.COMPLETED
            or total < state.run.request.result_count
        )
        return ResearchResultsResponse(
            run_id=run_id,
            items=all_items[offset : offset + limit],
            total=total,
            offset=offset,
            limit=limit,
            partial=partial,
        )

    def get_skipped_sources(
        self,
        run_id: UUID,
        *,
        offset: int,
        limit: int,
    ) -> SkippedSourcesResponse:
        """Return paginated skipped-source audit data."""
        state = self._get_state(run_id)
        sources = (
            state.result.skipped_sources
            if state.result is not None
            else (
                self._skipped_source_repository.list_for_run(run_id)
                if self._skipped_source_repository is not None
                else []
            )
        )
        items = [self._skipped_item(source) for source in sources]
        return SkippedSourcesResponse(
            run_id=run_id,
            items=items[offset : offset + limit],
            total=len(items),
            offset=offset,
            limit=limit,
        )

    async def export_google_sheets(
        self,
        run_id: UUID,
        *,
        spreadsheet_id: str | None = None,
    ) -> GoogleSheetsExportResponse:
        """Export a terminal run without exposing Google credentials."""
        state = self._get_state(run_id)
        if state.result is None or state.run.status is ResearchRunStatus.RUNNING:
            raise ResearchRunConflictError(
                "Google Sheets export is available after the run reaches a "
                "terminal state."
            )
        if self._google_sheets_exporter_factory is None:
            raise ResearchProviderUnavailableError(
                "Google Sheets export is not configured."
            )
        try:
            exporter = self._google_sheets_exporter_factory(spreadsheet_id)
        except GoogleSheetsExporterError as error:
            raise ResearchProviderUnavailableError(
                "Google Sheets export could not be initialized."
            ) from error
        queries = QueryPlanner().plan(
            state.run.request.query,
            location=state.input.location,
            city=state.input.city,
            country_tld=state.input.country_tld,
        )
        try:
            try:
                artifact = await exporter.export(
                    state.run,
                    state.result.records,
                    context=ExportContext(
                        skipped_sources=state.result.skipped_sources,
                        generated_queries=queries,
                        providers=[
                            provider.name
                            for provider in self.provider_config().providers
                            if provider.enabled
                        ],
                        strict_compliance_mode=self._settings.robots_strict_mode,
                        warnings=[
                            self._safe_text(warning)
                            for warning in state.result.warnings
                        ],
                        completion_time=state.run.updated_at,
                    ),
                )
            except GoogleSheetsExporterError as error:
                raise ResearchProviderUnavailableError(
                    "Google Sheets export could not be completed."
                ) from error
        finally:
            close = getattr(exporter, "aclose", None)
            if callable(close):
                outcome = close()
                if inspect.isawaitable(outcome):
                    await outcome
        return GoogleSheetsExportResponse(run_id=run_id, artifact=artifact)

    def provider_config(self) -> ProvidersResponse:
        """Return only non-secret provider state."""
        settings = self._settings
        brave_configured = self._secret_present(settings.brave_search_api_key)
        llm_selected = settings.llm_provider.casefold() != "disabled"
        llm_configured = (
            llm_selected
            and bool(settings.llm_api_url)
            and self._secret_present(settings.llm_api_key)
        )
        sheets_credentials = bool(settings.google_service_account_file) or (
            self._secret_present(settings.google_service_account_json)
        )
        sheets_target = bool(settings.google_sheets_spreadsheet_id) or (
            settings.google_sheets_create_allowed
        )
        providers = [
            ProviderStatus(
                name="brave_search",
                category="search",
                enabled=self._workflow is not None and brave_configured,
                configured=brave_configured,
            ),
            ProviderStatus(
                name="deterministic",
                category="extraction",
                enabled=self._workflow is not None,
                configured=True,
            ),
            ProviderStatus(
                name=settings.llm_provider,
                category="extraction",
                enabled=llm_configured,
                configured=llm_configured,
                model=settings.llm_model if llm_selected else None,
            ),
            ProviderStatus(
                name="opencorporates",
                category="enrichment",
                enabled=(
                    self._secret_present(settings.opencorporates_api_key)
                    and settings.opencorporates_licensed_data_use_allowed
                ),
                configured=self._secret_present(settings.opencorporates_api_key),
            ),
            ProviderStatus(
                name="wikidata",
                category="enrichment",
                enabled=settings.wikidata_enabled,
                configured=settings.wikidata_enabled,
            ),
            ProviderStatus(
                name="geonames",
                category="enrichment",
                enabled=self._secret_present(settings.geonames_username),
                configured=self._secret_present(settings.geonames_username),
            ),
            ProviderStatus(
                name="google_sheets",
                category="export",
                enabled=(
                    self._google_sheets_exporter_factory is not None
                    and sheets_credentials
                    and sheets_target
                ),
                configured=sheets_credentials and sheets_target,
            ),
        ]
        return ProvidersResponse(providers=providers)

    async def shutdown(self) -> None:
        """Cancel active process-local tasks during application shutdown."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        close = getattr(self._workflow, "aclose", None)
        if callable(close):
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome

    async def _execute(self, state: _RunState) -> None:
        """Execute one workflow while retaining terminal partial results."""

        async def on_progress(event: ResearchProgressEvent) -> None:
            state.latest_progress = event
            state.run = state.run.model_copy(update={"updated_at": event.occurred_at})

        workflow = self._workflow
        if workflow is None:
            return
        try:
            result = await workflow.run(
                state.run.request,
                location=state.input.location,
                country=state.input.country,
                language=state.input.language,
                city=state.input.city,
                country_tld=state.input.country_tld,
                on_progress=on_progress,
                run_id=state.run.id,
            )
            state.result = result
            state.run = result.run
            state.warnings = [self._safe_text(warning) for warning in result.warnings]
            if result.events:
                state.latest_progress = result.events[-1]
        except asyncio.CancelledError:
            state.run = state.run.model_copy(
                update={
                    "status": ResearchRunStatus.CANCELLED,
                    "updated_at": utc_now(),
                }
            )
            raise
        except Exception:
            state.run = state.run.model_copy(
                update={
                    "status": ResearchRunStatus.FAILED,
                    "error_message": "The research workflow failed unexpectedly.",
                    "updated_at": utc_now(),
                }
            )
            state.latest_progress = ResearchProgressEvent(
                sequence=1,
                stage=ResearchProgressStage.FAILED,
                message="Research failed unexpectedly.",
                total_items=state.run.request.result_count,
            )

    def _get_state(self, run_id: UUID) -> _RunState:
        state = self._states.get(run_id)
        if state is not None:
            return state
        stored = self._run_repository.get(run_id) if self._run_repository else None
        if stored is None:
            raise ResearchRunNotFoundError(f"Research run {run_id} was not found.")
        fallback_input = CreateResearchRunRequest(
            topic=stored.request.query,
            requested_fields=stored.request.requested_fields,
            result_count=stored.request.result_count,
            location="unspecified",
        )
        state = _RunState(run=stored, input=fallback_input)
        self._states[run_id] = state
        return state

    @staticmethod
    def _domain_request(request: CreateResearchRunRequest) -> ResearchRequest:
        return ResearchRequest(
            query=request.topic,
            requested_fields=request.requested_fields,
            result_count=request.result_count,
        )

    def _run_response(self, state: _RunState) -> ResearchRunResponse:
        progress = state.latest_progress
        records = state.result.records if state.result is not None else []
        skipped_sources = (
            state.result.skipped_sources if state.result is not None else []
        )
        discovered = (
            state.result.candidates_discovered if state.result is not None else 0
        )
        return ResearchRunResponse(
            id=state.run.id,
            status=state.run.status,
            progress_stage=progress.stage if progress else None,
            progress_message=progress.message if progress else None,
            completed_items=progress.completed_items or 0 if progress else 0,
            total_items=state.run.request.result_count,
            partial_result_count=len(records),
            discovered_candidate_count=discovered,
            approved_candidate_count=len(records),
            skipped_source_count=len(skipped_sources),
            completed_result_count=len(records),
            warnings=[self._safe_text(warning) for warning in state.warnings],
            error_message=(
                "The research workflow failed."
                if state.run.status is ResearchRunStatus.FAILED
                else None
            ),
            created_at=state.run.created_at,
            updated_at=state.run.updated_at,
        )

    def _result_item(
        self,
        ranked: RankedCompanyRecord,
        warnings: Sequence[str],
    ) -> ResearchResultItem:
        return self._company_item(
            ranked.company,
            warnings,
            relevance_score=ranked.relevance.total_score,
            relevance_explanation=ranked.relevance.explanation,
        )

    def _company_item(
        self,
        company: CompanyRecord,
        warnings: Sequence[str],
        *,
        relevance_score: int | None = None,
        relevance_explanation: Sequence[str] = (),
    ) -> ResearchResultItem:
        evidence_urls = sorted(
            {
                str(url)
                for field in company.extracted_fields
                for evidence in field.evidence
                for url in evidence.urls
            }
        )
        country = self._field_value(
            company,
            ("country", "location", "jurisdiction"),
        )
        contact = self._field_value(
            company,
            ("contact_page", "contact_page_url", "contact"),
        )
        try:
            contact_url = HttpUrl(contact) if contact else None
        except ValidationError:
            contact_url = None
        company_warnings = [
            warning
            for warning in warnings
            if company.name.casefold() in warning.casefold()
            or (
                company.website_url is not None
                and str(company.website_url).casefold() in warning.casefold()
            )
        ]
        return ResearchResultItem(
            id=company.id,
            company_name=company.name,
            website=company.website_url,
            country=country,
            services=company.services,
            contact_page=contact_url,
            short_summary=(company.description or "")[:500] or None,
            relevance_score=relevance_score,
            relevance_explanation=list(relevance_explanation),
            evidence_urls=[HttpUrl(url) for url in evidence_urls],
            validation_warnings=[
                self._safe_text(warning) for warning in company_warnings
            ],
            retrieved_at=company.updated_at,
        )

    @staticmethod
    def _field_value(company: CompanyRecord, names: tuple[str, ...]) -> str | None:
        supported = [
            field
            for field in company.extracted_fields
            if field.name in names
            and field.evidence
            and isinstance(field.value, str)
            and field.value.strip()
        ]
        if not supported:
            return None
        chosen = max(supported, key=lambda field: field.confidence or 0.0)
        return str(chosen.value).strip()

    def _skipped_item(self, source: SkippedSource) -> SkippedSourceItem:
        return SkippedSourceItem(
            id=source.id,
            domain=urlsplit(str(source.url)).hostname or "",
            url=source.url,
            reason=self._safe_text(source.reason),
            skipped_at=source.skipped_at,
        )

    @staticmethod
    def _secret_present(value: object) -> bool:
        getter = getattr(value, "get_secret_value", None)
        if callable(getter):
            secret = getter()
            return isinstance(secret, str) and bool(secret.strip())
        return False

    @staticmethod
    def _secret_value(value: object) -> str | None:
        getter = getattr(value, "get_secret_value", None)
        if not callable(getter):
            return None
        secret = getter()
        return secret if isinstance(secret, str) and secret else None

    def _safe_text(self, value: str) -> str:
        safe = value
        for secret in self._secret_values:
            safe = safe.replace(secret, "[REDACTED]")
        return safe
