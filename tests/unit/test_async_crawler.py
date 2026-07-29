"""Tests for the compliance-aware asynchronous website crawler."""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from app.models import CompliancePreflightResult, CrawlResult, PreflightDecision
from app.providers import (
    AsyncWebsiteCrawler,
    CrawlBlockedError,
    CrawlComplianceError,
    CrawlContentTypeError,
    CrawlResponseTooLargeError,
    CrawlRestrictedPathError,
)

Handler = Callable[[httpx.Request], httpx.Response]


class _CompliancePreflight:
    """Approve targets unless their normalized host is explicitly rejected."""

    def __init__(self, rejected_hosts: set[str] | None = None) -> None:
        self.rejected_hosts = rejected_hosts or set()
        self.calls: list[str] = []

    async def check(self, target_url: str) -> CompliancePreflightResult:
        self.calls.append(target_url)
        host = httpx.URL(target_url).host
        approved = host not in self.rejected_hosts
        return CompliancePreflightResult(
            target_url=target_url,
            normalized_domain=host,
            decision=(
                PreflightDecision.APPROVED if approved else PreflightDecision.REJECTED
            ),
            domain_reason="Fixture domain decision.",
            reason=(
                "Fixture approved the exact target."
                if approved
                else "Fixture rejected the redirect domain."
            ),
        )


def _run_crawl(
    handler: Handler,
    *,
    preflight: _CompliancePreflight | None = None,
    url: str = "https://example.com/",
    max_response_bytes: int = 2_000_000,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[CrawlResult, _CompliancePreflight]:
    """Run one crawler scenario using a pooled MockTransport client."""
    compliance = preflight or _CompliancePreflight()

    async def scenario() -> CrawlResult:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            crawler = AsyncWebsiteCrawler(
                compliance_preflight=compliance,
                client=client,
                request_delay_seconds=0,
                max_response_bytes=max_response_bytes,
                sleep=sleep or asyncio.sleep,
            )
            return await crawler.crawl(url, max_pages=5)

    return asyncio.run(scenario()), compliance


def test_redirects_are_manually_followed_and_every_hop_is_approved() -> None:
    """A same-domain redirect is checked before the redirected GET."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(
                301,
                request=request,
                headers={"Location": "/home"},
            )
        return httpx.Response(
            200,
            request=request,
            text="<html><main>Company home</main></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    result, preflight = _run_crawl(handler)

    assert str(result.canonical_url) == "https://example.com/home"
    assert [str(page.url) for page in result.pages] == ["https://example.com/home"]
    assert requested == ["https://example.com/", "https://example.com/home"]
    assert preflight.calls == requested


def test_unapproved_cross_domain_redirect_stops_before_network_access() -> None:
    """Cross-domain redirects never bypass the target domain's preflight."""
    requested: list[str] = []
    preflight = _CompliancePreflight({"blocked.example"})

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            request=request,
            headers={"Location": "https://blocked.example/home"},
        )

    with pytest.raises(CrawlComplianceError, match="cross-domain redirect"):
        _run_crawl(handler, preflight=preflight)

    assert requested == ["https://example.com/"]
    assert preflight.calls == [
        "https://example.com/",
        "https://blocked.example/home",
    ]


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/png", "application/octet-stream", ""],
)
def test_non_text_content_types_are_rejected(content_type: str) -> None:
    """Only explicit HTML, XHTML, and plain-text media types are accepted."""

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": content_type} if content_type else {}
        return httpx.Response(
            200,
            request=request,
            content=b"not safe crawler text",
            headers=headers,
        )

    with pytest.raises(CrawlContentTypeError):
        _run_crawl(handler)


def test_plain_text_is_accepted_without_link_discovery() -> None:
    """Public plain text is retained but never parsed as executable content."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text="Company information: https://example.com/ignored",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    result, _ = _run_crawl(handler)

    assert len(result.pages) == 1
    assert result.pages[0].html.startswith("Company information")


@pytest.mark.parametrize("declare_length", [False, True])
def test_maximum_response_size_is_enforced_while_streaming(
    declare_length: bool,
) -> None:
    """Both declared and undeclared oversized bodies stop at the byte limit."""

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": "text/html"}
        if declare_length:
            headers["Content-Length"] = "1025"
        return httpx.Response(
            200,
            request=request,
            content=b"x" * 1025,
            headers=headers,
        )

    with pytest.raises(CrawlResponseTooLargeError):
        _run_crawl(handler, max_response_bytes=1024)


def test_rate_limit_stops_and_respects_retry_after_without_retrying() -> None:
    """HTTP 429 waits for bounded Retry-After and makes no retry request."""
    requests = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "3", "Content-Type": "text/html"},
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(CrawlBlockedError, match="HTTP 429"):
        _run_crawl(handler, sleep=record_sleep)

    assert requests == 1
    assert delays == [3.0]


@pytest.mark.parametrize("status_code", [401, 403])
def test_authentication_and_access_blocks_stop_the_crawl(
    status_code: int,
) -> None:
    """The crawler never attempts to bypass authentication or access blocks."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            request=request,
            headers={"Content-Type": "text/html"},
        )

    with pytest.raises(CrawlBlockedError, match=f"HTTP {status_code}"):
        _run_crawl(handler)


@pytest.mark.parametrize(
    "challenge",
    [
        "<title>CAPTCHA</title><p>Complete the CAPTCHA to continue.</p>",
        "<title>Attention Required! | Cloudflare</title><p>Cloudflare Ray ID</p>",
        "<p>Please verify that you are human before accessing this website.</p>",
        "<div class='cf-chl-managed'>Checking your browser</div>",
        "<h1>Bot-protection</h1><p>Automated access denied.</p>",
    ],
)
def test_captcha_and_bot_protection_pages_stop_the_crawl(
    challenge: str,
) -> None:
    """Explicit technical challenges are not solved, evaded, or retried."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text=f"<html>{challenge}</html>",
            headers={"Content-Type": "text/html"},
        )

    with pytest.raises(CrawlBlockedError, match="CAPTCHA"):
        _run_crawl(handler)


def test_no_more_than_two_requests_run_concurrently_per_domain() -> None:
    """Apex and subdomain requests share the same two-response limit."""
    active = 0
    maximum_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(
            200,
            request=request,
            text="<html><main>Company</main></html>",
            headers={"Content-Type": "text/html"},
        )

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            crawler = AsyncWebsiteCrawler(
                compliance_preflight=_CompliancePreflight(),
                client=client,
                request_delay_seconds=0,
            )
            await asyncio.gather(
                crawler.crawl("https://example.com/one", max_pages=1),
                crawler.crawl("https://www.example.com/two", max_pages=1),
                crawler.crawl("https://docs.example.com/three", max_pages=1),
                crawler.crawl("https://shop.example.com/four", max_pages=1),
            )

    asyncio.run(scenario())

    assert maximum_active == 2


def test_configured_delay_spaces_request_starts_for_a_domain() -> None:
    """Subsequent same-domain page requests observe the configured delay."""
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        html = (
            "<html><a href='/about'>About</a></html>"
            if request.url.path == "/"
            else "<html><main>About</main></html>"
        )
        return httpx.Response(
            200,
            request=request,
            text=html,
            headers={"Content-Type": "text/html"},
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            crawler = AsyncWebsiteCrawler(
                compliance_preflight=_CompliancePreflight(),
                client=client,
                request_delay_seconds=1.5,
                sleep=record_sleep,
                monotonic=lambda: 0.0,
            )
            await crawler.crawl("https://example.com/", max_pages=2)

    asyncio.run(scenario())

    assert delays == [1.5]


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/account/profile",
        "/admin",
        "/checkout",
        "/cart",
        "/customers",
        "/customer-data/export",
        "/search",
        "/site-search/results",
        "/?q=private",
        "/api/v1/companies",
        "/%2561pi/v1/companies",
        "/wp-json/v2/pages",
        "/report.pdf",
        "/report.pdf/",
    ],
)
def test_restricted_and_binary_paths_are_never_requested(path: str) -> None:
    """Prohibited paths fail before compliance or network access."""
    requests = 0
    preflight = _CompliancePreflight()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise AssertionError("Restricted paths must never be requested")

    with pytest.raises(CrawlRestrictedPathError):
        _run_crawl(
            handler,
            preflight=preflight,
            url=f"https://example.com{path}",
        )

    assert requests == 0
    assert preflight.calls == []


def test_crawl_defaults_to_five_safe_same_domain_pages() -> None:
    """Link discovery ignores forms, scripts, external, and restricted links."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            links = "".join(
                f"<a href='/page-{index}'>Page {index}</a>" for index in range(1, 8)
            )
            html = f"""
            <html><body>
              {links}
              <a href="/login">Login</a>
              <a href="https://outside.example/about">Outside</a>
              <form action="/submit"><button>Submit</button></form>
              <script>fetch("/api/private")</script>
            </body></html>
            """
        else:
            html = "<html><main>Public company page</main></html>"
        return httpx.Response(
            200,
            request=request,
            text=html,
            headers={"Content-Type": "text/html"},
        )

    result, _ = _run_crawl(handler)

    assert len(result.pages) == 5
    assert all(page.url.host == "example.com" for page in result.pages)
    assert requested == [
        "https://example.com/",
        "https://example.com/page-1",
        "https://example.com/page-2",
        "https://example.com/page-3",
        "https://example.com/page-4",
    ]
