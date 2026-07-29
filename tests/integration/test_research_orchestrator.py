"""Mocked end-to-end coverage for the complete research workflow."""

from collections.abc import Sequence
from uuid import UUID

import pytest

from app.exporters.interfaces import ResultExporter
from app.models import (
    CompanyRecord,
    CompliancePreflightResult,
    CrawledPage,
    CrawlResult,
    EnrichmentResult,
    ExportArtifact,
    PreflightDecision,
    RankedCompanyRecord,
    ResearchProgressEvent,
    ResearchProgressStage,
    ResearchRequest,
    ResearchRun,
    ResearchRunStatus,
    SearchCandidate,
    SkippedSource,
)
from app.services.research_orchestrator import ResearchOrchestrator
from app.services.source_policy import SourcePolicyDecision, SourcePolicyResult
from app.services.structured_data_extraction import DeterministicCompanyExtractor


class _SearchProvider:
    """Return two deterministic batches without retaining response payloads."""

    def __init__(self) -> None:
        first_domains = ["blocked.example", "broken.example"]
        first_domains.extend(f"company{index:02}.example" for index in range(18))
        second_domains = [f"company{index:02}.example" for index in range(18, 30)]
        self._batches = [first_domains, second_domains]
        self.calls: list[tuple[str, int]] = []

    async def search(
        self,
        query: str,
        *,
        country: str = "US",
        language: str = "en",
        count: int = 10,
        offset: int = 0,
    ) -> list[SearchCandidate]:
        del country, language, count
        self.calls.append((query, offset))
        domains = self._batches[len(self.calls) - 1]
        return [
            SearchCandidate(
                url=f"https://{domain}/",
                title=f"{domain.split('.')[0].title()} | Shopify Agency",
                domain=domain,
                rank=index,
                provider="fake",
            )
            for index, domain in enumerate(domains, start=1)
        ]


class _SourcePolicy:
    def evaluate(self, source: str) -> SourcePolicyResult:
        blocked = "blocked.example" in source
        return SourcePolicyResult(
            decision=(
                SourcePolicyDecision.REJECTED
                if blocked
                else SourcePolicyDecision.APPROVED
            ),
            normalized_domain=source.split("/")[2],
            reason="Blocked fixture domain." if blocked else "Approved fixture domain.",
        )


class _CompliancePreflight:
    async def check(self, target_url: str) -> CompliancePreflightResult:
        return CompliancePreflightResult(
            target_url=target_url,
            normalized_domain=target_url.split("/")[2],
            decision=PreflightDecision.APPROVED,
            domain_reason="Approved by the fixture policy.",
            reason="Domain and robots checks approved the source.",
        )


class _WebsiteCrawler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def crawl(
        self,
        website_url: str,
        *,
        max_pages: int = 5,
    ) -> CrawlResult:
        del max_pages
        self.calls.append(website_url)
        if "broken.example" in website_url:
            raise TimeoutError("fixture crawl timeout")
        host = website_url.split("/")[2]
        number = int(host.removeprefix("company").split(".")[0])
        name = f"Company {number:02}"
        root = f"https://{host}/"
        navigation = """
            <nav>
              <a href="/about">About</a>
              <a href="/services">Services</a>
              <a href="/contact">Contact</a>
            </nav>
        """
        pages = [
            CrawledPage(
                url=root,
                html=self._html(
                    root,
                    name,
                    navigation,
                    "<h1>Shopify Agency</h1><p>Shopify development services.</p>",
                ),
            ),
            CrawledPage(
                url=f"{root}about",
                html=self._html(
                    f"{root}about",
                    name,
                    navigation,
                    "<h1>About us</h1><p>Based in the Netherlands.</p>",
                ),
            ),
            CrawledPage(
                url=f"{root}services",
                html=self._html(
                    f"{root}services",
                    name,
                    navigation,
                    "<h1>Services</h1><p>Shopify development</p>",
                ),
            ),
            CrawledPage(
                url=f"{root}contact",
                html=self._html(
                    f"{root}contact",
                    name,
                    navigation,
                    "<h1>Contact</h1><p>Contact our Shopify agency.</p>",
                ),
            ),
        ]
        return CrawlResult(
            requested_url=root,
            canonical_url=root,
            pages=pages,
        )

    @staticmethod
    def _html(url: str, name: str, navigation: str, body: str) -> str:
        return f"""
        <!doctype html>
        <html>
          <head>
            <title>{name} | Shopify Agency</title>
            <link rel="canonical" href="{url}">
            <meta property="og:site_name" content="{name}">
            <script type="application/ld+json">
              {{
                "@context": "https://schema.org",
                "@type": "Organization",
                "name": "{name}",
                "url": "{url}",
                "address": {{"addressCountry": "Netherlands"}},
                "knowsAbout": ["Shopify development"]
              }}
            </script>
          </head>
          <body>{navigation}<main>{body}</main></body>
        </html>
        """


class _EnrichmentProvider:
    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        self.calls = 0

    async def enrich(self, company: CompanyRecord) -> EnrichmentResult:
        self.calls += 1
        return EnrichmentResult(company=company)


class _RunRepository:
    def __init__(self) -> None:
        self.runs: dict[UUID, ResearchRun] = {}

    def add(self, run: ResearchRun) -> ResearchRun:
        self.runs[run.id] = run
        return run

    def get(self, run_id: UUID) -> ResearchRun | None:
        return self.runs.get(run_id)

    def update(self, run: ResearchRun) -> ResearchRun:
        self.runs[run.id] = run
        return run


class _CompanyRepository:
    def __init__(self) -> None:
        self.companies: list[CompanyRecord] = []

    def add(self, company: CompanyRecord) -> CompanyRecord:
        self.companies.append(company)
        return company

    def get(self, company_id: UUID) -> CompanyRecord | None:
        return next(
            (company for company in self.companies if company.id == company_id),
            None,
        )

    def list_for_run(self, run_id: UUID) -> list[CompanyRecord]:
        return [
            company for company in self.companies if company.research_run_id == run_id
        ]


class _SkippedSourceRepository:
    def __init__(self) -> None:
        self.sources: list[SkippedSource] = []

    def add(self, source: SkippedSource) -> SkippedSource:
        self.sources.append(source)
        return source

    def list_for_run(self, run_id: UUID) -> list[SkippedSource]:
        return [source for source in self.sources if source.research_run_id == run_id]


class _CsvExporter(ResultExporter):
    format_name = "csv"

    def __init__(self) -> None:
        self.record_count = 0

    async def export(
        self,
        run: ResearchRun,
        records: Sequence[RankedCompanyRecord],
    ) -> ExportArtifact:
        self.record_count = len(records)
        return ExportArtifact(
            format_name=self.format_name,
            location=f"memory://{run.id}/results.csv",
            record_count=len(records),
        )


@pytest.mark.anyio
async def test_complete_thirty_result_workflow_is_resilient() -> None:
    search = _SearchProvider()
    crawler = _WebsiteCrawler()
    enrichers = [
        _EnrichmentProvider("opencorporates"),
        _EnrichmentProvider("wikidata"),
        _EnrichmentProvider("geonames"),
    ]
    runs = _RunRepository()
    companies = _CompanyRepository()
    skipped = _SkippedSourceRepository()
    exporter = _CsvExporter()
    progress: list[ResearchProgressEvent] = []
    orchestrator = ResearchOrchestrator(
        search_provider=search,
        source_policy=_SourcePolicy(),
        compliance_preflight=_CompliancePreflight(),
        crawler=crawler,
        structured_extractor=DeterministicCompanyExtractor(),
        run_repository=runs,
        company_repository=companies,
        skipped_source_repository=skipped,
        enrichment_providers=enrichers,
        exporters=[exporter],
        search_budget=2,
        search_page_size=20,
        crawl_page_limit=4,
    )

    result = await orchestrator.run(
        ResearchRequest(
            query="Shopify agencies",
            requested_fields=[
                {"name": "services"},
                {"name": "country"},
            ],
            result_count=30,
        ),
        location="Netherlands",
        country="NL",
        language="en",
        city="Amsterdam",
        country_tld="nl",
        export_formats=["csv"],
        on_progress=progress.append,
    )

    assert result.final_stage is ResearchProgressStage.COMPLETED_WITH_WARNINGS
    assert result.run.status is ResearchRunStatus.COMPLETED
    assert len(result.records) == 30
    assert len(companies.companies) == 30
    assert result.search_requests_used == 2
    assert result.candidates_discovered == 32
    assert len(search.calls) == 2
    assert len(crawler.calls) == 31
    assert {source.url.host for source in result.skipped_sources} == {
        "blocked.example",
        "broken.example",
    }
    assert len(skipped.sources) == 2
    assert all(enricher.calls == 30 for enricher in enrichers)
    assert exporter.record_count == 30
    assert result.exports[0].record_count == 30
    assert [record.relevance.total_score for record in result.records] == sorted(
        (record.relevance.total_score for record in result.records),
        reverse=True,
    )
    stages = {event.stage for event in result.events}
    assert {
        ResearchProgressStage.PLANNING,
        ResearchProgressStage.SEARCHING,
        ResearchProgressStage.VALIDATING,
        ResearchProgressStage.CHECKING_COMPLIANCE,
        ResearchProgressStage.CRAWLING,
        ResearchProgressStage.EXTRACTING,
        ResearchProgressStage.DEDUPLICATING,
        ResearchProgressStage.SCORING,
        ResearchProgressStage.EXPORTING,
        ResearchProgressStage.COMPLETED_WITH_WARNINGS,
    } <= stages
    assert progress == result.events
    assert any("fixture crawl timeout" in warning for warning in result.warnings)


@pytest.mark.anyio
async def test_budget_exhaustion_returns_partial_success() -> None:
    search = _SearchProvider()
    orchestrator = ResearchOrchestrator(
        search_provider=search,
        source_policy=_SourcePolicy(),
        compliance_preflight=_CompliancePreflight(),
        crawler=_WebsiteCrawler(),
        structured_extractor=DeterministicCompanyExtractor(),
        run_repository=_RunRepository(),
        company_repository=_CompanyRepository(),
        skipped_source_repository=_SkippedSourceRepository(),
        search_budget=1,
        search_page_size=20,
        crawl_page_limit=4,
    )

    result = await orchestrator.run(
        {
            "query": "Shopify agencies",
            "requested_fields": [{"name": "services"}, {"name": "country"}],
            "result_count": 30,
        },
        location="Netherlands",
    )

    assert result.final_stage is ResearchProgressStage.COMPLETED_WITH_WARNINGS
    assert result.run.status is ResearchRunStatus.COMPLETED
    assert len(result.records) == 18
    assert any("budget was exhausted" in warning for warning in result.warnings)
