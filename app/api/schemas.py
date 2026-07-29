"""Public, credential-free schemas for the research HTTP API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.models import (
    ExportArtifact,
    RequestedField,
    ResearchProgressStage,
    ResearchRunStatus,
)


class ApiModel(BaseModel):
    """Strict base for public API payloads."""

    model_config = ConfigDict(extra="forbid")


class CreateResearchRunRequest(ApiModel):
    """Parameters accepted when starting an asynchronous research run."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "topic": "Shopify agencies in the Netherlands",
                    "requested_fields": [
                        {"name": "country"},
                        {"name": "services"},
                        {"name": "contact page"},
                    ],
                    "result_count": 30,
                    "location": "Netherlands",
                    "country": "NL",
                    "language": "en",
                    "country_tld": "nl",
                }
            ]
        },
    )

    topic: str = Field(min_length=1, max_length=500)
    requested_fields: list[RequestedField] = Field(min_length=1)
    result_count: int = Field(default=10, ge=1, le=100)
    location: str = Field(min_length=1, max_length=200)
    country: str = Field(default="US", pattern=r"^[A-Za-z]{2}$")
    language: str = Field(
        default="en",
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,8})?$",
    )
    city: str | None = Field(default=None, max_length=200)
    country_tld: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,63}$")

    @field_validator("topic", "location", "city", mode="before")
    @classmethod
    def compact_text(cls, value: object) -> object:
        """Trim human-entered text and turn optional blanks into null."""
        if not isinstance(value, str):
            return value
        compacted = " ".join(value.split())
        return compacted or None

    @field_validator("country", "language", "country_tld")
    @classmethod
    def normalize_codes(cls, value: str | None) -> str | None:
        """Normalize locale and TLD values without changing their meaning."""
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def requested_fields_are_unique(self) -> "CreateResearchRunRequest":
        """Reject duplicate fields after RequestedField normalization."""
        names = [field.name for field in self.requested_fields]
        if len(names) != len(set(names)):
            raise ValueError("requested field names must be unique")
        return self


class ResearchRunResponse(ApiModel):
    """Pollable research-run status with latest progress information."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "id": "6f9619ff-8b86-d011-b42d-00cf4fc964ff",
                    "status": "running",
                    "progress_stage": "crawling",
                    "progress_message": "Crawling approved company websites.",
                    "completed_items": 8,
                    "total_items": 30,
                    "partial_result_count": 0,
                    "discovered_candidate_count": 24,
                    "approved_candidate_count": 8,
                    "skipped_source_count": 16,
                    "completed_result_count": 0,
                    "warnings": [],
                    "error_message": None,
                    "created_at": "2026-07-29T10:00:00Z",
                    "updated_at": "2026-07-29T10:01:00Z",
                }
            ]
        },
    )

    id: UUID
    status: ResearchRunStatus
    progress_stage: ResearchProgressStage | None = None
    progress_message: str | None = None
    completed_items: int = Field(default=0, ge=0)
    total_items: int = Field(ge=1, le=100)
    partial_result_count: int = Field(default=0, ge=0)
    discovered_candidate_count: int = Field(default=0, ge=0)
    approved_candidate_count: int = Field(default=0, ge=0)
    skipped_source_count: int = Field(default=0, ge=0)
    completed_result_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ResearchResultItem(ApiModel):
    """One independently verified company safe for API delivery."""

    id: UUID
    company_name: str
    website: HttpUrl | None = None
    country: str | None = None
    services: list[str] = Field(default_factory=list)
    contact_page: HttpUrl | None = None
    short_summary: str | None = None
    relevance_score: int | None = Field(default=None, ge=0, le=100)
    relevance_explanation: list[str] = Field(default_factory=list)
    evidence_urls: list[HttpUrl] = Field(default_factory=list)
    compliance_status: Literal["approved"] = "approved"
    validation_warnings: list[str] = Field(default_factory=list)
    retrieved_at: datetime


class ResearchResultsResponse(ApiModel):
    """A stable slice of final or partial research results."""

    run_id: UUID
    items: list[ResearchResultItem]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    partial: bool


class SkippedSourceItem(ApiModel):
    """One source omitted from a run, without fetched page content."""

    id: UUID
    domain: str
    url: HttpUrl
    reason: str
    skipped_at: datetime


class SkippedSourcesResponse(ApiModel):
    """A stable slice of skipped-source audit records."""

    run_id: UUID
    items: list[SkippedSourceItem]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)


class GoogleSheetsExportResponse(ApiModel):
    """Google Sheets export artifact."""

    run_id: UUID
    artifact: ExportArtifact


class GoogleSheetsExportRequest(ApiModel):
    """Optional existing spreadsheet selected by the dashboard user."""

    spreadsheet_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]+$",
        max_length=256,
    )

    @field_validator("spreadsheet_id", mode="before")
    @classmethod
    def blank_spreadsheet_id_is_unset(cls, value: object) -> object:
        """Use server configuration when the input is blank."""
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value


class ProviderStatus(ApiModel):
    """Non-secret provider availability details."""

    name: str
    category: Literal["search", "extraction", "enrichment", "export"]
    enabled: bool
    configured: bool
    model: str | None = None


class ProvidersResponse(ApiModel):
    """Configured provider inventory with no credential values."""

    providers: list[ProviderStatus]


class ApiErrorDetail(ApiModel):
    """One safe validation-error location and explanation."""

    location: list[str | int]
    message: str
    error_type: str


class ApiError(ApiModel):
    """Machine-readable API error associated with a request ID."""

    code: str
    message: str
    request_id: str
    details: list[ApiErrorDetail] = Field(default_factory=list)


class ApiErrorResponse(ApiModel):
    """Consistent error envelope."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "research_run_not_found",
                        "message": "The research run was not found.",
                        "request_id": "portfolio-demo-1",
                        "details": [],
                    }
                }
            ]
        },
    )

    error: ApiError
