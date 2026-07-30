"""Resilient end-to-end orchestration for evidence-based company research."""

import inspect
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import JsonValue

from app.core.settings import get_settings
from app.exporters.interfaces import ResultExporter
from app.models import (
    CompanyEntity,
    CompanyExtraction,
    CompanyRecord,
    CompliancePreflightResult,
    CrawlResult,
    EntityResolutionOutcome,
    Evidence,
    ExportArtifact,
    ExportContext,
    ExtractedField,
    ExtractedPageContent,
    ExtractionStatus,
    FactBasis,
    OfficialIdentifier,
    OfficialWebsiteAssessment,
    PageCandidate,
    PreflightDecision,
    RankedCompanyRecord,
    ResearchOrchestrationResult,
    ResearchProgressEvent,
    ResearchProgressStage,
    ResearchRequest,
    ResearchRun,
    ResearchRunStatus,
    SearchCandidate,
    SkippedSource,
    SupportedField,
)
from app.models.domain import utc_now
from app.providers.crawling import WebsiteCrawler
from app.providers.enrichment import CompanyEnrichmentProvider
from app.providers.search import SearchProvider
from app.repositories.interfaces import (
    CompanyRecordRepository,
    ResearchRunRepository,
    SkippedSourceRepository,
)
from app.services.company_deduplication import (
    CompanyDeduplicationService,
    normalize_company_url,
)
from app.services.html_content_extractor import HtmlContentExtractor
from app.services.official_website import OfficialWebsiteIdentificationService
from app.services.page_selection import PageSelectionService
from app.services.query_planner import QueryPlanner
from app.services.relevance_scoring import RelevanceScoringService
from app.services.source_policy import SourcePolicyDecision, SourcePolicyResult
from app.services.structured_data_extraction import StructuredDataExtractor

ProgressCallback = Callable[
    [ResearchProgressEvent],
    Awaitable[None] | None,
]


class SourcePolicyEvaluator(Protocol):
    """Minimal source-policy behavior required by orchestration."""

    def evaluate(self, source: str) -> SourcePolicyResult:
        """Return a cached domain-policy decision."""
        ...


class CompliancePreflightChecker(Protocol):
    """Minimal asynchronous compliance-preflight behavior."""

    async def check(self, target_url: str) -> CompliancePreflightResult:
        """Return the combined domain, robots, and terms decision."""
        ...


class OfficialWebsiteIdentifier(Protocol):
    """Identify likely official roots from transient candidates."""

    def assess(self, candidate: SearchCandidate) -> OfficialWebsiteAssessment:
        """Return a deterministic official-site assessment."""
        ...


class _DraftCompany:
    """Internal independently verified record awaiting deduplication."""

    __slots__ = ("entity", "extraction")

    def __init__(
        self,
        entity: CompanyEntity,
        extraction: CompanyExtraction,
    ) -> None:
        self.entity = entity
        self.extraction = extraction


class ResearchOrchestrator:
    """Run the complete workflow while isolating per-domain failures."""

    def __init__(
        self,
        *,
        search_provider: SearchProvider,
        source_policy: SourcePolicyEvaluator,
        compliance_preflight: CompliancePreflightChecker,
        crawler: WebsiteCrawler,
        structured_extractor: StructuredDataExtractor,
        run_repository: ResearchRunRepository,
        company_repository: CompanyRecordRepository,
        skipped_source_repository: SkippedSourceRepository,
        query_planner: QueryPlanner | None = None,
        official_website_identifier: OfficialWebsiteIdentifier | None = None,
        page_selector: PageSelectionService | None = None,
        content_extractor: HtmlContentExtractor | None = None,
        enrichment_providers: Sequence[CompanyEnrichmentProvider] = (),
        deduplication_service: CompanyDeduplicationService | None = None,
        relevance_scoring_service: RelevanceScoringService | None = None,
        exporters: Sequence[ResultExporter] = (),
        search_budget: int | None = None,
        search_page_size: int | None = None,
        crawl_page_limit: int | None = None,
        search_result_retention_allowed: bool | None = None,
    ) -> None:
        settings = get_settings()
        self._search_provider = search_provider
        self._source_policy = source_policy
        self._compliance_preflight = compliance_preflight
        self._crawler = crawler
        self._structured_extractor = structured_extractor
        self._run_repository = run_repository
        self._company_repository = company_repository
        self._skipped_source_repository = skipped_source_repository
        self._query_planner = query_planner or QueryPlanner()
        self._official_identifier = (
            official_website_identifier or OfficialWebsiteIdentificationService()
        )
        self._page_selector = page_selector or PageSelectionService()
        self._content_extractor = content_extractor or HtmlContentExtractor()
        self._enrichment_providers = tuple(enrichment_providers)
        self._deduplicator = deduplication_service or CompanyDeduplicationService()
        self._scorer = relevance_scoring_service or RelevanceScoringService()
        self._exporters = {
            exporter.format_name.casefold(): exporter for exporter in exporters
        }
        self._search_budget = (
            search_budget
            if search_budget is not None
            else settings.research_search_budget
        )
        self._search_page_size = (
            search_page_size
            if search_page_size is not None
            else settings.research_search_page_size
        )
        self._crawl_page_limit = (
            crawl_page_limit
            if crawl_page_limit is not None
            else settings.research_crawl_page_limit
        )
        self._strict_compliance_mode = settings.robots_strict_mode
        self._retain_search_results = (
            search_result_retention_allowed
            if search_result_retention_allowed is not None
            else settings.search_result_retention_allowed
        )
        if not 1 <= self._search_budget <= 50:
            raise ValueError("search_budget must be between 1 and 50")
        if not 1 <= self._search_page_size <= 20:
            raise ValueError("search_page_size must be between 1 and 20")
        if not 1 <= self._crawl_page_limit <= 20:
            raise ValueError("crawl_page_limit must be between 1 and 20")

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
        """Execute the workflow and return final or partially successful records."""
        validated_request = ResearchRequest.model_validate(request)
        run = ResearchRun(
            id=run_id or uuid4(),
            request=validated_request,
            status=ResearchRunStatus.RUNNING,
        )
        events: list[ResearchProgressEvent] = []
        warnings: list[str] = []
        skipped: list[SkippedSource] = []
        artifacts: list[ExportArtifact] = []
        ranked_records: list[RankedCompanyRecord] = []
        search_requests_used = 0
        candidates_discovered = 0
        seen_websites: set[str] = set()
        drafts: list[_DraftCompany] = []

        await self._emit(
            events,
            warnings,
            ResearchProgressStage.PLANNING,
            "Validating the request and planning discovery queries.",
            on_progress,
        )
        try:
            self._run_repository.add(run)
            queries = self._query_planner.plan(
                validated_request.query,
                location=location,
                city=city,
                country_tld=country_tld,
            )
            await self._emit(
                events,
                warnings,
                ResearchProgressStage.SEARCHING,
                f"Searching with {len(queries)} planned query variations.",
                on_progress,
            )

            for budget_index in range(self._search_budget):
                query = queries[budget_index % len(queries)]
                offset = budget_index // len(queries)
                if offset > 9:
                    warnings.append(
                        "Search pagination limit was reached before the budget."
                    )
                    break
                try:
                    candidates = await self._search_provider.search(
                        query,
                        country=country,
                        language=language,
                        count=self._search_page_size,
                        offset=offset,
                    )
                    search_requests_used += 1
                    candidates_discovered += len(candidates)
                except Exception as error:
                    search_requests_used += 1
                    warnings.append(
                        f"Search request failed ({self._error_kind(error)})."
                    )
                    continue

                await self._emit(
                    events,
                    warnings,
                    ResearchProgressStage.VALIDATING,
                    f"Validating {len(candidates)} transient candidates.",
                    on_progress,
                    completed_items=len(drafts),
                    total_items=validated_request.result_count,
                )
                for candidate in candidates:
                    draft = await self._process_candidate(
                        run,
                        candidate,
                        validated_request,
                        seen_websites,
                        skipped,
                        warnings,
                        events,
                        on_progress,
                    )
                    if draft is not None:
                        drafts.append(draft)

                current, _ = self._deduplicate(drafts)
                if len(current) >= validated_request.result_count:
                    break

            unique_drafts, manual_reviews = self._deduplicate(drafts)
            await self._emit(
                events,
                warnings,
                ResearchProgressStage.DEDUPLICATING,
                f"Resolved {len(drafts)} verified drafts into "
                f"{len(unique_drafts)} companies.",
                on_progress,
            )
            if manual_reviews:
                warnings.append(
                    f"{manual_reviews} possible duplicate pair(s) require "
                    "manual review."
                )
            if len(unique_drafts) < validated_request.result_count:
                warnings.append(
                    "Search budget was exhausted before the requested result count "
                    f"was reached ({len(unique_drafts)}/"
                    f"{validated_request.result_count})."
                )

            await self._emit(
                events,
                warnings,
                ResearchProgressStage.SCORING,
                f"Scoring {len(unique_drafts)} independently verified companies.",
                on_progress,
            )
            scored: list[RankedCompanyRecord] = []
            for draft in unique_drafts:
                try:
                    relevance = self._scorer.score(
                        draft.entity.record,
                        draft.extraction,
                        topic=validated_request.query,
                        location=location,
                        requested_fields=validated_request.requested_fields,
                    )
                    scored.append(
                        RankedCompanyRecord(
                            company=draft.entity.record,
                            relevance=relevance,
                        )
                    )
                except Exception as error:
                    warnings.append(
                        f"Scoring failed for {draft.entity.record.name!r} "
                        f"({self._error_kind(error)})."
                    )

            scored.sort(
                key=lambda item: (
                    -item.relevance.total_score,
                    item.company.name.casefold(),
                    (
                        str(item.company.website_url)
                        if item.company.website_url is not None
                        else ""
                    ),
                )
            )
            ranked_records = scored[: validated_request.result_count]
            persisted_records: list[RankedCompanyRecord] = []
            for ranked in ranked_records:
                try:
                    stored = self._company_repository.add(ranked.company)
                    persisted_records.append(
                        ranked.model_copy(update={"company": stored})
                    )
                except Exception as error:
                    warnings.append(
                        f"Could not persist {ranked.company.name!r} "
                        f"({self._error_kind(error)})."
                    )
                    persisted_records.append(ranked)
            ranked_records = persisted_records

            requested_exports = tuple(
                dict.fromkeys(
                    item.strip().casefold() for item in export_formats if item.strip()
                )
            )
            if requested_exports:
                await self._emit(
                    events,
                    warnings,
                    ResearchProgressStage.EXPORTING,
                    f"Exporting results in {len(requested_exports)} format(s).",
                    on_progress,
                )
            for format_name in requested_exports:
                exporter = self._exporters.get(format_name)
                if exporter is None:
                    warnings.append(
                        f"No exporter is configured for format {format_name!r}."
                    )
                    continue
                try:
                    artifacts.append(
                        await exporter.export(
                            run,
                            ranked_records,
                            context=ExportContext(
                                skipped_sources=skipped,
                                generated_queries=queries,
                                providers=self._configured_provider_names(),
                                strict_compliance_mode=self._strict_compliance_mode,
                                warnings=warnings,
                                completion_time=utc_now(),
                            ),
                        )
                    )
                except Exception as error:
                    warnings.append(
                        f"Exporter {format_name!r} failed ({self._error_kind(error)})."
                    )

            terminal = (
                ResearchProgressStage.COMPLETED_WITH_WARNINGS
                if warnings
                else ResearchProgressStage.COMPLETED
            )
            run = run.model_copy(
                update={
                    "status": ResearchRunStatus.COMPLETED,
                    "updated_at": utc_now(),
                }
            )
            try:
                self._run_repository.update(run)
            except Exception as error:
                warnings.append(
                    f"Could not persist final run status ({self._error_kind(error)})."
                )
                terminal = ResearchProgressStage.COMPLETED_WITH_WARNINGS
            await self._emit(
                events,
                warnings,
                terminal,
                (
                    f"Research completed with {len(ranked_records)} final records"
                    + (" and warnings." if warnings else ".")
                ),
                on_progress,
                completed_items=len(ranked_records),
                total_items=validated_request.result_count,
            )
            return ResearchOrchestrationResult(
                run=run,
                final_stage=terminal,
                records=ranked_records,
                skipped_sources=skipped,
                exports=artifacts,
                events=events,
                warnings=warnings,
                search_requests_used=search_requests_used,
                candidates_discovered=candidates_discovered,
            )
        except Exception as error:
            error_kind = self._error_kind(error)
            warnings.append(f"Research workflow failed ({error_kind}).")
            run = run.model_copy(
                update={
                    "status": ResearchRunStatus.FAILED,
                    "error_message": "The research workflow failed unexpectedly.",
                    "updated_at": utc_now(),
                }
            )
            try:
                self._run_repository.update(run)
            except Exception as persistence_error:
                warnings.append(
                    "Could not persist failed run status "
                    f"({self._error_kind(persistence_error)})."
                )
            await self._emit(
                events,
                warnings,
                ResearchProgressStage.FAILED,
                f"Research failed after partial processing ({error_kind}).",
                on_progress,
                completed_items=len(ranked_records),
                total_items=validated_request.result_count,
            )
            return ResearchOrchestrationResult(
                run=run,
                final_stage=ResearchProgressStage.FAILED,
                records=ranked_records,
                skipped_sources=skipped,
                exports=artifacts,
                events=events,
                warnings=warnings,
                search_requests_used=search_requests_used,
                candidates_discovered=candidates_discovered,
            )

    async def aclose(self) -> None:
        """Close owned or injected provider resources once each."""
        seen: set[int] = set()
        resources = (
            self._search_provider,
            self._compliance_preflight,
            self._crawler,
            self._structured_extractor,
            *self._enrichment_providers,
            *self._exporters.values(),
        )
        for resource in resources:
            if id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "aclose", None)
            if callable(close):
                outcome = close()
                if inspect.isawaitable(outcome):
                    await outcome

    async def _process_candidate(
        self,
        run: ResearchRun,
        candidate: SearchCandidate,
        request: ResearchRequest,
        seen_websites: set[str],
        skipped: list[SkippedSource],
        warnings: list[str],
        events: list[ResearchProgressEvent],
        on_progress: ProgressCallback | None,
    ) -> _DraftCompany | None:
        """Validate and extract one candidate without propagating domain failures."""
        original_url = str(candidate.url)
        try:
            normalized_candidate = normalize_company_url(original_url)
        except ValueError as error:
            await self._skip(
                run,
                original_url,
                f"Candidate URL normalization failed ({self._error_kind(error)}).",
                skipped,
                warnings,
            )
            return None

        policy = self._source_policy.evaluate(normalized_candidate)
        if policy.decision is SourcePolicyDecision.REJECTED:
            await self._skip(
                run,
                original_url,
                f"Blocked by source policy: {policy.reason}",
                skipped,
                warnings,
            )
            return None

        assessment = self._official_identifier.assess(candidate)
        if not assessment.likely_official or assessment.website_url is None:
            await self._skip(
                run,
                original_url,
                f"Not identified as an official company website: {assessment.reason}",
                skipped,
                warnings,
            )
            return None
        website_url = normalize_company_url(str(assessment.website_url))
        if website_url in seen_websites:
            return None
        seen_websites.add(website_url)

        await self._emit(
            events,
            warnings,
            ResearchProgressStage.CHECKING_COMPLIANCE,
            f"Checking compliance for {website_url}.",
            on_progress,
        )
        try:
            preflight = await self._compliance_preflight.check(website_url)
        except Exception as error:
            await self._skip(
                run,
                website_url,
                f"Compliance preflight failed ({self._error_kind(error)}).",
                skipped,
                warnings,
            )
            return None
        if preflight.decision is not PreflightDecision.APPROVED:
            await self._skip(
                run,
                website_url,
                f"Compliance preflight did not approve the source: {preflight.reason}",
                skipped,
                warnings,
            )
            return None

        await self._emit(
            events,
            warnings,
            ResearchProgressStage.CRAWLING,
            f"Crawling approved website {website_url}.",
            on_progress,
        )
        try:
            crawl = await self._crawler.crawl(
                website_url,
                max_pages=self._crawl_page_limit,
            )
        except Exception as error:
            await self._skip(
                run,
                website_url,
                f"Website crawl failed ({self._error_kind(error)}).",
                skipped,
                warnings,
            )
            return None
        warnings.extend(f"{website_url}: {warning}" for warning in crawl.warnings)

        await self._emit(
            events,
            warnings,
            ResearchProgressStage.EXTRACTING,
            f"Selecting pages and extracting structured fields from {website_url}.",
            on_progress,
        )
        try:
            pages = self._select_clean_pages(
                crawl,
                relevant_terms=request.query.split(),
            )
            extraction = await self._structured_extractor.extract(
                pages,
                request.requested_fields,
            )
        except Exception as error:
            await self._skip(
                run,
                website_url,
                f"Content or structured extraction failed ({self._error_kind(error)}).",
                skipped,
                warnings,
            )
            return None
        if extraction.status is not ExtractionStatus.ACCEPTED:
            await self._skip(
                run,
                website_url,
                "Structured extraction was rejected: "
                + "; ".join(extraction.rejection_reasons),
                skipped,
                warnings,
            )
            return None
        if not self._independently_verified(extraction):
            await self._skip(
                run,
                website_url,
                "Final verification requires explicit cited company-name and "
                "official-website fields.",
                skipped,
                warnings,
            )
            return None

        company = self._company_from_extraction(run, extraction)
        identifiers: list[OfficialIdentifier] = []
        if self._enrichment_providers:
            await self._emit(
                events,
                warnings,
                ResearchProgressStage.ENRICHING,
                f"Enriching {company.name!r} through "
                f"{len(self._enrichment_providers)} configured provider(s).",
                on_progress,
                completed_items=0,
                total_items=len(self._enrichment_providers),
            )
        for provider in self._enrichment_providers:
            try:
                enriched = await provider.enrich(company)
                if (
                    enriched.company.name != company.name
                    or self._normalized_optional_url(enriched.company.website_url)
                    != self._normalized_optional_url(company.website_url)
                ):
                    warnings.append(
                        f"{provider.provider_name} enrichment for {website_url} "
                        "was ignored because it changed the independently "
                        "verified company identity."
                    )
                    continue
                if any(
                    not field.evidence for field in enriched.company.extracted_fields
                ):
                    warnings.append(
                        f"{provider.provider_name} enrichment for {website_url} "
                        "was ignored because an extracted field lacked evidence."
                    )
                    continue
                company = enriched.company.model_copy(
                    update={"research_run_id": run.id}
                )
                identifiers.extend(enriched.official_identifiers)
                warnings.extend(
                    f"{provider.provider_name} for {website_url}: {warning}"
                    for warning in enriched.warnings
                )
            except Exception as error:
                warnings.append(
                    f"{provider.provider_name} enrichment failed for "
                    f"{website_url} ({self._error_kind(error)})."
                )

        return _DraftCompany(
            CompanyEntity(
                record=company,
                canonical_url=crawl.canonical_url,
                official_identifiers=self._unique_identifiers(identifiers),
            ),
            extraction,
        )

    @staticmethod
    def _provider_name(provider: object) -> str:
        """Return a stable configured-provider name for export metadata."""
        configured = getattr(provider, "provider_name", None)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return type(provider).__name__

    def _configured_provider_names(self) -> list[str]:
        """List discovery, crawl, extraction, and optional enrichment providers."""
        return list(
            dict.fromkeys(
                self._provider_name(provider)
                for provider in (
                    self._search_provider,
                    self._crawler,
                    self._structured_extractor,
                    *self._enrichment_providers,
                )
            )
        )

    @staticmethod
    def _normalized_optional_url(url: object) -> str | None:
        """Normalize an optional validated URL for identity comparisons."""
        if url is None:
            return None
        return normalize_company_url(str(url))

    def _select_clean_pages(
        self,
        crawl: CrawlResult,
        *,
        relevant_terms: Sequence[str],
    ) -> list[ExtractedPageContent]:
        """Clean crawled HTML, rank page candidates, and retain available pages."""
        cleaned: list[ExtractedPageContent] = []
        for page in crawl.pages:
            if 200 <= page.http_status < 300:
                cleaned.append(
                    self._content_extractor.extract(str(page.url), page.html)
                )
        if not cleaned:
            raise ValueError("crawl contained no successful HTML pages")

        candidates: list[PageCandidate] = [
            PageCandidate(
                url=page.canonical_url,
                title=page.title or "",
                headings=page.headings,
            )
            for page in cleaned
        ]
        candidates.extend(
            PageCandidate(url=link.url, anchor_text=link.text)
            for page in cleaned
            for link in page.navigation_links
        )
        ranked = self._page_selector.select(
            str(crawl.canonical_url),
            candidates,
            relevant_terms=relevant_terms,
            limit=self._crawl_page_limit,
        )
        by_url = {
            normalize_company_url(str(page.canonical_url)): page for page in cleaned
        }
        selected = [
            by_url[normalized]
            for item in ranked
            if (normalized := normalize_company_url(str(item.url))) in by_url
        ]
        return selected or [cleaned[0]]

    @staticmethod
    def _independently_verified(extraction: CompanyExtraction) -> bool:
        """Require explicit citations for core identity and official website."""
        for name in ("company_name", "website_url"):
            field = extraction.field(name)
            if (
                field is None
                or field.value is None
                or field.basis is not FactBasis.EXPLICIT
                or not field.evidence_urls
            ):
                return False
        return True

    @staticmethod
    def _company_from_extraction(
        run: ResearchRun,
        extraction: CompanyExtraction,
    ) -> CompanyRecord:
        """Convert verified supported fields into the persistence domain model."""
        name_field = extraction.field("company_name")
        website_field = extraction.field("website_url")
        if (
            name_field is None
            or not isinstance(name_field.value, str)
            or website_field is None
            or not isinstance(website_field.value, str)
        ):
            raise ValueError("verified extraction lacks core string values")

        description_field = extraction.field("summary") or extraction.field(
            "description"
        )
        description = (
            description_field.value
            if description_field is not None
            and isinstance(description_field.value, str)
            else None
        )
        services_field = extraction.field("services")
        services = ResearchOrchestrator._string_values(
            services_field.value if services_field is not None else None
        )
        extracted_fields = [
            ResearchOrchestrator._to_extracted_field(field)
            for field in extraction.fields
            if field.value is not None and field.evidence_urls
        ]
        return CompanyRecord(
            research_run_id=run.id,
            name=name_field.value,
            website_url=website_field.value,
            description=description,
            services=services,
            extracted_fields=extracted_fields,
        )

    @staticmethod
    def _string_values(value: JsonValue | None) -> list[str]:
        """Normalize a string or string list for CompanyRecord services."""
        candidates = value if isinstance(value, list) else [value]
        return [
            item.strip()
            for item in candidates
            if isinstance(item, str) and item.strip()
        ]

    @staticmethod
    def _to_extracted_field(field: SupportedField) -> ExtractedField:
        """Convert supported-field citations into persistence evidence."""
        value_text = json.dumps(field.value, ensure_ascii=False)
        excerpt = field.evidence_fragment or (
            f"Supported {field.name}: {value_text[:450]}"
            if value_text
            else f"Supported {field.name}."
        )
        return ExtractedField(
            name=field.name,
            value=field.value,
            confidence=(
                field.confidence
                if field.confidence is not None
                else (0.95 if field.basis is FactBasis.EXPLICIT else 0.65)
            ),
            evidence=[
                Evidence(
                    urls=field.evidence_urls,
                    excerpt=excerpt,
                )
            ],
        )

    def _deduplicate(
        self,
        drafts: Sequence[_DraftCompany],
    ) -> tuple[list[_DraftCompany], int]:
        """Greedily merge authoritative matches and retain ambiguous names."""
        unique: list[_DraftCompany] = []
        manual_reviews = 0
        for draft in drafts:
            merged = False
            for index, existing in enumerate(unique):
                decision = self._deduplicator.resolve(
                    existing.entity,
                    draft.entity,
                )
                outcome = decision.outcome
                if outcome is EntityResolutionOutcome.MERGE:
                    merged_record = decision.merged_company
                    merge_metadata = decision.merge_metadata
                    if merged_record is None or merge_metadata is None:
                        raise RuntimeError(
                            "merge outcome did not contain complete merge audit data"
                        )
                    existing_history = merged_record.metadata.get(
                        "entity_resolution", []
                    )
                    history = (
                        list(existing_history)
                        if isinstance(existing_history, list)
                        else []
                    )
                    history.append(merge_metadata.model_dump(mode="json"))
                    merged_record = merged_record.model_copy(
                        update={
                            "metadata": {
                                **merged_record.metadata,
                                "entity_resolution": history,
                            }
                        }
                    )
                    unique[index] = _DraftCompany(
                        CompanyEntity(
                            record=merged_record,
                            canonical_url=(
                                existing.entity.canonical_url
                                or draft.entity.canonical_url
                            ),
                            official_identifiers=self._unique_identifiers(
                                [
                                    *existing.entity.official_identifiers,
                                    *draft.entity.official_identifiers,
                                ]
                            ),
                        ),
                        self._merge_extractions(
                            existing.extraction,
                            draft.extraction,
                        ),
                    )
                    merged = True
                    break
                if outcome is EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED:
                    manual_reviews += 1
            if not merged:
                unique.append(draft)
        return unique, manual_reviews

    @staticmethod
    def _merge_extractions(
        left: CompanyExtraction,
        right: CompanyExtraction,
    ) -> CompanyExtraction:
        """Merge supported facts for relevance scoring without duplicate names."""
        grouped: dict[str, list[SupportedField]] = defaultdict(list)
        for field in (*left.fields, *right.fields):
            grouped[field.name].append(field)
        selected: list[SupportedField] = []
        for name in sorted(grouped):
            candidates = grouped[name]
            non_null = [field for field in candidates if field.value is not None]
            if not non_null:
                selected.append(SupportedField(name=name, value=None))
                continue
            chosen = max(
                enumerate(non_null),
                key=lambda item: (
                    1 if item[1].basis is FactBasis.EXPLICIT else 0,
                    len(item[1].evidence_urls),
                    -item[0],
                ),
            )[1]
            same_value = [
                field
                for field in non_null
                if ResearchOrchestrator._json_key(field.value)
                == ResearchOrchestrator._json_key(chosen.value)
            ]
            evidence_urls = list(
                dict.fromkeys(
                    url for field in same_value for url in field.evidence_urls
                )
            )
            selected.append(chosen.model_copy(update={"evidence_urls": evidence_urls}))
        return CompanyExtraction(
            status=ExtractionStatus.ACCEPTED,
            fields=selected,
        )

    @staticmethod
    def _json_key(value: JsonValue) -> str:
        """Return a stable structured-value comparison key."""
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _unique_identifiers(
        identifiers: Sequence[OfficialIdentifier],
    ) -> list[OfficialIdentifier]:
        """Deduplicate normalized provider identifiers without reordering."""
        return list(
            {
                (identifier.source, identifier.value): identifier
                for identifier in identifiers
            }.values()
        )

    async def _skip(
        self,
        run: ResearchRun,
        url: str,
        reason: str,
        skipped: list[SkippedSource],
        warnings: list[str],
    ) -> None:
        """Create and persist one skipped-source report without failing the run."""
        if any(str(item.url) == url for item in skipped):
            return
        report = SkippedSource(
            research_run_id=run.id,
            url=url,
            reason=reason[:1_000],
        )
        skipped.append(report)
        warnings.append(f"Skipped {url}: {reason}")
        if self._retain_search_results:
            try:
                self._skipped_source_repository.add(report)
            except Exception as error:
                warnings.append(
                    f"Could not persist skipped source ({self._error_kind(error)})."
                )

    @staticmethod
    async def _emit(
        events: list[ResearchProgressEvent],
        warnings: list[str],
        stage: ResearchProgressStage,
        message: str,
        callback: ProgressCallback | None,
        *,
        completed_items: int | None = None,
        total_items: int | None = None,
    ) -> None:
        """Store and publish one ordered progress event."""
        event = ResearchProgressEvent(
            sequence=len(events) + 1,
            stage=stage,
            message=message,
            completed_items=completed_items,
            total_items=total_items,
            warning=stage
            in {
                ResearchProgressStage.COMPLETED_WITH_WARNINGS,
                ResearchProgressStage.FAILED,
            },
        )
        events.append(event)
        if callback is None:
            return
        try:
            callback_result = callback(event)
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception as error:
            warnings.append(
                f"Progress callback failed ({ResearchOrchestrator._error_kind(error)})."
            )

    @staticmethod
    def _error_kind(error: BaseException) -> str:
        """Return a non-secret diagnostic category instead of exception text."""
        return type(error).__name__
