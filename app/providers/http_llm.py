"""Environment-configurable JSON-over-HTTP structured LLM provider."""

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from app.core.settings import get_settings
from app.models.extraction import LLMExtractionRequest
from app.providers.llm import (
    LLMProviderConfigurationError,
    LLMProviderError,
    LLMProviderResponseError,
)

Sleep = Callable[[float], Awaitable[None]]
_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ConfigurableHttpLLMProvider:
    """POST a vendor-neutral structured request to a configured JSON gateway.

    The endpoint receives ``model``, ``instructions``, ``requested_fields``,
    ``pages``, and ``response_schema`` and must return the model JSON directly.
    """

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        settings = get_settings()
        self._api_url = (api_url or settings.llm_api_url or "").strip()
        configured_key = (
            settings.llm_api_key.get_secret_value()
            if settings.llm_api_key is not None
            else None
        )
        self._api_key = api_key if api_key is not None else configured_key
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.llm_timeout_seconds
        )
        self._max_retries = (
            max_retries if max_retries is not None else settings.llm_http_max_retries
        )
        self._backoff_seconds = (
            backoff_seconds
            if backoff_seconds is not None
            else settings.llm_retry_backoff_seconds
        )
        self._sleep = sleep
        if not self._api_url:
            raise LLMProviderConfigurationError(
                "LLM_API_URL is required for the HTTP LLM provider"
            )
        parsed_url = httpx.URL(self._api_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            raise LLMProviderConfigurationError(
                "LLM_API_URL must be an absolute HTTP(S) URL"
            )
        if not 0 < self._timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0 and 120")
        if not 0 <= self._max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if not 0 <= self._backoff_seconds <= 30:
            raise ValueError("backoff_seconds must be between 0 and 30")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
        self._owns_client = client is None

    async def generate_structured(
        self,
        request: LLMExtractionRequest,
        *,
        response_schema: dict[str, JsonValue],
    ) -> JsonValue:
        """Return validated JSON from the configured structured-generation gateway."""
        payload = {
            **request.model_dump(mode="json"),
            "response_schema": deepcopy(response_schema),
        }
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._api_url,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            except httpx.TransportError as error:
                if attempt >= self._max_retries:
                    raise LLMProviderError(
                        "The configured LLM endpoint could not be reached."
                    ) from error
                await self._sleep(self._backoff_seconds * (2**attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise LLMProviderError(
                        f"The configured LLM endpoint returned HTTP "
                        f"{response.status_code}."
                    )
                await self._sleep(self._backoff_seconds * (2**attempt))
                continue
            if not 200 <= response.status_code < 300:
                raise LLMProviderError(
                    f"The configured LLM endpoint returned HTTP {response.status_code}."
                )
            try:
                return _JSON_ADAPTER.validate_python(response.json())
            except (ValueError, ValidationError) as error:
                raise LLMProviderResponseError(
                    "The configured LLM endpoint returned invalid JSON."
                ) from error
        raise LLMProviderError("The configured LLM endpoint failed.")

    async def aclose(self) -> None:
        """Close the pooled client when this provider created it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "ConfigurableHttpLLMProvider":
        """Enter a provider context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Release owned HTTP resources."""
        await self.aclose()
