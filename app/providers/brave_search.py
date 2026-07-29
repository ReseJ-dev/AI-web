"""Asynchronous Brave Web Search provider."""

import asyncio
import html
import re
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.settings import get_settings
from app.models.search import SearchCandidate, SearchParameters
from app.services.domain_normalization import InvalidDomainError, normalize_domain

BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_HTML_TAG = re.compile(r"<[^>]+>")
_MAX_RETRY_DELAY_SECONDS = 30.0


class SearchProviderError(RuntimeError):
    """Base error raised by search providers."""


class SearchConfigurationError(SearchProviderError):
    """Raised when search-provider configuration is incomplete."""


class SearchAuthenticationError(SearchProviderError):
    """Raised when the Brave API rejects the configured credentials."""


class SearchAuthorizationError(SearchProviderError):
    """Raised when the Brave subscription cannot access the endpoint."""


class SearchRateLimitError(SearchProviderError):
    """Raised after Brave rate-limit retries are exhausted."""


class SearchTimeoutError(SearchProviderError):
    """Raised after search timeouts are exhausted."""


class SearchProviderUnavailableError(SearchProviderError):
    """Raised after transient provider failures are exhausted."""


class SearchResponseError(SearchProviderError):
    """Raised when Brave returns an invalid success payload."""


class _BraveResult(BaseModel):
    """Minimal fields retained while normalizing a Brave result."""

    model_config = ConfigDict(extra="ignore")

    title: str
    url: str


class _BraveWebResults(BaseModel):
    """Minimal Brave web-results section."""

    model_config = ConfigDict(extra="ignore")

    results: list[_BraveResult] = Field(default_factory=list)


class _BraveResponse(BaseModel):
    """Minimal Brave response shape; snippets are intentionally omitted."""

    model_config = ConfigDict(extra="ignore")

    web: _BraveWebResults | None = None


def _normalize_title(title: str) -> str:
    """Convert Brave title markup to compact plain text."""
    return " ".join(html.unescape(_HTML_TAG.sub("", title)).split())


def _rate_limit_delay(response: httpx.Response) -> float | None:
    """Read Brave's first retry/reset delay without using monthly reset values."""
    value = response.headers.get("Retry-After") or response.headers.get(
        "X-RateLimit-Reset"
    )
    if value is None:
        return None
    first_value = value.split(",", maxsplit=1)[0].strip()
    try:
        return max(0.0, float(first_value))
    except ValueError:
        return None


class BraveSearchProvider:
    """Discover transient candidates through the official Brave Search API."""

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
        configured_key = api_key or settings.brave_search_api_key
        if configured_key is None:
            raise SearchConfigurationError(
                "BRAVE_SEARCH_API_KEY is required for Brave Search"
            )
        key_value = (
            configured_key.get_secret_value()
            if isinstance(configured_key, SecretStr)
            else configured_key
        )
        if not key_value.strip():
            raise SearchConfigurationError("BRAVE_SEARCH_API_KEY must not be blank")
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
        """Return normalized candidates without retaining response snippets."""
        parameters = SearchParameters(
            query=query,
            country=country,
            language=language,
            count=count,
            offset=offset,
        )
        response = await self._request(parameters)
        try:
            payload = _BraveResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise SearchResponseError(
                "Brave Search returned an invalid response payload"
            ) from error

        results = payload.web.results if payload.web is not None else []
        candidates: list[SearchCandidate] = []
        for index, result in enumerate(results):
            title = _normalize_title(result.title)
            if not title:
                continue
            try:
                candidate = SearchCandidate(
                    url=result.url,
                    title=title,
                    domain=normalize_domain(result.url),
                    rank=(parameters.offset * parameters.count) + index + 1,
                    provider="brave",
                )
            except (InvalidDomainError, ValidationError):
                continue
            candidates.append(candidate)
        return candidates

    async def _request(self, parameters: SearchParameters) -> httpx.Response:
        """Execute one Brave request with bounded exponential retries."""
        request_params: dict[str, str | int] = {
            "q": parameters.query,
            "country": parameters.country,
            "search_lang": parameters.language,
            "count": parameters.count,
            "offset": parameters.offset,
        }
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key.get_secret_value(),
        }

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    BRAVE_WEB_SEARCH_URL,
                    params=request_params,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as error:
                if attempt == self._max_retries:
                    raise SearchTimeoutError(
                        "Brave Search timed out after retry attempts"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue
            except httpx.TransportError as error:
                if attempt == self._max_retries:
                    raise SearchProviderUnavailableError(
                        "Brave Search transport failed after retry attempts"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code == 200:
                return response
            if response.status_code == 401:
                raise SearchAuthenticationError(
                    "Brave Search rejected the configured API key"
                )
            if response.status_code == 403:
                raise SearchAuthorizationError(
                    "Brave Search subscription is not authorized"
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    delay = self._backoff_delay(attempt)
                    if response.status_code == 429:
                        reset_delay = _rate_limit_delay(response)
                        if reset_delay is not None:
                            delay = max(delay, reset_delay)
                    await self._sleep(min(delay, _MAX_RETRY_DELAY_SECONDS))
                    continue
                if response.status_code == 429:
                    raise SearchRateLimitError(
                        "Brave Search rate limit exceeded after retry attempts"
                    )
                raise SearchProviderUnavailableError(
                    "Brave Search service failed after retry attempts"
                )
            raise SearchProviderError(
                f"Brave Search returned HTTP {response.status_code}"
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

    async def __aenter__(self) -> "BraveSearchProvider":
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
