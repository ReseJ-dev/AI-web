"""Core research domain models."""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    field_validator,
    model_validator,
)

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def normalize_field_name(value: str) -> str:
    """Normalize a human-entered field name to lower snake case."""
    normalized = _NON_ALPHANUMERIC.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise ValueError("field name must contain at least one letter or number")
    return normalized


class DomainModel(BaseModel):
    """Base configuration shared by domain models."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class UtcTimestampedModel(DomainModel):
    """Base model that guarantees UTC-aware timestamps."""

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetimes(cls, value: object) -> object:
        """Reject naive datetimes and normalize aware datetimes to UTC."""
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include timezone information")
        return value.astimezone(UTC)


class RequestedField(DomainModel):
    """A normalized field requested by the user."""

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        """Normalize field names before length validation."""
        if not isinstance(value, str):
            return value
        return normalize_field_name(value)

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        """Trim optional descriptions and convert blank descriptions to null."""
        if not isinstance(value, str):
            return value
        return value.strip() or None


class ResearchRequest(DomainModel):
    """Input needed to start a research run."""

    query: str = Field(min_length=1, max_length=500)
    requested_fields: list[RequestedField] = Field(min_length=1)
    result_count: int = Field(default=10, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """Trim and reject a blank research query."""
        query = value.strip()
        if not query:
            raise ValueError("query must not be blank")
        return query

    @model_validator(mode="after")
    def ensure_unique_fields(self) -> Self:
        """Require normalized requested field names to be unique."""
        names = [field.name for field in self.requested_fields]
        if len(names) != len(set(names)):
            raise ValueError("requested field names must be unique")
        return self


class ResearchRunStatus(StrEnum):
    """Lifecycle status of a research run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResearchRun(UtcTimestampedModel):
    """A single execution of a research request."""

    id: UUID = Field(default_factory=uuid4)
    request: ResearchRequest
    status: ResearchRunStatus = ResearchRunStatus.PENDING
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        """Ensure update time does not precede creation time."""
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self


class Evidence(DomainModel):
    """Supporting source material for an extracted field."""

    urls: list[HttpUrl] = Field(default_factory=list)
    excerpt: str = Field(min_length=1, max_length=5_000)
    source_title: str | None = Field(default=None, max_length=500)


class ExtractedField(DomainModel):
    """A structured value extracted for a requested field."""

    name: str = Field(min_length=1, max_length=100)
    value: JsonValue
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        """Use the same canonical name as requested fields."""
        if not isinstance(value, str):
            return value
        return normalize_field_name(value)


class CompanyRecord(UtcTimestampedModel):
    """Research data collected for one company."""

    id: UUID = Field(default_factory=uuid4)
    research_run_id: UUID
    name: str = Field(min_length=1, max_length=300)
    website_url: HttpUrl | None = None
    description: str | None = None
    services: list[str] = Field(default_factory=list)
    extracted_fields: list[ExtractedField] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ComplianceStatus(StrEnum):
    """Result of evaluating a company against research requirements."""

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class RelevanceScoreBreakdown(DomainModel):
    """Individual inputs and total for a company's relevance score."""

    query_match: float = Field(ge=0, le=1)
    service_match: float = Field(ge=0, le=1)
    evidence_quality: float = Field(ge=0, le=1)
    total: float = Field(ge=0, le=1)


class ComplianceDecision(UtcTimestampedModel):
    """Compliance result recorded for a company."""

    id: UUID = Field(default_factory=uuid4)
    company_record_id: UUID
    status: ComplianceStatus
    reasons: list[str] = Field(default_factory=list)
    relevance: RelevanceScoreBreakdown | None = None
    decided_at: datetime = Field(default_factory=utc_now)


class SkippedSource(UtcTimestampedModel):
    """A source excluded from a research run and the reason why."""

    id: UUID = Field(default_factory=uuid4)
    research_run_id: UUID
    url: HttpUrl
    reason: str = Field(min_length=1, max_length=1_000)
    skipped_at: datetime = Field(default_factory=utc_now)
