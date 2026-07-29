"""Transient compliance-preflight records."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.domain import UtcTimestampedModel, utc_now


class PreflightDecision(StrEnum):
    """Outcome used by robots, terms, and combined preflight checks."""

    APPROVED = "approved"
    REJECTED = "rejected"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class RobotsPolicyRecord(UtcTimestampedModel):
    """Auditable outcome of checking one path against robots.txt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    robots_url: HttpUrl
    http_status: int | None = Field(default=None, ge=100, le=599)
    requested_path: str = Field(min_length=1, max_length=8_192)
    decision: PreflightDecision
    checked_at: datetime = Field(default_factory=utc_now)
    response_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason: str = Field(min_length=1, max_length=2_000)


class TermsLink(BaseModel):
    """Public legal or terms link discovered in an HTML page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    label: str = Field(default="", max_length=1_000)


class TermsPolicyResult(UtcTimestampedModel):
    """Advisory terms scan result; never a legal conclusion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    terms_url: HttpUrl
    decision: PreflightDecision
    signals: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)
    reason: str = Field(min_length=1, max_length=2_000)


class CompliancePreflightResult(UtcTimestampedModel):
    """Combined source-domain, robots, and terms preflight outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_url: HttpUrl
    normalized_domain: str | None = None
    decision: PreflightDecision
    domain_reason: str
    robots_checks: list[RobotsPolicyRecord] = Field(default_factory=list)
    terms_results: list[TermsPolicyResult] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)
    reason: str = Field(min_length=1, max_length=4_000)
