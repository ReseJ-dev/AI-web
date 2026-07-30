"""Strict loading for the synthetic offline portfolio scenario."""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.core.yaml_config import load_yaml_mapping


class DemoResearchRequest(BaseModel):
    """Research brief displayed by the offline scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=1, max_length=500)
    required_result_count: int = Field(ge=1, le=100)
    required_fields: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def fields_are_unique(self) -> Self:
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("demo requested fields must be unique")
        return self


class DemoWorksheet(BaseModel):
    """One Google Sheets tab and its ordered columns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    columns: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def columns_are_unique(self) -> Self:
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("demo worksheet columns must be unique")
        return self


class DemoOfflineFiles(BaseModel):
    """Committed files that make the scenario usable without network access."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fake_results: Path
    google_sheets_previews: tuple[Path, ...] = Field(min_length=1)


class DemoScenario(BaseModel):
    """Validated demonstration configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    demo_data: Literal[True]
    demo_notice: str = Field(min_length=20, max_length=1_000)
    scenario_id: str = Field(pattern=r"^[a-z0-9-]+$")
    research_request: DemoResearchRequest
    example_search_queries: tuple[str, ...] = Field(min_length=1)
    google_sheets_output: tuple[DemoWorksheet, ...] = Field(min_length=1)
    offline_files: DemoOfflineFiles

    @model_validator(mode="after")
    def collections_are_unique(self) -> Self:
        if len(self.example_search_queries) != len(set(self.example_search_queries)):
            raise ValueError("demo search queries must be unique")
        sheet_names = [sheet.name for sheet in self.google_sheets_output]
        if len(sheet_names) != len(set(sheet_names)):
            raise ValueError("demo worksheet names must be unique")
        if "demo" not in self.demo_notice.casefold():
            raise ValueError("demo notice must clearly identify demo data")
        return self


class DemoCompanyResult(BaseModel):
    """One deliberately fictional company result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    demo_data: Literal[True]
    company_name: str = Field(pattern=r"^DEMO — ")
    website: HttpUrl
    country: Literal["Netherlands"]
    services: tuple[str, ...] = Field(min_length=1)
    contact_page: HttpUrl
    short_summary: str = Field(min_length=20, max_length=500)
    relevance_score: int = Field(ge=0, le=100)
    relevance_explanation: tuple[str, ...] = Field(min_length=1)
    evidence_urls: tuple[HttpUrl, ...] = Field(min_length=1)
    validation_warnings: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def uses_only_reserved_demo_sources(self) -> Self:
        urls = (self.website, self.contact_page, *self.evidence_urls)
        if any(url.host is None or not url.host.endswith(".example") for url in urls):
            raise ValueError("demo results may use only reserved .example domains")
        warning_text = " ".join(self.validation_warnings).casefold()
        if "demo data" not in warning_text or "fictional" not in warning_text:
            raise ValueError("demo results must carry a clear fictional-data warning")
        return self


class DemoResultDataset(BaseModel):
    """Complete synthetic result set for one scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    demo_data: Literal[True]
    demo_notice: str = Field(min_length=20, max_length=1_000)
    scenario_id: str = Field(pattern=r"^[a-z0-9-]+$")
    total: int = Field(ge=1, le=100)
    items: tuple[DemoCompanyResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def total_matches_items(self) -> Self:
        if self.total != len(self.items):
            raise ValueError("demo result total must match the item count")
        if "demo" not in self.demo_notice.casefold():
            raise ValueError("demo dataset notice must clearly identify demo data")
        return self


def load_demo_scenario(path: Path) -> DemoScenario:
    """Load the synthetic scenario configuration without network access."""
    return DemoScenario.model_validate(load_yaml_mapping(path))


def load_demo_results(path: Path) -> DemoResultDataset:
    """Load the committed fictional result set."""
    return DemoResultDataset.model_validate_json(path.read_text(encoding="utf-8"))


def load_demo_bundle(
    scenario_path: Path,
    results_path: Path,
) -> tuple[DemoScenario, DemoResultDataset]:
    """Load and cross-check one configuration and its synthetic results."""
    scenario = load_demo_scenario(scenario_path)
    results = load_demo_results(results_path)
    if scenario.scenario_id != results.scenario_id:
        raise ValueError("demo scenario and result identifiers must match")
    if scenario.research_request.required_result_count != results.total:
        raise ValueError("demo result count must match the configured request")
    return scenario, results
