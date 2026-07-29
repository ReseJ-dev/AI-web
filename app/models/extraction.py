"""Strict models for evidence-based structured company extraction."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator

from app.models.domain import normalize_field_name


class FactBasis(StrEnum):
    """Whether a value is stated by a source or inferred from supported facts."""

    EXPLICIT = "explicit"
    INFERENCE = "inference"


class ExtractionStatus(StrEnum):
    """Final validation status for a structured extraction."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ExtractionMethod(StrEnum):
    """Deterministic or model-assisted method supporting a field."""

    CANONICAL_URL = "canonical_url"
    CONTACT_LINK = "contact_link"
    HEADING = "heading"
    JSON_LD_ORGANIZATION = "json_ld_organization"
    META_DESCRIPTION = "meta_description"
    OPEN_GRAPH = "open_graph"
    PAGE_TITLE = "page_title"
    SERVICE_SECTION = "service_section"
    COMBINED_DETERMINISTIC = "combined_deterministic"
    LLM = "llm"


class SupportedField(BaseModel):
    """One nullable company field with source-level provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    value: JsonValue = None
    evidence_urls: list[HttpUrl] = Field(default_factory=list)
    basis: FactBasis | None = None
    evidence_fragment: str | None = Field(default=None, max_length=500)
    extraction_method: ExtractionMethod | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_name(cls, data: object) -> object:
        """Normalize field names before validating the strict payload."""
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            return {**data, "name": normalize_field_name(data["name"])}
        return data

    @model_validator(mode="after")
    def validate_support(self) -> Self:
        """Require evidence and a basis for values, and neither for nulls."""
        if self.value is None:
            if (
                self.evidence_urls
                or self.basis is not None
                or self.evidence_fragment is not None
                or self.extraction_method is not None
                or self.confidence is not None
            ):
                raise ValueError("null fields must not claim extraction provenance")
            return self
        if not self.evidence_urls:
            raise ValueError("non-null fields require at least one evidence URL")
        if self.basis is None:
            raise ValueError("non-null fields require an explicit or inference basis")
        provenance = (
            self.evidence_fragment,
            self.extraction_method,
            self.confidence,
        )
        if any(item is not None for item in provenance) and any(
            item is None for item in provenance
        ):
            raise ValueError(
                "field evidence fragment, extraction method, and confidence "
                "must be supplied together"
            )
        if self.evidence_fragment is not None and not self.evidence_fragment.strip():
            raise ValueError("field evidence fragments must not be blank")
        if isinstance(self.value, str):
            maximum = 1_000 if self.name == "summary" else 2_000
            if len(self.value) > maximum:
                raise ValueError(f"{self.name} exceeds the supported text length")
        if (
            self.name == "services"
            and isinstance(self.value, list)
            and any(isinstance(item, str) and len(item) > 300 for item in self.value)
        ):
            raise ValueError("service names must not contain long source passages")
        return self


class LLMCompanyResponse(BaseModel):
    """Vendor-neutral JSON shape returned by an LLM provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: list[SupportedField]

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        """Prevent ambiguous duplicate values for the same field."""
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("LLM response field names must be unique")
        return self


class CompanyExtraction(BaseModel):
    """Validated extraction result produced by any extraction strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ExtractionStatus
    fields: list[SupportedField]
    rejection_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        """Keep rejection state and its human-readable reasons consistent."""
        if self.status is ExtractionStatus.REJECTED and not self.rejection_reasons:
            raise ValueError("rejected extractions require at least one reason")
        if self.status is ExtractionStatus.ACCEPTED and self.rejection_reasons:
            raise ValueError("accepted extractions must not include rejection reasons")
        return self

    def field(self, name: str) -> SupportedField | None:
        """Return a field by its normalized name."""
        normalized = normalize_field_name(name)
        return next((field for field in self.fields if field.name == normalized), None)


class LLMPageInput(BaseModel):
    """The only website content supplied to an LLM: clean text and its URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_url: HttpUrl
    cleaned_text: str = Field(max_length=200_000)


class LLMExtractionRequest(BaseModel):
    """Vendor-neutral structured-generation request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1, max_length=200)
    requested_fields: list[str]
    pages: list[LLMPageInput] = Field(min_length=1)
    instructions: str = Field(min_length=1, max_length=10_000)
