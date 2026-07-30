"""Tests for configurable source-domain policies."""

from pathlib import Path

import pytest
import yaml

from app.services import (
    SourcePolicyDecision,
    SourcePolicyService,
    normalize_domain,
)

PROJECT_CONFIG = Path("config")


def _write_policy_files(
    config_dir: Path,
    *,
    approved_exact: list[str] | None = None,
    approved_subdomains: list[str] | None = None,
    blocked_exact: list[str] | None = None,
    blocked_subdomains: list[str] | None = None,
    candidates: list[str] | None = None,
) -> None:
    """Create a complete policy fixture in a temporary directory."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "approved_domains.yaml").write_text(
        yaml.safe_dump(
            {
                "exact_domains": approved_exact or [],
                "include_subdomains": approved_subdomains or [],
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "blocked_domains.yaml").write_text(
        yaml.safe_dump(
            {
                "exact_domains": blocked_exact or [],
                "include_subdomains": blocked_subdomains or [],
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "source_policies.yaml").write_text(
        yaml.safe_dump(
            {
                "candidate_review": {
                    "exact_domains": candidates or [],
                    "include_subdomains": [],
                },
                "unknown_domain_decision": "manual_review_required",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "source",
    [
        "shopify.com",
        "https://linkedin.com/company/example",
        "agency.clutch.co",
        "api.crunchbase.com",
        "WWW.FACEBOOK.COM",
        "instagram.com",
    ],
)
def test_blocked_domains_are_rejected(source: str) -> None:
    """Configured blocked hosts and their subdomains are rejected."""
    result = SourcePolicyService(PROJECT_CONFIG).evaluate(source)

    assert result.decision is SourcePolicyDecision.REJECTED
    assert "blocked" in result.reason


def test_exact_approved_domain_treats_www_as_apex_alias(tmp_path: Path) -> None:
    """The conventional www host shares an apex exact-domain decision."""
    _write_policy_files(tmp_path, approved_exact=["trusted.example"])
    service = SourcePolicyService(tmp_path)

    approved = service.evaluate("https://trusted.example/about")
    subdomain = service.evaluate("www.trusted.example")

    assert approved.decision is SourcePolicyDecision.APPROVED
    assert "exact-domain rule" in approved.reason
    assert subdomain.decision is SourcePolicyDecision.APPROVED


def test_subdomain_rule_approves_domain_and_descendants(tmp_path: Path) -> None:
    """Recursive rules match the configured domain and its descendants."""
    _write_policy_files(
        tmp_path,
        approved_subdomains=["research.example"],
    )
    service = SourcePolicyService(tmp_path)

    assert (
        service.evaluate("research.example").decision is SourcePolicyDecision.APPROVED
    )
    result = service.evaluate("eu.data.research.example")
    assert result.decision is SourcePolicyDecision.APPROVED
    assert "domain-and-subdomains rule" in result.reason


def test_unknown_and_candidate_domains_require_manual_review() -> None:
    """Candidates are not automatically approved and unknown hosts are held."""
    service = SourcePolicyService(PROJECT_CONFIG)

    candidate = service.evaluate("askphill.com")
    unknown = service.evaluate("previously-unseen.example")

    assert candidate.decision is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    assert "candidate" in candidate.reason
    assert unknown.decision is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    assert "no approved or blocked" in unknown.reason


@pytest.mark.parametrize(
    "source",
    [
        "",
        "not a domain",
        "https://",
        "localhost",
        "127.0.0.1",
        "bad_domain.example",
    ],
)
def test_malformed_domains_are_rejected(source: str) -> None:
    """Malformed sources produce an explained rejection instead of an error."""
    result = SourcePolicyService(PROJECT_CONFIG).evaluate(source)

    assert result.decision is SourcePolicyDecision.REJECTED
    assert result.normalized_domain is None
    assert "malformed" in result.reason


def test_internationalized_domains_are_normalized_and_matched(
    tmp_path: Path,
) -> None:
    """Unicode hostnames use canonical IDNA form for policy matching."""
    _write_policy_files(
        tmp_path,
        approved_subdomains=["münich.example"],
    )

    result = SourcePolicyService(tmp_path).evaluate(
        "https://BÜCHER.MÜNICH.example/catalogue"
    )

    assert result.decision is SourcePolicyDecision.APPROVED
    assert result.normalized_domain == "xn--bcher-kva.xn--mnich-kva.example"
    assert normalize_domain("MÜNICH.example.") == "xn--mnich-kva.example"


def test_reload_replaces_the_cached_policy(tmp_path: Path) -> None:
    """Configuration changes are invisible until an explicit atomic reload."""
    _write_policy_files(tmp_path)
    service = SourcePolicyService(tmp_path)

    assert (
        service.evaluate("newly-approved.example").decision
        is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    )

    _write_policy_files(
        tmp_path,
        approved_exact=["newly-approved.example"],
    )
    assert (
        service.evaluate("newly-approved.example").decision
        is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    )

    service.reload()

    assert (
        service.evaluate("newly-approved.example").decision
        is SourcePolicyDecision.APPROVED
    )
