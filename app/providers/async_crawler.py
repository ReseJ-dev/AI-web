"""Compliance-aware asynchronous website crawler."""

import asyncio
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpx
import tldextract
from bs4 import BeautifulSoup

from app.core.settings import get_settings
from app.models.compliance import CompliancePreflightResult, PreflightDecision
from app.models.orchestration import CrawledPage, CrawlResult
from app.services.domain_normalization import InvalidDomainError, normalize_domain

Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_STATUSES = frozenset({401, 403, 429})
_ALLOWED_CONTENT_TYPES = frozenset({"application/xhtml+xml", "text/html", "text/plain"})
_RESTRICTED_SEGMENTS = frozenset(
    {
        "account",
        "accounts",
        "admin",
        "administrator",
        "api",
        "apis",
        "cart",
        "checkout",
        "customer",
        "customers",
        "graphql",
        "login",
        "logout",
        "search",
        "signin",
        "wp-admin",
        "wp-json",
    }
)
_RESTRICTED_PATH_TOKENS = frozenset(
    {
        "account",
        "admin",
        "api",
        "cart",
        "checkout",
        "graphql",
        "login",
        "search",
        "signin",
    }
)
_SEARCH_QUERY_KEYS = frozenset({"q", "query", "s", "search"})
_RESTRICTED_PHRASES = (
    "customer-data",
    "customer_data",
    "internal-search",
    "internal_search",
)
_BINARY_EXTENSIONS = frozenset(
    {
        ".7z",
        ".avi",
        ".bin",
        ".bmp",
        ".csv",
        ".doc",
        ".docx",
        ".dmg",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".rar",
        ".tar",
        ".webp",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)
_CHALLENGE_PATTERNS = (
    re.compile(r"\bcaptcha\b", re.IGNORECASE),
    re.compile(r"verify (?:that )?you are (?:a )?human", re.IGNORECASE),
    re.compile(r"checking your browser before accessing", re.IGNORECASE),
    re.compile(r"cf-chl-(?:captcha|managed|jschl)", re.IGNORECASE),
    re.compile(r"cloudflare ray id", re.IGNORECASE),
    re.compile(r"attention required[! ]*\|[ ]*cloudflare", re.IGNORECASE),
    re.compile(r"bot[- ]protection", re.IGNORECASE),
    re.compile(r"automated access (?:has been )?(?:blocked|denied)", re.IGNORECASE),
)
_PUBLIC_SUFFIX_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


class ComplianceChecker(Protocol):
    """Minimal CompliancePreflightService behavior required by the crawler."""

    async def check(self, target_url: str) -> CompliancePreflightResult:
        """Return whether the exact target may be fetched."""
        ...


class CrawlError(RuntimeError):
    """Base exception for safe crawler failures."""


class CrawlComplianceError(CrawlError):
    """A target or redirect was not approved for access."""


class CrawlRestrictedPathError(CrawlError):
    """A URL points to a prohibited or binary-like path."""


class CrawlResponseError(CrawlError):
    """A remote response cannot be safely consumed."""


class CrawlContentTypeError(CrawlResponseError):
    """A response is not HTML or plain text."""


class CrawlResponseTooLargeError(CrawlResponseError):
    """A response exceeds the configured byte limit."""


class CrawlBlockedError(CrawlResponseError):
    """A site explicitly blocked or challenged automated access."""


class CrawlRedirectError(CrawlResponseError):
    """A redirect chain is missing, invalid, unapproved, or too long."""


@dataclass(slots=True)
class _DomainState:
    """Shared concurrency and pacing state for one registrable domain."""

    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    delay_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_started_at: float | None = None


@dataclass(frozen=True, slots=True)
class _FetchedContent:
    """One validated response after any approved redirects."""

    url: str
    status_code: int
    text: str
    content_type: str


@dataclass(frozen=True, slots=True)
class _OpenResponse:
    """A streaming response holding its domain concurrency permit."""

    response: httpx.Response
    domain_state: _DomainState


class AsyncWebsiteCrawler:
    """Fetch a small, safe set of approved company pages without a browser."""

    def __init__(
        self,
        *,
        compliance_preflight: ComplianceChecker,
        client: httpx.AsyncClient | None = None,
        user_agent: str | None = None,
        request_delay_seconds: float | None = None,
        max_response_bytes: int | None = None,
        timeout_seconds: float | None = None,
        max_retry_after_seconds: float | None = None,
        max_redirects: int = 10,
        sleep: Sleep = asyncio.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        settings = get_settings()
        self._compliance_preflight = compliance_preflight
        self._user_agent = user_agent or settings.project_user_agent
        self._request_delay_seconds = (
            request_delay_seconds
            if request_delay_seconds is not None
            else settings.crawler_request_delay_seconds
        )
        self._max_response_bytes = (
            max_response_bytes
            if max_response_bytes is not None
            else settings.crawler_max_response_bytes
        )
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.crawler_timeout_seconds
        )
        self._max_retry_after_seconds = (
            max_retry_after_seconds
            if max_retry_after_seconds is not None
            else settings.crawler_max_retry_after_seconds
        )
        self._max_redirects = max_redirects
        self._sleep = sleep
        self._monotonic = monotonic
        if not self._user_agent.strip():
            raise ValueError("user_agent must not be blank")
        if not 0 <= self._request_delay_seconds <= 60:
            raise ValueError("request_delay_seconds must be between 0 and 60")
        if not 1_024 <= self._max_response_bytes <= 5_000_000:
            raise ValueError("max_response_bytes must be between 1024 and 5000000")
        if not 0 < self._timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0 and 120")
        if not 0 <= self._max_retry_after_seconds <= 3_600:
            raise ValueError("max_retry_after_seconds must be between 0 and 3600")
        if not 0 <= self._max_redirects <= 20:
            raise ValueError("max_redirects must be between 0 and 20")

        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
            ),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._domain_states: dict[str, _DomainState] = {}

    async def crawl(
        self,
        website_url: str,
        *,
        max_pages: int = 5,
    ) -> CrawlResult:
        """Crawl approved same-domain public pages with bounded GET requests."""
        if not 1 <= max_pages <= 20:
            raise ValueError("max_pages must be between 1 and 20")
        requested_url = self._normalize_url(website_url)
        initial_domain = self._domain_key(requested_url)
        queue: deque[str] = deque([requested_url])
        queued = {requested_url}
        visited: set[str] = set()
        pages: list[CrawledPage] = []
        warnings: list[str] = []
        canonical_url = requested_url

        while queue and len(pages) < max_pages:
            target = queue.popleft()
            if target in visited:
                continue
            visited.add(target)
            try:
                fetched = await self._fetch_with_redirects(target)
            except CrawlError as error:
                if not pages:
                    raise
                warnings.append(f"Stopped before fetching {target}: {error}")
                break
            if not pages:
                canonical_url = fetched.url
                initial_domain = self._domain_key(fetched.url)
            pages.append(
                CrawledPage(
                    url=fetched.url,
                    html=fetched.text,
                    http_status=fetched.status_code,
                )
            )
            if fetched.content_type not in {"text/html", "application/xhtml+xml"}:
                continue
            for link in self._discover_links(fetched.url, fetched.text):
                if len(queued) >= max_pages * 20:
                    break
                if (
                    self._domain_key(link) == initial_domain
                    and link not in queued
                    and link not in visited
                ):
                    queued.add(link)
                    queue.append(link)

        return CrawlResult(
            requested_url=requested_url,
            canonical_url=canonical_url,
            pages=pages,
            warnings=warnings,
        )

    async def _fetch_with_redirects(self, target_url: str) -> _FetchedContent:
        """Validate every hop and manually follow approved redirects."""
        current_url = target_url
        previous_domain = self._domain_key(current_url)
        for redirect_count in range(self._max_redirects + 1):
            self._validate_safe_path(current_url)
            preflight = await self._compliance_preflight.check(current_url)
            if preflight.decision is not PreflightDecision.APPROVED:
                qualifier = (
                    "cross-domain redirect"
                    if self._domain_key(current_url) != previous_domain
                    else "target"
                )
                raise CrawlComplianceError(
                    f"The {qualifier} was not approved: {preflight.reason}"
                )

            opened = await self._request(current_url)
            response = opened.response
            if response.status_code in _REDIRECT_STATUSES:
                if redirect_count >= self._max_redirects:
                    await self._close_response(opened)
                    raise CrawlRedirectError("The redirect limit was exceeded.")
                location = response.headers.get("Location")
                await self._close_response(opened)
                if not location:
                    raise CrawlRedirectError(
                        "The redirect response omitted a Location header."
                    )
                next_url = self._normalize_url(urljoin(current_url, location))
                previous_domain = self._domain_key(current_url)
                current_url = next_url
                continue
            return await self._consume_response(opened, current_url)
        raise CrawlRedirectError("The redirect limit was exceeded.")

    async def _request(self, url: str) -> _OpenResponse:
        """Issue one pooled, throttled GET without redirect following."""
        domain = self._domain_key(url)
        state = self._domain_states.setdefault(domain, _DomainState())
        await state.semaphore.acquire()
        try:
            async with state.delay_lock:
                now = self._monotonic()
                if state.last_started_at is not None:
                    delay = max(
                        0.0,
                        state.last_started_at + self._request_delay_seconds - now,
                    )
                    if delay:
                        await self._sleep(delay)
                state.last_started_at = self._monotonic()
            request = self._client.build_request(
                "GET",
                url,
                headers={
                    "Accept": ("text/html,application/xhtml+xml,text/plain;q=0.8"),
                    "User-Agent": self._user_agent,
                },
                timeout=self._timeout_seconds,
            )
            response = await self._client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            return _OpenResponse(response=response, domain_state=state)
        except httpx.TransportError as error:
            state.semaphore.release()
            raise CrawlResponseError(
                "The website request failed without a usable response."
            ) from error
        except BaseException:
            state.semaphore.release()
            raise

    async def _consume_response(
        self,
        opened: _OpenResponse,
        url: str,
    ) -> _FetchedContent:
        """Validate status, type, size, and bot challenges while streaming."""
        response = opened.response
        try:
            if response.status_code in _BLOCKED_STATUSES:
                if response.status_code == 429:
                    delay = self._retry_after_seconds(
                        response.headers.get("Retry-After")
                    )
                    if delay > 0:
                        async with opened.domain_state.delay_lock:
                            await self._sleep(delay)
                            opened.domain_state.last_started_at = self._monotonic()
                raise CrawlBlockedError(
                    f"HTTP {response.status_code} stopped the crawl."
                )
            if not 200 <= response.status_code < 300:
                raise CrawlResponseError(
                    f"HTTP {response.status_code} stopped the crawl."
                )

            content_type = self._content_type(response)
            content_length = self._content_length(response)
            if content_length is not None and content_length > self._max_response_bytes:
                raise CrawlResponseTooLargeError(
                    "The declared response size exceeds the configured limit."
                )

            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > self._max_response_bytes:
                    raise CrawlResponseTooLargeError(
                        "The streamed response exceeds the configured limit."
                    )
                content.extend(chunk)
            text = content.decode(response.encoding or "utf-8", errors="replace")
            if self._contains_bot_challenge(text):
                raise CrawlBlockedError(
                    "A CAPTCHA, Cloudflare challenge, or bot-protection page "
                    "stopped the crawl."
                )
            return _FetchedContent(
                url=url,
                status_code=response.status_code,
                text=text,
                content_type=content_type,
            )
        except httpx.HTTPError as error:
            raise CrawlResponseError(
                "The response stream failed before safe content was available."
            ) from error
        finally:
            await self._close_response(opened)

    @staticmethod
    async def _close_response(opened: _OpenResponse) -> None:
        """Close a stream and release exactly one domain permit."""
        try:
            await opened.response.aclose()
        finally:
            opened.domain_state.semaphore.release()

    def _content_type(self, response: httpx.Response) -> str:
        """Return an allowlisted media type or reject the response."""
        media_type = str(
            response.headers.get("Content-Type", "")
            .partition(";")[0]
            .strip()
            .casefold()
        )
        if media_type not in _ALLOWED_CONTENT_TYPES:
            raise CrawlContentTypeError(
                "Only HTML and plain-text responses may be crawled."
            )
        return media_type

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        """Parse a non-negative Content-Length value when supplied."""
        raw_value = response.headers.get("Content-Length")
        if raw_value is None:
            return None
        try:
            value = int(raw_value)
        except ValueError as error:
            raise CrawlResponseError(
                "The response supplied an invalid Content-Length header."
            ) from error
        if value < 0:
            raise CrawlResponseError(
                "The response supplied an invalid Content-Length header."
            )
        return value

    def _retry_after_seconds(self, value: str | None) -> float:
        """Parse Retry-After seconds or an HTTP date, with a safety cap."""
        if value is None:
            return 0.0
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        return min(max(0.0, delay), self._max_retry_after_seconds)

    @classmethod
    def _discover_links(cls, source_url: str, html: str) -> list[str]:
        """Extract only safe HTTP GET links; forms and scripts are ignored."""
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            try:
                link = cls._normalize_url(urljoin(source_url, href))
                cls._validate_safe_path(link)
            except (CrawlError, ValueError):
                continue
            if link not in links:
                links.append(link)
        return links

    @staticmethod
    def _contains_bot_challenge(text: str) -> bool:
        """Detect common explicit access challenges in bounded response text."""
        sample = text[:250_000]
        return any(pattern.search(sample) for pattern in _CHALLENGE_PATTERNS)

    @staticmethod
    def _normalize_url(value: str) -> str:
        """Normalize a public HTTP URL without credentials or fragments."""
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"}:
            raise ValueError("crawler URLs must use HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("crawler URLs must not contain credentials")
        try:
            host = normalize_domain(value)
        except InvalidDomainError as error:
            raise ValueError(f"crawler URL is malformed: {error}") from error
        port = parsed.port
        default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
            parsed.scheme.casefold() == "https" and port == 443
        )
        netloc = host if port is None or default_port else f"{host}:{port}"
        path = parsed.path or "/"
        return urlunsplit(
            (
                parsed.scheme.casefold(),
                netloc,
                path,
                parsed.query,
                "",
            )
        )

    @staticmethod
    def _domain_key(url: str) -> str:
        """Return an offline registrable domain for shared throttling."""
        host = normalize_domain(url)
        extracted = _PUBLIC_SUFFIX_EXTRACTOR(host)
        return extracted.top_domain_under_public_suffix or host

    @staticmethod
    def _validate_safe_path(url: str) -> None:
        """Reject restricted application areas and binary-looking targets."""
        parsed = urlsplit(url)
        path = parsed.path
        for _ in range(3):
            decoded_path = unquote(path)
            if decoded_path == path:
                break
            path = decoded_path
        path = path.casefold()
        segments = {segment.strip() for segment in path.split("/") if segment.strip()}
        path_tokens = {
            token
            for segment in segments
            for token in re.split(r"[^a-z0-9]+", segment)
            if token
        }
        query_keys = {
            key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        if (
            segments.intersection(_RESTRICTED_SEGMENTS)
            or path_tokens.intersection(_RESTRICTED_PATH_TOKENS)
            or query_keys.intersection(_SEARCH_QUERY_KEYS)
            or any(phrase in path for phrase in _RESTRICTED_PHRASES)
        ):
            raise CrawlRestrictedPathError(
                "The URL points to a prohibited account, transaction, "
                "customer-data, search, administration, or API path."
            )
        final_segment = path.rstrip("/").rsplit("/", maxsplit=1)[-1]
        if any(final_segment.endswith(extension) for extension in _BINARY_EXTENSIONS):
            raise CrawlRestrictedPathError(
                "The URL path resembles a binary or document download."
            )

    async def aclose(self) -> None:
        """Close the pooled client when it is owned by this crawler."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AsyncWebsiteCrawler":
        """Enter an owned crawler context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Release owned network resources."""
        await self.aclose()
