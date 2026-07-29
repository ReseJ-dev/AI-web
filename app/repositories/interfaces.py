"""Repository interfaces for domain persistence."""

from typing import Protocol
from uuid import UUID

from app.models.domain import (
    CompanyRecord,
    ComplianceDecision,
    ResearchRun,
    SkippedSource,
)


class ResearchRunRepository(Protocol):
    """Persistence operations for research runs."""

    def add(self, run: ResearchRun) -> ResearchRun:
        """Store and return a research run."""
        ...

    def get(self, run_id: UUID) -> ResearchRun | None:
        """Return a research run by identifier."""
        ...

    def update(self, run: ResearchRun) -> ResearchRun:
        """Persist changes to a research run."""
        ...


class CompanyRecordRepository(Protocol):
    """Persistence operations for company records."""

    def add(self, company: CompanyRecord) -> CompanyRecord:
        """Store and return a company record."""
        ...

    def get(self, company_id: UUID) -> CompanyRecord | None:
        """Return a company record by identifier."""
        ...

    def list_for_run(self, run_id: UUID) -> list[CompanyRecord]:
        """Return all company records for a research run."""
        ...


class ComplianceDecisionRepository(Protocol):
    """Persistence operations for compliance decisions."""

    def add(self, decision: ComplianceDecision) -> ComplianceDecision:
        """Store and return a compliance decision."""
        ...

    def get_for_company(self, company_id: UUID) -> ComplianceDecision | None:
        """Return the compliance decision for a company."""
        ...


class SkippedSourceRepository(Protocol):
    """Persistence operations for skipped sources."""

    def add(self, source: SkippedSource) -> SkippedSource:
        """Store and return a skipped source."""
        ...

    def list_for_run(self, run_id: UUID) -> list[SkippedSource]:
        """Return all skipped sources for a research run."""
        ...
