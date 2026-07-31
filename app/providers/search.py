"""Search-provider abstraction, shared errors, and configuration."""

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from app.core.settings import get_settings
from app.models.search import SearchCandidate


@runtime_checkable
class SearchProvider(Protocol):
    """Replaceable asynchronous candidate-discovery provider."""

    async def search(
        self,
        query: str,
        *,
        country: str = "US",
        language: str = "en",
        count: int = 10,
        offset: int = 0,
    ) -> list[SearchCandidate]:
        """Return normalized transient candidates for one result page."""
        ...


class SearchProviderError(RuntimeError):
    """Base error raised by search providers."""


class SearchConfigurationError(SearchProviderError):
    """Raised when search-provider configuration is incomplete."""


class SearchAuthenticationError(SearchProviderError):
    """Raised when the search API rejects the configured credentials."""


class SearchAuthorizationError(SearchProviderError):
    """Raised when the search subscription cannot access the endpoint."""


class SearchRateLimitError(SearchProviderError):
    """Raised after search rate-limit retries are exhausted."""


class SearchTimeoutError(SearchProviderError):
    """Raised after search timeouts are exhausted."""


class SearchProviderUnavailableError(SearchProviderError):
    """Raised after transient provider failures are exhausted."""


class SearchResponseError(SearchProviderError):
    """Raised when a provider returns an invalid success payload."""


def resolve_search_provider(
    factories: Mapping[str, Callable[[], SearchProvider]] | None = None,
) -> SearchProvider:
    """Instantiate the provider selected by ``SEARCH_PROVIDER``."""
    from app.providers.brave_search import BraveSearchProvider
    from app.providers.tavily_search import TavilySearchProvider

    provider_name = get_settings().search_provider.casefold().strip()
    available: dict[str, Callable[[], SearchProvider]] = {
        "brave": BraveSearchProvider,
        "tavily": TavilySearchProvider,
    }
    available.update(factories or {})
    factory = available.get(provider_name)
    if factory is None:
        raise SearchConfigurationError(
            f"Search provider {provider_name!r} is not configured"
        )
    return factory()
