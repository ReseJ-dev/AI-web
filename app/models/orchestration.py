"""Models shared by the end-to-end research orchestration workflow."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.models.domain import (
    CompanyRecord,
    ResearchRun,
    SkippedSource,
    UtcTimestampedModel,
    utc_now,
)
from app.models.entity_resolution import OfficialIdentifier
from app.models.relevance import RelevanceScoreResult


class ResearchProgressStage(StrEnum):
    """Observable workflow stages emitted by the orchestrator."""

    PLANNING = "planning"
    SEARCHING = "searching"
    VALIDATING = "validating"
    CHECKING_COMPLIANCE = "checking_compliance"
    CRAWLING = "crawling"
    EXTRACTING = "extracting"
    ENRICHING = "enriching"
    DEDUPLICATING = "deduplicating"
    SCORING = "scoring"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class ResearchProgressEvent(UtcTimestampedModel):
    """One ordered status update suitable for UI, API, or CLI consumers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    stage: ResearchProgressStage
    message: str = Field(min_length=1, max_length=2_000)
    completed_items: int | None = Field(default=None, ge=0)
    total_items: int | None = Field(default=None, ge=0)
    warning: bool = False
    occurred_at: datetime = Field(default_factory=utc_now)


class CrawledPage(BaseModel):
    """One fetched page retained only for local content processing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    html: str = Field(max_length=5_000_000)
    http_status: int = Field(default=200, ge=100, le=599)


class CrawlResult(BaseModel):
    """Bounded same-domain crawl output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_url: HttpUrl
    canonical_url: HttpUrl
    pages: list[CrawledPage] = Field(min_length=1, max_length=20)
    warnings: list[str] = Field(default_factory=list)


class EnrichmentResult(BaseModel):
    """A provider's evidence-preserving enrichment output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    company: CompanyRecord
    official_identifiers: list[OfficialIdentifier] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OfficialWebsiteAssessment(BaseModel):
    """Deterministic assessment of a transient search candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    likely_official: bool
    website_url: HttpUrl | None = None
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def require_url_for_official_site(self) -> Self:
        """A positive assessment must identify the normalized website root."""
        if self.likely_official and self.website_url is None:
            raise ValueError("official website assessments require a URL")
        return self


class RankedCompanyRecord(BaseModel):
    """A final verified company with its deterministic relevance score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    company: CompanyRecord
    relevance: RelevanceScoreResult


class ExportArtifact(BaseModel):
    """Result produced by an exporter implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=2_048)
    record_count: int = Field(ge=0)


class ExportContext(UtcTimestampedModel):
    """Run-level data required by full-fidelity result exporters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skipped_sources: list[SkippedSource] = Field(default_factory=list)
    generated_queries: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    strict_compliance_mode: bool = True
    warnings: list[str] = Field(default_factory=list)
    completion_time: datetime = Field(default_factory=utc_now)


class ResearchOrchestrationResult(BaseModel):
    """Final workflow outcome including partial records and audit events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: ResearchRun
    final_stage: ResearchProgressStage
    records: list[RankedCompanyRecord] = Field(default_factory=list)
    skipped_sources: list[SkippedSource] = Field(default_factory=list)
    exports: list[ExportArtifact] = Field(default_factory=list)
    events: list[ResearchProgressEvent] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    search_requests_used: int = Field(ge=0)
    candidates_discovered: int = Field(ge=0)

    @model_validator(mode="after")
    def terminal_stage_is_valid(self) -> Self:
        """Only terminal progress stages can finish a workflow."""
        terminal = {
            ResearchProgressStage.COMPLETED,
            ResearchProgressStage.COMPLETED_WITH_WARNINGS,
            ResearchProgressStage.FAILED,
        }
        if self.final_stage not in terminal:
            raise ValueError("orchestration result requires a terminal stage")
        return self
