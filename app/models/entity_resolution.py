"""Models for explainable company deduplication and entity resolution."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator

from app.models.domain import CompanyRecord


class EntityResolutionOutcome(StrEnum):
    """Possible decisions for a pair of company entities."""

    MERGE = "merge"
    KEEP_SEPARATE = "keep_separate"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class OfficialIdentifierSource(StrEnum):
    """Supported authoritative company-identifier sources."""

    OPENCORPORATES = "opencorporates"
    WIKIDATA = "wikidata"


class OfficialIdentifier(BaseModel):
    """An identifier asserted by an official external entity source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: OfficialIdentifierSource
    value: str = Field(min_length=1, max_length=500)


class CompanyEntity(BaseModel):
    """A company record plus redirect and official-identifier context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: CompanyRecord
    canonical_url: HttpUrl | None = None
    official_identifiers: list[OfficialIdentifier] = Field(default_factory=list)


class MergeMetadata(BaseModel):
    """Provenance retained alongside a merged company record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_record_ids: list[UUID] = Field(min_length=2)
    alternative_values: dict[str, list[JsonValue]] = Field(default_factory=dict)
    evidence_urls: list[HttpUrl] = Field(default_factory=list)
    explanation: list[str] = Field(min_length=1)


class EntityResolutionResult(BaseModel):
    """Decision, signals, and optional merged entity for one record pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left_record_id: UUID
    right_record_id: UUID
    outcome: EntityResolutionOutcome
    confidence: float = Field(ge=0, le=1)
    fuzzy_name_score: float | None = Field(default=None, ge=0, le=100)
    explanation: list[str] = Field(min_length=1)
    merged_company: CompanyRecord | None = None
    merge_metadata: MergeMetadata | None = None

    @model_validator(mode="after")
    def validate_merge_payload(self) -> Self:
        """Require merge payloads only for merge decisions."""
        if self.outcome is EntityResolutionOutcome.MERGE:
            if self.merged_company is None or self.merge_metadata is None:
                raise ValueError("merge decisions require a company and merge metadata")
        elif self.merged_company is not None or self.merge_metadata is not None:
            raise ValueError("non-merge decisions must not contain merged data")
        return self
