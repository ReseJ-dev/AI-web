"""Fixture-based integration tests for combined compliance preflight."""

import asyncio
from pathlib import Path

import httpx
import yaml

from app.models import CompliancePreflightResult, PreflightDecision
from app.services import (
    CompliancePreflightService,
    RobotsPolicyService,
    SourcePolicyService,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _write_approved_policy(config_dir: Path) -> None:
    """Create a source policy that approves example.com and its subdomains."""
    config_dir.mkdir(exist_ok=True)
    (config_dir / "approved_domains.yaml").write_text(
        yaml.safe_dump(
            {
                "exact_domains": [],
                "include_subdomains": ["example.com"],
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "blocked_domains.yaml").write_text(
        yaml.safe_dump({"exact_domains": [], "include_subdomains": []}),
        encoding="utf-8",
    )
    (config_dir / "source_policies.yaml").write_text(
        yaml.safe_dump(
            {
                "candidate_review": {
                    "exact_domains": [],
                    "include_subdomains": [],
                },
                "unknown_domain_decision": "manual_review_required",
            }
        ),
        encoding="utf-8",
    )


def test_preflight_fetches_robots_before_fixture_content(
    tmp_path: Path,
) -> None:
    """Domain, robots, landing page, and terms checks run in safe order."""
    _write_approved_policy(tmp_path)
    homepage = (FIXTURES / "home_with_terms.html").read_text(encoding="utf-8")
    terms = (FIXTURES / "terms_prohibits_automation.html").read_text(encoding="utf-8")
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert request.headers["User-Agent"] == "PortfolioResearchBot/1.0"
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                request=request,
                text="User-agent: *\nAllow: /\n",
            )
        if request.url.path == "/":
            return httpx.Response(
                200,
                request=request,
                text=homepage,
                headers={"Content-Type": "text/html"},
            )
        if request.url.path == "/terms-of-use":
            return httpx.Response(
                200,
                request=request,
                text=terms,
                headers={"Content-Type": "text/html"},
            )
        raise AssertionError(f"Unexpected request path: {request.url.path}")

    async def scenario() -> CompliancePreflightResult:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            robots = RobotsPolicyService(
                client=client,
                user_agent="PortfolioResearchBot/1.0",
                cache_ttl_seconds=60,
            )
            service = CompliancePreflightService(
                domain_policy=SourcePolicyService(tmp_path),
                robots_policy=robots,
                client=client,
                user_agent="PortfolioResearchBot/1.0",
            )
            return await service.check("https://example.com/")

    result = asyncio.run(scenario())

    assert requested_paths == ["/robots.txt", "/", "/terms-of-use"]
    assert result.decision is PreflightDecision.MANUAL_REVIEW_REQUIRED
    assert [record.requested_path for record in result.robots_checks] == [
        "/",
        "/terms-of-use",
    ]
    assert result.terms_results[0].signals == ["potential_automated_access_prohibition"]
    assert "not legal advice" in result.reason


def test_blocked_domain_cannot_be_overridden_or_fetched() -> None:
    """A domain rejection stops preflight before robots or content requests."""
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise AssertionError("Blocked domains must not be fetched")

    async def scenario() -> CompliancePreflightResult:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = CompliancePreflightService(
                domain_policy=SourcePolicyService(Path("config")),
                client=client,
            )
            return await service.check("https://linkedin.com/company/example")

    result = asyncio.run(scenario())

    assert result.decision is PreflightDecision.REJECTED
    assert result.robots_checks == []
    assert result.terms_results == []
    assert requests == 0


def test_approved_origin_reuses_terms_preflight_without_refetching_child(
    tmp_path: Path,
) -> None:
    """Crawler path checks reuse the origin risk scan and only re-evaluate robots."""
    _write_approved_policy(tmp_path)
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/":
            return httpx.Response(
                200,
                text="<html><body>Public company homepage.</body></html>",
                headers={"Content-Type": "text/html"},
            )
        raise AssertionError("A child content page must be fetched only by the crawler")

    async def scenario() -> tuple[CompliancePreflightResult, CompliancePreflightResult]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = RobotsPolicyService(client=client, cache_ttl_seconds=60)
            service = CompliancePreflightService(
                domain_policy=SourcePolicyService(tmp_path),
                robots_policy=robots,
                client=client,
            )
            root = await service.check("https://example.com/")
            child = await service.check("https://example.com/about")
            return root, child

    root, child = asyncio.run(scenario())

    assert root.decision is PreflightDecision.APPROVED
    assert child.decision is PreflightDecision.APPROVED
    assert requested_paths == ["/robots.txt", "/"]
