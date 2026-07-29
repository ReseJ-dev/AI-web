"""Tests for search-provider implementations."""

import asyncio
from typing import Any, cast

import httpx
import pytest
from structlog.typing import WrappedLogger

from app.core.logging import redact_secrets
from app.models import SearchCandidate
from app.providers import (
    BraveSearchProvider,
    FakeSearchProvider,
    SearchAuthenticationError,
    SearchAuthorizationError,
    SearchConfigurationError,
    SearchProvider,
    SearchProviderUnavailableError,
    SearchRateLimitError,
    SearchTimeoutError,
)


def _success_response(request: httpx.Request) -> httpx.Response:
    """Return a representative Brave response that includes discarded snippets."""
    return httpx.Response(
        200,
        request=request,
        json={
            "type": "search",
            "web": {
                "results": [
                    {
                        "title": "<strong>Example</strong> Agency",
                        "url": "https://www.example.com/services",
                        "description": "This search snippet must not be retained.",
                        "extra_snippets": ["Nor should alternate snippets."],
                    }
                ]
            },
        },
    )


def test_brave_provider_rejects_blank_api_key() -> None:
    """Whitespace-only credentials fail before any network request."""
    with pytest.raises(SearchConfigurationError, match="must not be blank"):
        BraveSearchProvider(api_key="   ")


def test_brave_provider_sends_parameters_and_discards_snippets() -> None:
    """Brave requests use documented fields and return normalized candidates."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "super-secret"
        assert request.url.params["q"] == "Shopify agency Netherlands"
        assert request.url.params["country"] == "NL"
        assert request.url.params["search_lang"] == "nl"
        assert request.url.params["count"] == "5"
        assert request.url.params["offset"] == "2"
        return _success_response(request)

    async def scenario() -> list[SearchCandidate]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = BraveSearchProvider(
                api_key="super-secret",
                client=client,
                max_retries=0,
            )
            return await provider.search(
                "  Shopify   agency Netherlands ",
                country="nl",
                language="NL",
                count=5,
                offset=2,
            )

    candidates = asyncio.run(scenario())

    assert candidates == [
        SearchCandidate(
            url="https://www.example.com/services",
            title="Example Agency",
            domain="www.example.com",
            rank=11,
            provider="brave",
        )
    ]
    serialized = candidates[0].model_dump(mode="json")
    assert "description" not in serialized
    assert "snippet" not in serialized


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, SearchAuthenticationError),
        (403, SearchAuthorizationError),
    ],
)
def test_brave_provider_handles_non_retryable_auth_errors(
    status_code: int,
    error_type: type[Exception],
) -> None:
    """Authentication and authorization failures fail immediately."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, request=request)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = BraveSearchProvider(
                api_key="secret-not-in-error",
                client=client,
                max_retries=3,
                backoff_seconds=0,
            )
            await provider.search("safe query")

    with pytest.raises(error_type) as captured:
        asyncio.run(scenario())

    assert attempts == 1
    assert "secret-not-in-error" not in str(captured.value)
    assert "safe query" not in str(captured.value)


def test_brave_provider_retries_429_using_backoff_and_reset_header() -> None:
    """Rate limits are retried and respect Brave's first reset interval."""
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                request=request,
                headers={"X-RateLimit-Reset": "2, 86400"},
            )
        return _success_response(request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async def scenario() -> list[SearchCandidate]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = BraveSearchProvider(
                api_key="secret",
                client=client,
                max_retries=1,
                backoff_seconds=0.25,
                sleep=record_sleep,
            )
            return await provider.search("Shopify agency")

    assert len(asyncio.run(scenario())) == 1
    assert attempts == 2
    assert delays == [2.0]


def test_brave_provider_uses_exponential_backoff_for_server_errors() -> None:
    """Transient 5xx responses use exponentially increasing delays."""
    responses = iter([500, 503, 200])
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(responses)
        if status == 200:
            return _success_response(request)
        return httpx.Response(status, request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = BraveSearchProvider(
                api_key="secret",
                client=client,
                max_retries=2,
                backoff_seconds=0.25,
                sleep=record_sleep,
            )
            await provider.search("Shopify agency")

    asyncio.run(scenario())

    assert delays == [0.25, 0.5]


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (429, SearchRateLimitError),
        (500, SearchProviderUnavailableError),
    ],
)
def test_brave_provider_reports_exhausted_transient_errors(
    status_code: int,
    error_type: type[Exception],
) -> None:
    """Exhausted rate-limit and server retries use explicit exception types."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    async def no_wait(delay: float) -> None:
        return None

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = BraveSearchProvider(
                api_key="secret",
                client=client,
                max_retries=1,
                backoff_seconds=0,
                sleep=no_wait,
            )
            await provider.search("Shopify agency")

    with pytest.raises(error_type):
        asyncio.run(scenario())


def test_brave_provider_retries_timeouts() -> None:
    """HTTP timeouts are bounded and surfaced after retry exhaustion."""
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = BraveSearchProvider(
                api_key="secret",
                client=client,
                max_retries=1,
                backoff_seconds=0.1,
                sleep=record_sleep,
            )
            await provider.search("Shopify agency")

    with pytest.raises(SearchTimeoutError):
        asyncio.run(scenario())

    assert attempts == 2
    assert delays == [0.1]


def test_fake_provider_satisfies_protocol_and_paginates_in_memory() -> None:
    """The fake is replaceable with the real provider and performs no I/O."""
    candidates = [
        SearchCandidate(
            url=f"https://example.com/{rank}",
            title=f"Candidate {rank}",
            domain="example.com",
            rank=rank,
            provider="fake",
        )
        for rank in range(1, 5)
    ]
    provider = FakeSearchProvider(candidates)

    assert isinstance(provider, SearchProvider)
    page = asyncio.run(
        provider.search(
            "  research   agencies ",
            country="nl",
            language="EN",
            count=2,
            offset=1,
        )
    )

    assert page == candidates[2:4]
    assert provider.calls[0].query == "research agencies"
    assert provider.calls[0].country == "NL"


def test_structured_logging_redacts_api_credentials() -> None:
    """Credential-like fields are redacted recursively before rendering."""
    event = {
        "event": "provider_configured",
        "BRAVE_SEARCH_API_KEY": "top-secret",
        "other_api_key": "also-secret",
        "headers": {"X-Subscription-Token": "top-secret"},
    }

    redacted = redact_secrets(
        cast(WrappedLogger, cast(Any, object())),
        "info",
        event,
    )

    assert redacted["BRAVE_SEARCH_API_KEY"] == "[REDACTED]"
    assert redacted["other_api_key"] == "[REDACTED]"
    assert redacted["headers"] == {"X-Subscription-Token": "[REDACTED]"}
