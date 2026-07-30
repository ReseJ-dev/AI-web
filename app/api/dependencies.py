"""Production dependency composition for the research API."""

from collections.abc import Callable

from app.core.settings import Settings, get_settings
from app.exporters import GoogleSheetsExporter, ResultExporter
from app.providers import (
    AsyncWebsiteCrawler,
    BraveSearchProvider,
    CompanyEnrichmentProvider,
    GeoNamesProvider,
    OpenCorporatesProvider,
    WikidataProvider,
    resolve_llm_provider,
)
from app.repositories import (
    SqlAlchemyCompanyRecordRepository,
    SqlAlchemyResearchRunRepository,
    SqlAlchemySkippedSourceRepository,
)
from app.services import (
    CompliancePreflightService,
    CompositeCompanyExtractor,
    DeterministicCompanyExtractor,
    LLMCompanyExtractor,
    ResearchOrchestrator,
    ResearchRunApplicationService,
    SourcePolicyService,
    StructuredDataExtractor,
)


def _secret_present(value: object) -> bool:
    getter = getattr(value, "get_secret_value", None)
    if not callable(getter):
        return False
    secret = getter()
    return isinstance(secret, str) and bool(secret.strip())


def _structured_extractor(settings: Settings) -> StructuredDataExtractor:
    deterministic = DeterministicCompanyExtractor()
    if settings.llm_provider.casefold() == "disabled":
        return deterministic
    llm = LLMCompanyExtractor(
        resolve_llm_provider(),
        model=settings.llm_model,
    )
    return CompositeCompanyExtractor(deterministic, llm)


def _enrichment_providers(settings: Settings) -> list[CompanyEnrichmentProvider]:
    providers: list[CompanyEnrichmentProvider] = []
    if (
        _secret_present(settings.opencorporates_api_key)
        and settings.opencorporates_licensed_data_use_allowed
    ):
        providers.append(OpenCorporatesProvider())
    if settings.wikidata_enabled:
        providers.append(WikidataProvider())
    if _secret_present(settings.geonames_username):
        providers.append(GeoNamesProvider())
    return providers


def _google_exporter_factory(
    settings: Settings,
) -> Callable[[str | None], ResultExporter] | None:
    credentials = bool(settings.google_service_account_file) or _secret_present(
        settings.google_service_account_json
    )
    if not credentials:
        return None

    def build(spreadsheet_id: str | None) -> ResultExporter:
        return GoogleSheetsExporter(spreadsheet_id=spreadsheet_id)

    return build


def build_research_service() -> ResearchRunApplicationService:
    """Build configured providers while keeping missing optional providers disabled."""
    settings = get_settings()
    run_repository = SqlAlchemyResearchRunRepository()
    company_repository = SqlAlchemyCompanyRecordRepository()
    skipped_repository = SqlAlchemySkippedSourceRepository()
    workflow: ResearchOrchestrator | None = None
    if _secret_present(settings.brave_search_api_key):
        source_policy = SourcePolicyService()
        compliance = CompliancePreflightService(domain_policy=source_policy)
        workflow = ResearchOrchestrator(
            search_provider=BraveSearchProvider(),
            source_policy=source_policy,
            compliance_preflight=compliance,
            crawler=AsyncWebsiteCrawler(compliance_preflight=compliance),
            structured_extractor=_structured_extractor(settings),
            enrichment_providers=_enrichment_providers(settings),
            run_repository=run_repository,
            company_repository=company_repository,
            skipped_source_repository=skipped_repository,
        )
    return ResearchRunApplicationService(
        workflow=workflow,
        google_sheets_exporter_factory=_google_exporter_factory(settings),
        run_repository=run_repository,
        company_repository=company_repository,
        skipped_source_repository=skipped_repository,
        settings=settings,
    )
