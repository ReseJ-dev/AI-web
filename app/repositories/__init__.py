"""Persistence repository contracts."""

from app.repositories.interfaces import (
    CompanyRecordRepository,
    ComplianceDecisionRepository,
    ResearchRunRepository,
    SkippedSourceRepository,
)

__all__ = [
    "CompanyRecordRepository",
    "ComplianceDecisionRepository",
    "ResearchRunRepository",
    "SkippedSourceRepository",
]
