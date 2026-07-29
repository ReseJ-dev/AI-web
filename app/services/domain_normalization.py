"""Canonical domain normalization for source-policy decisions."""

import ipaddress
import re
from urllib.parse import SplitResult, urlsplit

import idna

_ASCII_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SUPPORTED_SCHEMES = frozenset({"http", "https"})


class InvalidDomainError(ValueError):
    """Raised when a source value cannot be normalized to a valid domain."""


def _split_source(source: str) -> SplitResult:
    """Parse a bare domain or HTTP URL into URL components."""
    candidate = source.strip()
    if not candidate:
        raise InvalidDomainError("domain must not be blank")

    try:
        if candidate.startswith("//") or "://" in candidate:
            parsed = urlsplit(candidate)
        else:
            parsed = urlsplit(f"//{candidate}")
        _ = parsed.port
    except ValueError as error:
        raise InvalidDomainError(f"invalid domain or port: {source!r}") from error

    if parsed.scheme and parsed.scheme.lower() not in _SUPPORTED_SCHEMES:
        raise InvalidDomainError(f"unsupported URL scheme: {parsed.scheme}")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidDomainError("credentials are not allowed in source domains")
    return parsed


def normalize_domain(source: str) -> str:
    """Return a lowercase ASCII/IDNA domain from a bare domain or HTTP URL."""
    if not isinstance(source, str):
        raise InvalidDomainError("domain must be a string")

    parsed = _split_source(source)
    hostname = parsed.hostname
    if hostname is None:
        raise InvalidDomainError(f"source does not contain a domain: {source!r}")

    hostname = hostname.rstrip(".")
    if not hostname:
        raise InvalidDomainError("domain must not be blank")

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise InvalidDomainError("IP addresses are not valid source domains")

    try:
        normalized = idna.encode(
            hostname,
            uts46=True,
            std3_rules=True,
        ).decode("ascii")
    except idna.IDNAError as error:
        raise InvalidDomainError(
            f"invalid internationalized domain: {source!r}"
        ) from error

    if len(normalized) > 253:
        raise InvalidDomainError("domain exceeds the maximum length")
    labels = normalized.split(".")
    if len(labels) < 2 or any(not _ASCII_LABEL.fullmatch(label) for label in labels):
        raise InvalidDomainError(f"malformed domain: {source!r}")
    return normalized.lower()
