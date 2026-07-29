"""Replaceable result exporter abstraction."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.models.domain import ResearchRun
from app.models.orchestration import ExportArtifact, RankedCompanyRecord


@runtime_checkable
class ResultExporter(Protocol):
    """Export a completed or partially successful research result."""

    format_name: str

    async def export(
        self,
        run: ResearchRun,
        records: Sequence[RankedCompanyRecord],
    ) -> ExportArtifact:
        """Export final ranked records and return an artifact reference."""
        ...
