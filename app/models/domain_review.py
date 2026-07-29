"""Auditable domain inspection and manual-review records."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, HttpUrl

from app.models.compliance import RobotsPolicyRecord
from app.models.domain import DomainModel, UtcTimestampedModel, utc_now


class ReviewDecision(StrEnum):
    """Explicit changes a human reviewer may make."""

    APPROVED = "approved"
    REJECTED = "rejected"
    REMOVED = "removed"


class RedirectObservation(DomainModel):
    """One public homepage redirect response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    http_status: int = Field(ge=100, le=599)
    location: HttpUrl | None = None


class DomainInspection(DomainModel):
    """Evidence gathered for a manual domain-policy review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    normalized_domain: str
    source_policy_status: Literal[
        "approved",
        "rejected",
        "manual_review_required",
    ]
    source_policy_reason: str
    robots: RobotsPolicyRecord
    terms_page_candidates: list[HttpUrl] = Field(default_factory=list)
    automated_access_risk_signals: list[str] = Field(default_factory=list)
    redirects: list[RedirectObservation] = Field(default_factory=list)
    proposed_public_paths: list[HttpUrl] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DomainReviewRecord(UtcTimestampedModel):
    """Append-only audit entry for an explicit human review decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    decision: ReviewDecision
    reviewer: str = Field(min_length=1, max_length=200)
    timestamp: datetime = Field(default_factory=utc_now)
    review_note: str = Field(min_length=1, max_length=2_000)
    robots_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    terms_page_url: HttpUrl | None = None
