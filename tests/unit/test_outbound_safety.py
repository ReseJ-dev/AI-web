"""Tests for the shared SSRF and outbound-port policy."""

import pytest

from app.services.outbound_safety import (
    UnsafeOutboundUrlError,
    ensure_public_http_url,
    validate_public_http_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "https://example.com:8443/",
        "http://localhost/",
        "http://service.local/",
    ],
)
def test_static_outbound_policy_rejects_private_targets(url: str) -> None:
    """Literal internal targets, local names, and arbitrary ports are denied."""
    with pytest.raises(UnsafeOutboundUrlError):
        validate_public_http_url(url)


@pytest.mark.anyio
async def test_dns_answers_are_checked_for_rebinding_targets() -> None:
    """A public-looking hostname cannot resolve to a private network address."""

    async def private_resolver(host: str, port: int) -> list[str]:
        assert host == "example.com"
        assert port == 443
        return ["10.0.0.8"]

    with pytest.raises(UnsafeOutboundUrlError, match="non-public"):
        await ensure_public_http_url(
            "https://example.com/",
            resolver=private_resolver,
        )
