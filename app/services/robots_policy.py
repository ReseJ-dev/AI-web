"""RFC 9309-style robots.txt policy checks."""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from app.core.settings import get_settings
from app.models import PreflightDecision, RobotsPolicyRecord
from app.models.domain import utc_now

_MAX_ROBOTS_BYTES = 500 * 1_024
_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")
_PRODUCT_TOKEN = re.compile(r"^[^\s/]+")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _normalize_octets(value: str) -> str:
    """Normalize URI octets for RFC-style robots path comparison."""
    encoded = quote(
        value,
        safe="/:?&=+$,;@-._~!()*'%",
        encoding="utf-8",
        errors="strict",
    )

    def replace_escape(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        if character in _UNRESERVED:
            return character
        return f"%{match.group(1).upper()}"

    return _PERCENT_ESCAPE.sub(replace_escape, encoded)


@dataclass(frozen=True, slots=True)
class _RobotsRule:
    """One allow or disallow path pattern."""

    pattern: str
    allow: bool

    @property
    def specificity(self) -> int:
        """Return pattern length excluding wildcard and terminal anchor syntax."""
        pattern = self.pattern[:-1] if self.pattern.endswith("$") else self.pattern
        return len(pattern.replace("*", ""))

    def matches(self, requested_path: str) -> bool:
        """Match `*` and terminal `$` using case-sensitive path semantics."""
        normalized_pattern = _normalize_octets(self.pattern)
        anchored = normalized_pattern.endswith("$")
        if anchored:
            normalized_pattern = normalized_pattern[:-1]
        expression = re.escape(normalized_pattern).replace(r"\*", ".*")
        if anchored:
            expression = f"{expression}$"
        return re.match(f"^{expression}", requested_path) is not None


@dataclass(frozen=True, slots=True)
class _RobotsGroup:
    """One robots group with one or more user-agent names."""

    user_agents: tuple[str, ...]
    rules: tuple[_RobotsRule, ...]


@dataclass(frozen=True, slots=True)
class _RobotsRules:
    """Parsed robots groups and parser ambiguity state."""

    groups: tuple[_RobotsGroup, ...]
    ambiguous: bool

    @classmethod
    def parse(cls, content: str) -> "_RobotsRules":
        """Parse all usable groups while retaining malformed-line ambiguity."""
        groups: list[_RobotsGroup] = []
        current_agents: list[str] = []
        current_rules: list[_RobotsRule] = []
        ambiguous = False

        def finish_group() -> None:
            if current_agents:
                groups.append(
                    _RobotsGroup(
                        user_agents=tuple(current_agents),
                        rules=tuple(current_rules),
                    )
                )
            current_agents.clear()
            current_rules.clear()

        for raw_line in content.lstrip("\ufeff").splitlines():
            line = raw_line.split("#", maxsplit=1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                ambiguous = True
                continue
            field, value = (part.strip() for part in line.split(":", maxsplit=1))
            normalized_field = field.lower()

            if normalized_field == "user-agent":
                if current_agents and current_rules:
                    finish_group()
                if value:
                    current_agents.append(value.lower())
                else:
                    ambiguous = True
                continue

            if normalized_field not in {"allow", "disallow"}:
                continue
            if not current_agents:
                continue
            if not value:
                continue
            current_rules.append(
                _RobotsRule(
                    pattern=value,
                    allow=normalized_field == "allow",
                )
            )

        finish_group()
        if content.strip() and not groups:
            ambiguous = True
        return cls(groups=tuple(groups), ambiguous=ambiguous)

    def decide(
        self,
        user_agent: str,
        requested_path: str,
    ) -> tuple[PreflightDecision, str]:
        """Apply matching groups and most-specific allow/disallow precedence."""
        if requested_path.split("?", maxsplit=1)[0] == "/robots.txt":
            return (
                PreflightDecision.APPROVED,
                "The /robots.txt resource is implicitly allowed.",
            )
        product_match = _PRODUCT_TOKEN.match(user_agent.strip())
        product_token = (
            product_match.group(0).lower()
            if product_match is not None
            else user_agent.strip().lower()
        )
        exact_groups = [
            group for group in self.groups if product_token in group.user_agents
        ]
        applicable = exact_groups or [
            group for group in self.groups if "*" in group.user_agents
        ]
        normalized_path = _normalize_octets(requested_path)
        matching_rules = [
            rule
            for group in applicable
            for rule in group.rules
            if rule.matches(normalized_path)
        ]

        if matching_rules:
            winner = max(
                matching_rules,
                key=lambda rule: (rule.specificity, rule.allow),
            )
            if winner.allow:
                return (
                    PreflightDecision.APPROVED,
                    f"robots.txt allows {requested_path} via rule {winner.pattern!r}.",
                )
            return (
                PreflightDecision.REJECTED,
                f"robots.txt disallows {requested_path} via rule {winner.pattern!r}.",
            )

        if self.ambiguous:
            return (
                PreflightDecision.MANUAL_REVIEW_REQUIRED,
                "robots.txt contained malformed or unparseable directives and "
                "no decisive path rule matched.",
            )
        return (
            PreflightDecision.APPROVED,
            "No applicable robots.txt rule disallows the requested path.",
        )


@dataclass(frozen=True, slots=True)
class _CachedRobotsPolicy:
    """Fetched robots response and its derived policy state."""

    expires_at: float
    checked_at: datetime
    robots_url: str
    http_status: int | None
    response_hash: str | None
    rules: _RobotsRules | None
    fallback_decision: PreflightDecision | None
    fallback_reason: str | None


class RobotsPolicyService:
    """Fetch, cache, and evaluate robots.txt before content access."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        user_agent: str | None = None,
        cache_ttl_seconds: float | None = None,
        strict_mode: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self._user_agent = user_agent or settings.project_user_agent
        self._cache_ttl_seconds = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else settings.robots_cache_ttl_seconds
        )
        self._strict_mode = (
            strict_mode if strict_mode is not None else settings.robots_strict_mode
        )
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.compliance_http_timeout_seconds
        )
        if not self._user_agent.strip():
            raise ValueError("user_agent must not be blank")
        if not 0 <= self._cache_ttl_seconds <= 86_400:
            raise ValueError("cache_ttl_seconds must be between 0 and 86400")
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds)
        )
        self._owns_client = client is None
        self._cache: dict[str, _CachedRobotsPolicy] = {}
        self._cache_lock = asyncio.Lock()

    async def check(self, target_url: str) -> RobotsPolicyRecord:
        """Check a target path, fetching robots.txt first when not cached."""
        origin, robots_url, requested_path = self._target_parts(target_url)
        policy = await self._get_policy(origin, robots_url)

        if policy.rules is not None:
            decision, reason = policy.rules.decide(
                self._user_agent,
                requested_path,
            )
        else:
            if policy.fallback_decision is None or policy.fallback_reason is None:
                raise AssertionError("cached robots fallback is incomplete")
            decision = policy.fallback_decision
            reason = policy.fallback_reason

        return RobotsPolicyRecord(
            robots_url=policy.robots_url,
            http_status=policy.http_status,
            requested_path=requested_path,
            decision=decision,
            checked_at=policy.checked_at,
            response_hash=policy.response_hash,
            reason=reason,
        )

    async def _get_policy(
        self,
        origin: str,
        robots_url: str,
    ) -> _CachedRobotsPolicy:
        """Return a live cache entry or fetch a replacement."""
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(origin)
            if cached is not None and cached.expires_at > now:
                return cached

        replacement = await self._fetch_policy(robots_url)
        async with self._cache_lock:
            self._cache[origin] = replacement
        return replacement

    async def _fetch_policy(self, robots_url: str) -> _CachedRobotsPolicy:
        """Fetch robots.txt and convert HTTP outcomes into conservative policy."""
        checked_at = utc_now()
        expires_at = time.monotonic() + self._cache_ttl_seconds
        try:
            response = await self._client.get(
                robots_url,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": self._user_agent,
                },
                timeout=self._timeout_seconds,
                follow_redirects=True,
            )
        except httpx.TransportError:
            decision = (
                PreflightDecision.REJECTED
                if self._strict_mode
                else PreflightDecision.MANUAL_REVIEW_REQUIRED
            )
            mode = "strict-mode rejection" if self._strict_mode else "manual review"
            return _CachedRobotsPolicy(
                expires_at=expires_at,
                checked_at=checked_at,
                robots_url=robots_url,
                http_status=None,
                response_hash=None,
                rules=None,
                fallback_decision=decision,
                fallback_reason=(
                    f"robots.txt could not be reached; conservative {mode} applies."
                ),
            )

        response_hash = hashlib.sha256(response.content).hexdigest()
        status = response.status_code
        if 200 <= status < 300:
            if len(response.content) > _MAX_ROBOTS_BYTES:
                return _CachedRobotsPolicy(
                    expires_at=expires_at,
                    checked_at=checked_at,
                    robots_url=robots_url,
                    http_status=status,
                    response_hash=response_hash,
                    rules=None,
                    fallback_decision=PreflightDecision.MANUAL_REVIEW_REQUIRED,
                    fallback_reason=(
                        "robots.txt exceeds the supported 500 KiB parse limit; "
                        "manual review is required."
                    ),
                )
            return _CachedRobotsPolicy(
                expires_at=expires_at,
                checked_at=checked_at,
                robots_url=robots_url,
                http_status=status,
                response_hash=response_hash,
                rules=_RobotsRules.parse(response.text),
                fallback_decision=None,
                fallback_reason=None,
            )

        if status in {404, 410}:
            return _CachedRobotsPolicy(
                expires_at=expires_at,
                checked_at=checked_at,
                robots_url=robots_url,
                http_status=status,
                response_hash=response_hash,
                rules=_RobotsRules(groups=(), ambiguous=False),
                fallback_decision=None,
                fallback_reason=None,
            )

        if status >= 500:
            decision = (
                PreflightDecision.REJECTED
                if self._strict_mode
                else PreflightDecision.MANUAL_REVIEW_REQUIRED
            )
            reason = (
                f"robots.txt returned HTTP {status}; the site is unreachable "
                "for robots policy purposes."
            )
        else:
            decision = PreflightDecision.MANUAL_REVIEW_REQUIRED
            reason = (
                f"robots.txt returned ambiguous HTTP {status}; manual review "
                "is required."
            )
        return _CachedRobotsPolicy(
            expires_at=expires_at,
            checked_at=checked_at,
            robots_url=robots_url,
            http_status=status,
            response_hash=response_hash,
            rules=None,
            fallback_decision=decision,
            fallback_reason=reason,
        )

    @staticmethod
    def _target_parts(target_url: str) -> tuple[str, str, str]:
        """Return normalized origin, robots URL, and path-with-query."""
        parsed = urlsplit(target_url)
        if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("target_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("target_url must not contain credentials")

        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        default_port = (scheme == "http" and port == 80) or (
            scheme == "https" and port == 443
        )
        authority = hostname if port is None or default_port else f"{hostname}:{port}"
        origin = urlunsplit((scheme, authority, "", "", ""))
        robots_url = urlunsplit((scheme, authority, "/robots.txt", "", ""))
        requested_path = parsed.path or "/"
        if parsed.query:
            requested_path = f"{requested_path}?{parsed.query}"
        return origin, robots_url, requested_path

    async def clear_cache(self) -> None:
        """Drop cached robots responses, primarily for development and tests."""
        async with self._cache_lock:
            self._cache.clear()

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""
        if self._owns_client:
            await self._client.aclose()
