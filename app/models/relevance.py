"""Models for deterministic, explainable relevance scoring."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RelevanceComponent(StrEnum):
    """Fixed scoring components whose maxima sum to one hundred."""

    TOPIC_MATCH = "topic_match"
    LOCATION_MATCH = "location_match"
    RELEVANT_SERVICES = "relevant_services"
    OFFICIAL_WEBSITE_CONFIDENCE = "official_website_confidence"
    CONTACT_PAGE = "contact_page"
    EVIDENCE_QUALITY = "evidence_quality"
    REQUESTED_FIELD_COMPLETENESS = "requested_field_completeness"


class ScorePenalty(BaseModel):
    """A transparent deduction from a component's maximum score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    component: RelevanceComponent
    points: float = Field(gt=0, le=30)
    reason: str = Field(min_length=1, max_length=1_000)


class ComponentScore(BaseModel):
    """Earned points and rationale for one deterministic component."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(ge=0, le=30)
    maximum: int = Field(gt=0, le=30)
    explanation: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def score_does_not_exceed_maximum(self) -> Self:
        """Prevent malformed component totals."""
        if self.score > self.maximum:
            raise ValueError("component score must not exceed its maximum")
        return self


class RelevanceScoreResult(BaseModel):
    """Complete reproducible score with components and missing-evidence costs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_score: float = Field(ge=0, le=100)
    components: dict[RelevanceComponent, ComponentScore]
    explanation: list[str] = Field(min_length=1)
    missing_evidence_penalties: list[ScorePenalty] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        """Require the reported total to equal the component sum."""
        component_total = round(
            sum(component.score for component in self.components.values()),
            2,
        )
        if self.total_score != component_total:
            raise ValueError("total score must equal the sum of component scores")
        return self
