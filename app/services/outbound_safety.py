"""Shared SSRF guard for requests to website-controlled HTTP destinations."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]
_ALLOWED_PORTS = frozenset({80, 443})


class UnsafeOutboundUrlError(ValueError):
    """Raised when an outbound URL can reach a non-public network target."""


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_public_http_url(url: str) -> tuple[str, int]:
    """Validate URL syntax, credentials, port, and literal IP destinations."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as error:
        raise UnsafeOutboundUrlError("Outbound URL is malformed.") from error
    host = parsed.hostname
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeOutboundUrlError(
            "Outbound URL must be an absolute credential-free HTTP(S) URL."
        )
    normalized_host = host.rstrip(".").casefold()
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
        or port not in _ALLOWED_PORTS
    ):
        raise UnsafeOutboundUrlError(
            "Outbound URL targets a local host or unsupported port."
        )
    try:
        public = _is_public_address(normalized_host)
    except ValueError:
        public = True
    if not public:
        raise UnsafeOutboundUrlError(
            "Outbound URL resolves directly to a non-public IP address."
        )
    return normalized_host, port


async def _system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return list(dict.fromkeys(record[4][0] for record in records))


async def ensure_public_http_url(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> None:
    """Resolve a hostname immediately before a request and reject unsafe answers."""
    host, port = validate_public_http_url(url)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return
    resolve = resolver or _system_resolver
    try:
        addresses = await resolve(host, port)
    except OSError as error:
        raise UnsafeOutboundUrlError(
            "Outbound hostname could not be resolved safely."
        ) from error
    if not addresses or any(not _is_public_address(item) for item in addresses):
        raise UnsafeOutboundUrlError(
            "Outbound hostname resolved to a non-public IP address."
        )
