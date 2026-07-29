"""Offline guarantees for the optional live website smoke-test workflow."""

from pathlib import Path

from app.core.live_smoke import (
    LIVE_SMOKE_ENVIRONMENT_FLAG,
    live_smoke_enabled,
    load_live_smoke_config,
)
from app.services import SourcePolicyDecision, SourcePolicyService

EXPECTED_SAMPLE_DOMAINS = {
    "askphill.com",
    "opklopper.nl",
    "shopmonkey.nl",
    "code.digital",
}


def test_live_smoke_requires_explicit_true_flag() -> None:
    """Unset and ambiguous environment values never enable network tests."""
    assert live_smoke_enabled({}) is False
    assert live_smoke_enabled({LIVE_SMOKE_ENVIRONMENT_FLAG: ""}) is False
    assert live_smoke_enabled({LIVE_SMOKE_ENVIRONMENT_FLAG: "1"}) is False
    assert live_smoke_enabled({LIVE_SMOKE_ENVIRONMENT_FLAG: "yes"}) is False
    assert live_smoke_enabled({LIVE_SMOKE_ENVIRONMENT_FLAG: "false"}) is False
    assert live_smoke_enabled({LIVE_SMOKE_ENVIRONMENT_FLAG: "true"}) is True
    assert live_smoke_enabled({LIVE_SMOKE_ENVIRONMENT_FLAG: " TRUE "}) is True


def test_sample_domains_remain_manual_review_only() -> None:
    """The sample list never grants approval and current policy approves none."""
    config = load_live_smoke_config(Path("config/live_smoke_domains.yaml"))
    policy = SourcePolicyService(Path("config"))

    assert config.requires_manual_approval is True
    assert set(config.domains) == EXPECTED_SAMPLE_DOMAINS
    assert config.maximum_pages_per_domain == 1
    assert config.maximum_redirects <= 2
    assert config.maximum_response_bytes <= 500_000
    assert config.request_delay_seconds >= 1
    assert "live" in config.user_agent.casefold()
    assert "smoke" in config.user_agent.casefold()
    for domain in config.domains:
        result = policy.evaluate(domain)
        assert result.decision is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
        assert "candidate" in result.reason
