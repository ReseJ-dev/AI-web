"""Deterministic in-memory search provider for tests and local development."""

from dataclasses import dataclass

from app.models.search import SearchCandidate, SearchParameters


@dataclass(frozen=True, slots=True)
class SearchCall:
    """Parameters recorded for a fake provider invocation."""

    query: str
    country: str
    language: str
    count: int
    offset: int


class FakeSearchProvider:
    """Return configured candidates without making network requests."""

    def __init__(self, candidates: list[SearchCandidate] | None = None) -> None:
        self._candidates = tuple(candidates or [])
        self.calls: list[SearchCall] = []

    async def search(
        self,
        query: str,
        *,
        country: str = "US",
        language: str = "en",
        count: int = 10,
        offset: int = 0,
    ) -> list[SearchCandidate]:
        """Return one page and record normalized invocation parameters."""
        parameters = SearchParameters(
            query=query,
            country=country,
            language=language,
            count=count,
            offset=offset,
        )
        self.calls.append(
            SearchCall(
                query=parameters.query,
                country=parameters.country,
                language=parameters.language,
                count=parameters.count,
                offset=parameters.offset,
            )
        )
        page_start = parameters.offset * parameters.count
        page_end = page_start + parameters.count
        return list(self._candidates[page_start:page_end])
