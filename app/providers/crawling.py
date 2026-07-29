"""Replaceable website crawler abstraction."""

from typing import Protocol, runtime_checkable

from app.models.orchestration import CrawlResult


@runtime_checkable
class WebsiteCrawler(Protocol):
    """Fetch a bounded set of same-domain company pages."""

    async def crawl(self, website_url: str, *, max_pages: int = 5) -> CrawlResult:
        """Return raw HTML pages after compliance approval."""
        ...
