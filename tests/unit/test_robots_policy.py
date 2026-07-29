"""Tests for RFC-style robots policy checks."""

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

import httpx

from app.models import PreflightDecision, RobotsPolicyRecord
from app.services import RobotsPolicyService

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_wildcards_anchors_specific_agents_and_cache() -> None:
    """Wildcard paths, terminal anchors, agent groups, and caching are applied."""
    robots_content = (FIXTURES / "robots_wildcards.txt").read_bytes()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.path == "/robots.txt"
        assert request.headers["User-Agent"] == "PortfolioResearchBot/1.0"
        return httpx.Response(200, request=request, content=robots_content)

    async def scenario() -> list[RobotsPolicyRecord]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = RobotsPolicyService(
                client=client,
                user_agent="PortfolioResearchBot/1.0",
                cache_ttl_seconds=60,
            )
            return [
                await service.check("https://example.com/bot-only"),
                await service.check("https://example.com/private/secret"),
                await service.check("https://example.com/private/public"),
                await service.check("https://example.com/private/public/child"),
                await service.check("https://example.com/brochure.pdf"),
                await service.check("https://example.com/brochure.pdf?download=1"),
            ]

    records = asyncio.run(scenario())

    assert [record.decision for record in records] == [
        PreflightDecision.APPROVED,
        PreflightDecision.REJECTED,
        PreflightDecision.APPROVED,
        PreflightDecision.REJECTED,
        PreflightDecision.REJECTED,
        PreflightDecision.APPROVED,
    ]
    assert requests == 1
    assert records[0].http_status == 200
    assert str(records[0].robots_url) == "https://example.com/robots.txt"
    assert records[0].requested_path == "/bot-only"
    assert records[0].response_hash == hashlib.sha256(robots_content).hexdigest()
    assert records[0].checked_at.utcoffset() == timedelta(0)


def test_unavailable_robots_file_allows_access() -> None:
    """A conventional not-found robots response has no blocking rules."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    async def scenario() -> RobotsPolicyRecord:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await RobotsPolicyService(client=client).check(
                "https://example.com/public"
            )

    record = asyncio.run(scenario())

    assert record.decision is PreflightDecision.APPROVED
    assert record.http_status == 404


def test_malformed_robots_file_requires_manual_review() -> None:
    """An unparseable robots document is treated as ambiguous."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text="this is not a robots directive",
        )

    async def scenario() -> RobotsPolicyRecord:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await RobotsPolicyService(client=client).check(
                "https://example.com/public"
            )

    record = asyncio.run(scenario())

    assert record.decision is PreflightDecision.MANUAL_REVIEW_REQUIRED
    assert "malformed" in record.reason


def test_network_failure_is_conservative_in_strict_mode() -> None:
    """Strict mode rejects unreachable robots; relaxed mode requests review."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    async def scenario(strict_mode: bool) -> RobotsPolicyRecord:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await RobotsPolicyService(
                client=client,
                strict_mode=strict_mode,
            ).check("https://example.com/public")

    strict = asyncio.run(scenario(True))
    relaxed = asyncio.run(scenario(False))

    assert strict.decision is PreflightDecision.REJECTED
    assert relaxed.decision is PreflightDecision.MANUAL_REVIEW_REQUIRED
    assert strict.http_status is None
    assert strict.response_hash is None
