"""Application services."""

from app.services.company_deduplication import (
    CompanyDeduplicationService,
    normalize_company_name,
    normalize_company_url,
    normalize_official_identifier,
    registrable_domain,
)
from app.services.compliance_preflight import CompliancePreflightService
from app.services.domain_normalization import (
    InvalidDomainError,
    normalize_domain,
)
from app.services.html_content_extractor import HtmlContentExtractor
from app.services.page_selection import PageSelectionService
from app.services.query_planner import QueryPlanner
from app.services.robots_policy import RobotsPolicyService
from app.services.source_policy import (
    SourcePolicyDecision,
    SourcePolicyResult,
    SourcePolicyService,
)
from app.services.structured_data_extraction import (
    CompositeCompanyExtractor,
    DeterministicCompanyExtractor,
    LLMCompanyExtractor,
    StructuredDataExtractor,
)
from app.services.terms_policy import TermsPolicyScanner

__all__ = [
    "CompanyDeduplicationService",
    "CompliancePreflightService",
    "CompositeCompanyExtractor",
    "DeterministicCompanyExtractor",
    "HtmlContentExtractor",
    "InvalidDomainError",
    "LLMCompanyExtractor",
    "PageSelectionService",
    "QueryPlanner",
    "RobotsPolicyService",
    "SourcePolicyDecision",
    "SourcePolicyResult",
    "SourcePolicyService",
    "StructuredDataExtractor",
    "TermsPolicyScanner",
    "normalize_company_name",
    "normalize_company_url",
    "normalize_domain",
    "normalize_official_identifier",
    "registrable_domain",
]
