"""Configuration guards for explicitly enabled live website smoke tests."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.yaml_config import load_yaml_mapping
from app.services.domain_normalization import normalize_domain

LIVE_SMOKE_ENVIRONMENT_FLAG = "RUN_LIVE_WEBSITE_SMOKE_TESTS"


class LiveSmokeConfig(BaseModel):
    """Strict low-volume limits and manually reviewed smoke-test targets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires_manual_approval: Literal[True]
    user_agent: str = Field(min_length=10, max_length=200)
    request_delay_seconds: float = Field(ge=1, le=10)
    timeout_seconds: float = Field(gt=0, le=15)
    maximum_response_bytes: int = Field(ge=1_024, le=500_000)
    maximum_pages_per_domain: int = Field(ge=1, le=2)
    maximum_redirects: int = Field(ge=0, le=3)
    domains: tuple[str, ...] = Field(min_length=1, max_length=10)

    @field_validator("user_agent")
    @classmethod
    def require_descriptive_live_user_agent(cls, value: str) -> str:
        """Make live smoke traffic readily identifiable in server logs."""
        compacted = " ".join(value.split())
        if "live" not in compacted.casefold() or "smoke" not in compacted.casefold():
            raise ValueError("live smoke user agent must describe its purpose")
        return compacted

    @field_validator("domains", mode="before")
    @classmethod
    def normalize_domains(cls, value: object) -> object:
        """Normalize configured domains before enforcing uniqueness."""
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(
            normalize_domain(domain) if isinstance(domain, str) else domain
            for domain in value
        )

    @model_validator(mode="after")
    def domains_are_unique(self) -> Self:
        """Avoid sending duplicate live traffic to one domain."""
        if len(self.domains) != len(set(self.domains)):
            raise ValueError("live smoke domains must be unique")
        return self


def load_live_smoke_config(path: Path) -> LiveSmokeConfig:
    """Load a strict sample configuration without performing network access."""
    return LiveSmokeConfig.model_validate(load_yaml_mapping(path))


def live_smoke_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Require the explicit literal value ``true``; all other values are off."""
    values = environment if environment is not None else os.environ
    return values.get(LIVE_SMOKE_ENVIRONMENT_FLAG, "").strip().casefold() == "true"
