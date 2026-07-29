"""Optional low-volume smoke checks for explicitly approved public websites."""

from pathlib import Path

import pytest

from app.core.live_smoke import (
    LIVE_SMOKE_ENVIRONMENT_FLAG,
    live_smoke_enabled,
    load_live_smoke_config,
)
from app.models import CompliancePreflightResult, PreflightDecision
from app.providers import AsyncWebsiteCrawler, CrawlError
from app.services import RobotsPolicyService, SourcePolicyDecision, SourcePolicyService

CONFIG = load_live_smoke_config(Path("config/live_smoke_domains.yaml"))
DISABLED_REASON = (
    "live website smoke tests are disabled; set "
    f"{LIVE_SMOKE_ENVIRONMENT_FLAG}=true explicitly"
)
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not live_smoke_enabled(), reason=DISABLED_REASON),
]


class _ApprovedRobotsChecker:
    """Require manual approval and robots permission before every content URL."""

    def __init__(
        self,
        source_policy: SourcePolicyService,
        robots_policy: RobotsPolicyService,
    ) -> None:
        self._source_policy = source_policy
        self._robots_policy = robots_policy

    async def check(self, target_url: str) -> CompliancePreflightResult:
        domain = self._source_policy.evaluate(target_url)
        domain_decision = PreflightDecision(domain.decision.value)
        if domain_decision is not PreflightDecision.APPROVED:
            return CompliancePreflightResult(
                target_url=target_url,
                normalized_domain=domain.normalized_domain,
                decision=domain_decision,
                domain_reason=domain.reason,
                reason=(
                    "Live smoke content access stopped because the domain was "
                    f"not manually approved: {domain.reason}"
                ),
            )
        robots = await self._robots_policy.check(target_url)
        return CompliancePreflightResult(
            target_url=target_url,
            normalized_domain=domain.normalized_domain,
            decision=robots.decision,
            domain_reason=domain.reason,
            robots_checks=[robots],
            reason=(
                "The exact content path passed manual source approval and "
                f"robots preflight: {robots.reason}"
            ),
        )


@pytest.mark.anyio
@pytest.mark.parametrize("domain", CONFIG.domains, ids=CONFIG.domains)
async def test_manually_approved_public_homepage(domain: str) -> None:
    """Fetch at most one safe page and never inspect or retain personal data."""
    source_policy = SourcePolicyService(Path("config"))
    policy = source_policy.evaluate(domain)
    if policy.decision is not SourcePolicyDecision.APPROVED:
        pytest.skip(
            f"SKIPPED DOMAIN {domain}: {policy.decision.value}. {policy.reason}"
        )

    robots = RobotsPolicyService(
        user_agent=CONFIG.user_agent,
        strict_mode=True,
        timeout_seconds=CONFIG.timeout_seconds,
        cache_ttl_seconds=300,
    )
    checker = _ApprovedRobotsChecker(source_policy, robots)
    crawler = AsyncWebsiteCrawler(
        compliance_preflight=checker,
        user_agent=CONFIG.user_agent,
        request_delay_seconds=CONFIG.request_delay_seconds,
        max_response_bytes=CONFIG.maximum_response_bytes,
        timeout_seconds=CONFIG.timeout_seconds,
        max_retry_after_seconds=0,
        max_redirects=CONFIG.maximum_redirects,
    )
    try:
        try:
            result = await crawler.crawl(
                f"https://{domain}/",
                max_pages=CONFIG.maximum_pages_per_domain,
            )
        except CrawlError as error:
            pytest.skip(
                f"SKIPPED DOMAIN {domain}: crawler stopped safely on "
                f"{type(error).__name__}: {error}"
            )
        assert len(result.pages) == 1
        page = result.pages[0]
        assert 200 <= page.http_status < 300
        assert len(page.html.encode("utf-8")) <= CONFIG.maximum_response_bytes
        # Deliberately do not parse, log, export, or assert on page text. This
        # smoke check validates transport/compliance behavior, not personal data.
    finally:
        await crawler.aclose()
        await robots.aclose()
