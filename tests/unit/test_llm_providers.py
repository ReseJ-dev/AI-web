"""Tests for configurable structured-generation providers."""

import asyncio
import json
from typing import cast

import httpx
import pytest
from pydantic import JsonValue

from app.models import LLMExtractionRequest
from app.providers import (
    ConfigurableHttpLLMProvider,
    LLMProvider,
    LLMProviderConfigurationError,
    LLMProviderResponseError,
)


def _request() -> LLMExtractionRequest:
    return LLMExtractionRequest(
        model="company-model",
        requested_fields=["company_name"],
        pages=[
            {
                "source_url": "https://example.com/",
                "cleaned_text": "Example Commerce builds Shopify stores.",
            }
        ],
        instructions="Return strict supported JSON.",
    )


def test_http_provider_posts_only_structured_request_and_schema() -> None:
    """The configured gateway receives clean content and no website metadata."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = cast(dict[str, object], json.loads(request.content))
        assert payload["model"] == "company-model"
        assert payload["pages"] == [
            {
                "source_url": "https://example.com/",
                "cleaned_text": "Example Commerce builds Shopify stores.",
            }
        ]
        assert "response_schema" in payload
        assert "organization_data" not in request.content.decode()
        assert "test-secret" not in request.content.decode()
        return httpx.Response(
            200,
            request=request,
            json={"fields": []},
        )

    async def scenario() -> JsonValue:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = ConfigurableHttpLLMProvider(
                api_url="https://llm-gateway.example/extract",
                api_key="test-secret",
                client=client,
                max_retries=0,
            )
            assert isinstance(provider, LLMProvider)
            return await provider.generate_structured(
                _request(),
                response_schema={"type": "object"},
            )

    assert asyncio.run(scenario()) == {"fields": []}


def test_http_provider_retries_transient_failures() -> None:
    """Transient endpoint failures use bounded exponential backoff."""
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, request=request, json={"fields": []})

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = ConfigurableHttpLLMProvider(
                api_url="https://llm-gateway.example/extract",
                client=client,
                max_retries=1,
                backoff_seconds=0.25,
                sleep=record_sleep,
            )
            await provider.generate_structured(
                _request(),
                response_schema={"type": "object"},
            )

    asyncio.run(scenario())

    assert attempts == 2
    assert delays == [0.25]


def test_http_provider_rejects_invalid_json() -> None:
    """A successful HTTP response must still contain valid JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = ConfigurableHttpLLMProvider(
                api_url="https://llm-gateway.example/extract",
                client=client,
                max_retries=0,
            )
            await provider.generate_structured(
                _request(),
                response_schema={"type": "object"},
            )

    with pytest.raises(LLMProviderResponseError, match="invalid JSON"):
        asyncio.run(scenario())


def test_http_provider_requires_a_configured_endpoint() -> None:
    """The environment-backed adapter fails before making an ambiguous request."""
    with pytest.raises(LLMProviderConfigurationError, match="absolute HTTP"):
        ConfigurableHttpLLMProvider(api_url="ftp://llm.example/extract")
