"""Configurable source-domain policy decisions."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.settings import get_settings
from app.core.yaml_config import load_yaml_mapping
from app.services.domain_normalization import InvalidDomainError, normalize_domain


class SourcePolicyDecision(StrEnum):
    """Available outcomes when evaluating a source domain."""

    APPROVED = "approved"
    REJECTED = "rejected"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class DomainRuleSet(BaseModel):
    """Exact and recursive domain rules loaded from YAML."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exact_domains: tuple[str, ...] = ()
    include_subdomains: tuple[str, ...] = ()

    @field_validator("exact_domains", "include_subdomains", mode="before")
    @classmethod
    def normalize_domains(cls, value: object) -> object:
        """Normalize configured domains and remove duplicates."""
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            return value
        normalized: list[str] = []
        for domain in value:
            if not isinstance(domain, str):
                return value
            normalized.append(normalize_domain(domain))
        return tuple(dict.fromkeys(normalized))


class SourcePoliciesFile(BaseModel):
    """Settings held in source_policies.yaml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_review: DomainRuleSet
    unknown_domain_decision: SourcePolicyDecision

    @field_validator("unknown_domain_decision")
    @classmethod
    def require_manual_review_default(
        cls,
        value: SourcePolicyDecision,
    ) -> SourcePolicyDecision:
        """Prevent unknown sources from being silently approved or rejected."""
        if value is not SourcePolicyDecision.MANUAL_REVIEW_REQUIRED:
            raise ValueError("unknown_domain_decision must be manual_review_required")
        return value


@dataclass(frozen=True, slots=True)
class SourcePolicyResult:
    """Source-policy outcome with its canonical domain and explanation."""

    decision: SourcePolicyDecision
    normalized_domain: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class _PolicySnapshot:
    """Immutable policy state that can be swapped atomically."""

    approved: DomainRuleSet
    blocked: DomainRuleSet
    candidates: DomainRuleSet


@dataclass(frozen=True, slots=True)
class _RuleMatch:
    """The configured rule responsible for a policy match."""

    domain: str
    includes_subdomains: bool


def _match_rule(domain: str, rules: DomainRuleSet) -> _RuleMatch | None:
    """Find the most specific exact or recursive rule for a domain."""
    aliases = {domain, domain.removeprefix("www.")}
    aliases.update(
        item.removeprefix("www.")
        for item in rules.exact_domains
        if item.startswith("www.")
    )
    exact = next(
        (
            rule
            for rule in rules.exact_domains
            if rule in aliases or rule.removeprefix("www.") == domain
        ),
        None,
    )
    if exact is not None:
        return _RuleMatch(domain=exact, includes_subdomains=False)

    for rule in sorted(rules.include_subdomains, key=len, reverse=True):
        if domain == rule or domain.endswith(f".{rule}"):
            return _RuleMatch(domain=rule, includes_subdomains=True)
    return None


class SourcePolicyService:
    """Evaluate source domains against cached YAML policy configuration."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self._config_dir = config_dir or get_settings().source_policy_config_dir
        self._lock = RLock()
        self._snapshot: _PolicySnapshot | None = None

    def _load_snapshot(self) -> _PolicySnapshot:
        """Load and validate a complete immutable policy snapshot."""
        approved = DomainRuleSet.model_validate(
            load_yaml_mapping(self._config_dir / "approved_domains.yaml")
        )
        blocked = DomainRuleSet.model_validate(
            load_yaml_mapping(self._config_dir / "blocked_domains.yaml")
        )
        policies = SourcePoliciesFile.model_validate(
            load_yaml_mapping(self._config_dir / "source_policies.yaml")
        )
        return _PolicySnapshot(
            approved=approved,
            blocked=blocked,
            candidates=policies.candidate_review,
        )

    def _get_snapshot(self) -> _PolicySnapshot:
        """Load the policy once and return its cached immutable snapshot."""
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._load_snapshot()
            return self._snapshot

    def reload(self) -> None:
        """Reload YAML files and atomically replace the cached policy."""
        replacement = self._load_snapshot()
        with self._lock:
            self._snapshot = replacement

    def evaluate(self, source: str) -> SourcePolicyResult:
        """Return a policy decision and human-readable reason for a source."""
        try:
            domain = normalize_domain(source)
        except InvalidDomainError as error:
            return SourcePolicyResult(
                decision=SourcePolicyDecision.REJECTED,
                normalized_domain=None,
                reason=f"Rejected because the source domain is malformed: {error}.",
            )

        snapshot = self._get_snapshot()

        blocked_match = _match_rule(domain, snapshot.blocked)
        if blocked_match is not None:
            return SourcePolicyResult(
                decision=SourcePolicyDecision.REJECTED,
                normalized_domain=domain,
                reason=self._match_reason("blocked", blocked_match, domain),
            )

        approved_match = _match_rule(domain, snapshot.approved)
        if approved_match is not None:
            return SourcePolicyResult(
                decision=SourcePolicyDecision.APPROVED,
                normalized_domain=domain,
                reason=self._match_reason("approved", approved_match, domain),
            )

        candidate_match = _match_rule(domain, snapshot.candidates)
        if candidate_match is not None:
            return SourcePolicyResult(
                decision=SourcePolicyDecision.MANUAL_REVIEW_REQUIRED,
                normalized_domain=domain,
                reason=(
                    f"Manual review is required because {domain} is configured "
                    f"as a candidate domain ({candidate_match.domain}) and has "
                    "not been approved."
                ),
            )

        return SourcePolicyResult(
            decision=SourcePolicyDecision.MANUAL_REVIEW_REQUIRED,
            normalized_domain=domain,
            reason=(
                f"Manual review is required because {domain} has no approved "
                "or blocked source rule."
            ),
        )

    def decide(self, source: str) -> SourcePolicyResult:
        """Alias for evaluate, suitable for policy-oriented call sites."""
        return self.evaluate(source)

    @staticmethod
    def _match_reason(
        policy_name: str,
        match: _RuleMatch,
        domain: str,
    ) -> str:
        """Describe the exact configuration rule responsible for a decision."""
        outcome = "Approved" if policy_name == "approved" else "Rejected"
        rule_kind = (
            "domain-and-subdomains rule"
            if match.includes_subdomains
            else "exact-domain rule"
        )
        return (
            f"{outcome} because {domain} matches the {policy_name} "
            f"{rule_kind} for {match.domain}."
        )
