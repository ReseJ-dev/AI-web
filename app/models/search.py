"""Transient search-provider models."""

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
)

CountryCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=2),
]
LanguageCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=20),
]


class SearchParameters(BaseModel):
    """Validated parameters shared by all search providers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=400)
    country: CountryCode = "US"
    language: LanguageCode = "en"
    count: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=9)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """Collapse query whitespace and enforce Brave's 50-word limit."""
        query = " ".join(value.split())
        if not query:
            raise ValueError("query must not be blank")
        if len(query.split()) > 50:
            raise ValueError("query must contain no more than 50 words")
        return query

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        """Normalize ISO-style country codes."""
        if not value.isalpha():
            raise ValueError("country must contain two letters")
        return value.upper()

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        """Normalize search language codes."""
        normalized = value.lower()
        if not all(part.isalpha() for part in normalized.split("-")):
            raise ValueError("language must contain letters and optional hyphens")
        return normalized


class SearchCandidate(BaseModel):
    """Normalized, transient candidate returned by a search provider.

    This model intentionally has no snippet field and is not mapped to any
    persistence model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    title: str = Field(min_length=1, max_length=1_000)
    domain: str = Field(min_length=3, max_length=253)
    rank: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=50)
