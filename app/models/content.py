"""Models for company-page selection and clean HTML extraction."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue


class PageCategory(StrEnum):
    """Company-page categories in preferred extraction order."""

    HOMEPAGE = "homepage"
    ABOUT = "about"
    SERVICES = "services"
    SOLUTIONS = "solutions"
    EXPERTISE = "expertise"
    CONTACT = "contact"
    RELEVANT = "relevant"
    OTHER = "other"


class NavigationLink(BaseModel):
    """Normalized link and visible anchor label found in a page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    text: str = Field(default="", max_length=1_000)


class PageCandidate(BaseModel):
    """Same-domain page metadata available to the selection service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    anchor_text: str = Field(default="", max_length=1_000)
    title: str = Field(default="", max_length=1_000)
    headings: list[str] = Field(default_factory=list)
    navigation_position: int | None = Field(default=None, ge=0)


class RankedPage(BaseModel):
    """Page candidate with a deterministic ranking explanation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    category: PageCategory
    score: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)
    anchor_text: str = ""
    title: str = ""
    headings: list[str] = Field(default_factory=list)
    navigation_position: int | None = Field(default=None, ge=0)


class ExtractedPageContent(BaseModel):
    """Structured metadata and LLM-bounded visible page text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_url: HttpUrl
    canonical_url: HttpUrl
    title: str | None = Field(default=None, max_length=2_000)
    meta_description: str | None = Field(default=None, max_length=5_000)
    open_graph: dict[str, str] = Field(default_factory=dict)
    organization_data: list[dict[str, JsonValue]] = Field(default_factory=list)
    main_text: str
    headings: list[str] = Field(default_factory=list)
    navigation_links: list[NavigationLink] = Field(default_factory=list)
    contact_page_candidates: list[NavigationLink] = Field(default_factory=list)
    source_html_length: int = Field(ge=0)
    extracted_text_length: int = Field(ge=0)
    truncated: bool = False
