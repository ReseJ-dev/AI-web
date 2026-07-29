"""Persistence repository contracts."""

from app.repositories.interfaces import (
    CompanyRecordRepository,
    ComplianceDecisionRepository,
    ResearchRunRepository,
    SkippedSourceRepository,
)
from app.repositories.sqlalchemy import (
    SqlAlchemyCompanyRecordRepository,
    SqlAlchemyResearchRunRepository,
    SqlAlchemySkippedSourceRepository,
)

__all__ = [
    "CompanyRecordRepository",
    "ComplianceDecisionRepository",
    "ResearchRunRepository",
    "SkippedSourceRepository",
    "SqlAlchemyCompanyRecordRepository",
    "SqlAlchemyResearchRunRepository",
    "SqlAlchemySkippedSourceRepository",
]
