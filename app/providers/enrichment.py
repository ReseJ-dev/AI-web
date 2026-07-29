"""Replaceable structured company enrichment abstraction."""

from typing import Protocol, runtime_checkable

from app.models.domain import CompanyRecord
from app.models.orchestration import EnrichmentResult


@runtime_checkable
class CompanyEnrichmentProvider(Protocol):
    """Enrich a company from an independently configured structured source."""

    provider_name: str

    async def enrich(self, company: CompanyRecord) -> EnrichmentResult:
        """Return supported additions without discarding existing evidence."""
        ...
