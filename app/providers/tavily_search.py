"""Asynchronous Tavily Search provider."""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.settings import get_settings
from app.models.search import SearchCandidate, SearchParameters
from app.providers.search import (
    SearchAuthenticationError,
    SearchAuthorizationError,
    SearchConfigurationError,
    SearchProviderError,
    SearchProviderUnavailableError,
    SearchRateLimitError,
    SearchResponseError,
    SearchTimeoutError,
)
from app.services.domain_normalization import InvalidDomainError, normalize_domain

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_RETRY_DELAY_SECONDS = 30.0


class _TavilyResult(BaseModel):
    """Minimal fields retained while normalizing a Tavily result."""

    model_config = ConfigDict(extra="ignore")

    title: str
    url: str


class _TavilyResponse(BaseModel):
    """Minimal Tavily response shape; content and scores are discarded."""

    model_config = ConfigDict(extra="ignore")

    results: list[_TavilyResult] = Field(default_factory=list)


def _retry_after_delay(response: httpx.Response) -> float | None:
    """Read an optional Retry-After hint without failing on malformed values."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


class TavilySearchProvider:
    """Discover transient candidates through the official Tavily Search API."""

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        settings = get_settings()
        configured_key = api_key or settings.tavily_api_key
        if configured_key is None:
            raise SearchConfigurationError(
                "TAVILY_API_KEY is required for Tavily Search"
            )
        key_value = (
            configured_key.get_secret_value()
            if isinstance(configured_key, SecretStr)
            else configured_key
        )
        if not key_value.strip():
            raise SearchConfigurationError("TAVILY_API_KEY must not be blank")
        self._api_key = SecretStr(key_value.strip())
        self._timeout_seconds: float = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.search_timeout_seconds
        )
        self._max_retries: int = (
            max_retries if max_retries is not None else settings.search_max_retries
        )
        self._backoff_seconds: float = (
            backoff_seconds
            if backoff_seconds is not None
            else settings.search_backoff_seconds
        )
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if self._max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self._max_retries > 10:
            raise ValueError("max_retries must not exceed ten")
        if self._backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")

        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds)
        )

    async def search(
        self,
        query: str,
        *,
        country: str = "US",
        language: str = "en",
        count: int = 10,
        offset: int = 0,
    ) -> list[SearchCandidate]:
        """Return normalized candidates without retaining response content.

        Tavily does not paginate or filter by country/language, so offsets
        beyond the first page return no further candidates.
        """
        parameters = SearchParameters(
            query=query,
            country=country,
            language=language,
            count=count,
            offset=offset,
        )
        if parameters.offset > 0:
            return []
        response = await self._request(parameters)
        try:
            payload = _TavilyResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise SearchResponseError(
                "Tavily Search returned an invalid response payload"
            ) from error

        candidates: list[SearchCandidate] = []
        for index, result in enumerate(payload.results):
            title = " ".join(result.title.split())
            if not title:
                continue
            try:
                candidate = SearchCandidate(
                    url=result.url,
                    title=title,
                    domain=normalize_domain(result.url),
                    rank=(parameters.offset * parameters.count) + index + 1,
                    provider="tavily",
                )
            except (InvalidDomainError, ValidationError):
                continue
            candidates.append(candidate)
        return candidates

    async def _request(self, parameters: SearchParameters) -> httpx.Response:
        """Execute one Tavily request with bounded exponential retries."""
        request_body = {
            "query": parameters.query,
            "max_results": parameters.count,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
        }

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    TAVILY_SEARCH_URL,
                    json=request_body,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as error:
                if attempt == self._max_retries:
                    raise SearchTimeoutError(
                        "Tavily Search timed out after retry attempts"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue
            except httpx.TransportError as error:
                if attempt == self._max_retries:
                    raise SearchProviderUnavailableError(
                        "Tavily Search transport failed after retry attempts"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code == 200:
                return response
            if response.status_code == 401:
                raise SearchAuthenticationError(
                    "Tavily Search rejected the configured API key"
                )
            if response.status_code == 403:
                raise SearchAuthorizationError(
                    "Tavily Search plan is not authorized for this endpoint"
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    delay = self._backoff_delay(attempt)
                    if response.status_code == 429:
                        retry_delay = _retry_after_delay(response)
                        if retry_delay is not None:
                            delay = max(delay, retry_delay)
                    await self._sleep(min(delay, _MAX_RETRY_DELAY_SECONDS))
                    continue
                if response.status_code == 429:
                    raise SearchRateLimitError(
                        "Tavily Search rate limit exceeded after retry attempts"
                    )
                raise SearchProviderUnavailableError(
                    "Tavily Search service failed after retry attempts"
                )
            raise SearchProviderError(
                f"Tavily Search returned HTTP {response.status_code}"
            )

        raise AssertionError("search retry loop exited unexpectedly")

    def _backoff_delay(self, attempt: int) -> float:
        """Return a bounded exponential delay for a zero-based retry attempt."""
        return min(
            self._backoff_seconds * (2.0**attempt),
            _MAX_RETRY_DELAY_SECONDS,
        )

    async def aclose(self) -> None:
        """Close the internally managed HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "TavilySearchProvider":
        """Enter an async provider context."""
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """Close provider-owned network resources."""
        await self.aclose()
