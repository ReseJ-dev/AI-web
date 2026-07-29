"""Search-provider abstraction."""

from typing import Protocol, runtime_checkable

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
