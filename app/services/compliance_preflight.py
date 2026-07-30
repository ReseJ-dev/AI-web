"""Combined domain, robots, and advisory terms compliance preflight."""

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.settings import get_settings
from app.models import (
    CompliancePreflightResult,
    PreflightDecision,
    RobotsPolicyRecord,
    TermsPolicyResult,
)
from app.services.outbound_safety import (
    UnsafeOutboundUrlError,
    ensure_public_http_url,
    validate_public_http_url,
)
from app.services.robots_policy import RobotsPolicyService
from app.services.source_policy import (
    SourcePolicyDecision,
    SourcePolicyResult,
    SourcePolicyService,
)
from app.services.terms_policy import TermsPolicyScanner

_LEGAL_DISCLAIMER = (
    "Terms scanning is a compliance risk signal only and is not legal advice."
)
_MAX_PREFLIGHT_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class _HtmlFetchResult:
    """Transient HTML fetch outcome used only during preflight."""

    html: str | None
    reason: str


def _preflight_decision(decision: SourcePolicyDecision) -> PreflightDecision:
    """Map source-policy decisions to the shared preflight decision enum."""
    return PreflightDecision(decision.value)


class CompliancePreflightService:
    """Run domain policy before robots and robots before any content fetch."""

    def __init__(
        self,
        *,
        domain_policy: SourcePolicyService | None = None,
        robots_policy: RobotsPolicyService | None = None,
        terms_scanner: TermsPolicyScanner | None = None,
        client: httpx.AsyncClient | None = None,
        user_agent: str | None = None,
        timeout_seconds: float | None = None,
        max_terms_documents: int | None = None,
    ) -> None:
        settings = get_settings()
        self._domain_policy = domain_policy or SourcePolicyService()
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds or settings.compliance_http_timeout_seconds
            )
        )
        self._owns_client = client is None
        self._user_agent = user_agent or settings.project_user_agent
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.compliance_http_timeout_seconds
        )
        self._max_terms_documents = (
            max_terms_documents
            if max_terms_documents is not None
            else settings.terms_max_documents
        )
        if not self._user_agent.strip():
            raise ValueError("user_agent must not be blank")
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not 1 <= self._max_terms_documents <= 10:
            raise ValueError("max_terms_documents must be between 1 and 10")

        self._robots_policy = robots_policy or RobotsPolicyService(
            client=self._client,
            user_agent=self._user_agent,
            timeout_seconds=self._timeout_seconds,
        )
        self._owns_robots_policy = robots_policy is None
        self._terms_scanner = terms_scanner or TermsPolicyScanner()
        self._approved_targets: set[str] = set()
        self._approved_origins: set[str] = set()
        self._prefetched_html: dict[str, str] = {}

    async def check(self, target_url: str) -> CompliancePreflightResult:
        """Return a combined decision without bypassing earlier policy blocks."""
        domain_result = self._domain_policy.evaluate(target_url)
        domain_decision = _preflight_decision(domain_result.decision)
        if domain_decision is not PreflightDecision.APPROVED:
            return self._result(
                target_url=target_url,
                domain_result=domain_result,
                decision=domain_decision,
                robots_checks=[],
                terms_results=[],
                reason=(
                    f"Preflight stopped at the domain policy. {domain_result.reason}"
                ),
            )

        target_robots = await self._robots_policy.check(target_url)
        robots_checks = [target_robots]
        if target_robots.decision is not PreflightDecision.APPROVED:
            return self._result(
                target_url=target_url,
                domain_result=domain_result,
                decision=target_robots.decision,
                robots_checks=robots_checks,
                terms_results=[],
                reason=(
                    "Preflight stopped before fetching content because "
                    f"{target_robots.reason}"
                ),
            )

        origin = self._origin(target_url)
        if target_url in self._approved_targets or origin in self._approved_origins:
            return self._result(
                target_url=target_url,
                domain_result=domain_result,
                decision=PreflightDecision.APPROVED,
                robots_checks=robots_checks,
                terms_results=[],
                reason=(
                    "Domain and target-path robots policies permit access; the "
                    "origin terms preflight was already approved. "
                    f"{_LEGAL_DISCLAIMER}"
                ),
            )

        target_page = await self._fetch_html(target_url)
        if target_page.html is None:
            return self._result(
                target_url=target_url,
                domain_result=domain_result,
                decision=PreflightDecision.MANUAL_REVIEW_REQUIRED,
                robots_checks=robots_checks,
                terms_results=[],
                reason=f"{target_page.reason} {_LEGAL_DISCLAIMER}",
            )

        terms_links = self._terms_scanner.discover_links(
            target_url,
            target_page.html,
        )
        terms_results: list[TermsPolicyResult] = []
        truncated = len(terms_links) > self._max_terms_documents

        for terms_link in terms_links[: self._max_terms_documents]:
            terms_url = str(terms_link.url)
            terms_domain = self._domain_policy.evaluate(terms_url)
            if terms_domain.decision is not SourcePolicyDecision.APPROVED:
                terms_results.append(
                    self._manual_terms_result(
                        terms_url,
                        "The linked terms document was not fetched because its "
                        f"domain policy is not approved: {terms_domain.reason}",
                    )
                )
                continue

            terms_robots = await self._robots_policy.check(terms_url)
            robots_checks.append(terms_robots)
            if terms_robots.decision is not PreflightDecision.APPROVED:
                terms_results.append(
                    self._manual_terms_result(
                        terms_url,
                        "The linked terms document was not fetched because "
                        f"{terms_robots.reason}",
                    )
                )
                continue

            terms_page = await self._fetch_html(terms_url)
            if terms_page.html is None:
                terms_results.append(
                    self._manual_terms_result(
                        terms_url,
                        terms_page.reason,
                    )
                )
                continue
            terms_results.append(
                self._terms_scanner.scan_document(
                    terms_url,
                    terms_page.html,
                )
            )

        has_manual_signal = any(
            result.decision is PreflightDecision.MANUAL_REVIEW_REQUIRED
            for result in terms_results
        )
        if truncated or has_manual_signal:
            detail = (
                "The terms link limit was reached before all documents could "
                "be reviewed. "
                if truncated
                else ""
            )
            return self._result(
                target_url=target_url,
                domain_result=domain_result,
                decision=PreflightDecision.MANUAL_REVIEW_REQUIRED,
                robots_checks=robots_checks,
                terms_results=terms_results,
                reason=(
                    f"{detail}One or more terms checks require human review. "
                    f"{_LEGAL_DISCLAIMER}"
                ),
            )

        if not terms_links:
            terms_reason = "No public terms-policy link was identified."
        else:
            terms_reason = "No automated-access restriction signal was identified."
        result = self._result(
            target_url=target_url,
            domain_result=domain_result,
            decision=PreflightDecision.APPROVED,
            robots_checks=robots_checks,
            terms_results=terms_results,
            reason=(
                "Domain and robots policies permit preflight access. "
                f"{terms_reason} {_LEGAL_DISCLAIMER}"
            ),
        )
        self._approved_targets.add(target_url)
        self._approved_origins.add(origin)
        self._prefetched_html[target_url] = target_page.html
        return result

    def take_prefetched_content(self, target_url: str) -> str | None:
        """Transfer one approved transient homepage body to the crawler."""
        return self._prefetched_html.pop(target_url, None)

    async def _fetch_html(self, url: str) -> _HtmlFetchResult:
        """Fetch one content page after its robots decision is approved."""
        try:
            validate_public_http_url(url)
            if self._owns_client:
                await ensure_public_http_url(url)
        except UnsafeOutboundUrlError:
            return _HtmlFetchResult(
                html=None,
                reason="The compliance page did not pass outbound network safety.",
            )
        try:
            async with self._client.stream(
                "GET",
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": self._user_agent,
                },
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    return _HtmlFetchResult(
                        html=None,
                        reason=(
                            "The compliance page returned ambiguous HTTP "
                            f"{response.status_code}."
                        ),
                    )
                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and not (
                    content_type.startswith("text/html")
                    or content_type.startswith("application/xhtml+xml")
                ):
                    return _HtmlFetchResult(
                        html=None,
                        reason=(
                            "The compliance page did not return an HTML content type."
                        ),
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_PREFLIGHT_BYTES:
                        return _HtmlFetchResult(
                            html=None,
                            reason="The compliance page exceeded the 1 MB limit.",
                        )
                    content.extend(chunk)
        except httpx.TransportError:
            return _HtmlFetchResult(
                html=None,
                reason="The compliance page could not be fetched.",
            )

        return _HtmlFetchResult(
            html=bytes(content).decode("utf-8", errors="replace"),
            reason="The compliance page was fetched.",
        )

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit(
            (parsed.scheme.casefold(), parsed.netloc.casefold(), "", "", "")
        )

    @staticmethod
    def _manual_terms_result(
        terms_url: str,
        reason: str,
    ) -> TermsPolicyResult:
        """Create a non-legal manual-review result for an unscanned terms page."""
        return TermsPolicyResult(
            terms_url=terms_url,
            decision=PreflightDecision.MANUAL_REVIEW_REQUIRED,
            signals=["terms_document_not_scanned"],
            reason=f"{reason} {_LEGAL_DISCLAIMER}",
        )

    @staticmethod
    def _result(
        *,
        target_url: str,
        domain_result: SourcePolicyResult,
        decision: PreflightDecision,
        robots_checks: list[RobotsPolicyRecord],
        terms_results: list[TermsPolicyResult],
        reason: str,
    ) -> CompliancePreflightResult:
        """Build a consistent immutable combined result."""
        return CompliancePreflightResult(
            target_url=target_url,
            normalized_domain=domain_result.normalized_domain,
            decision=decision,
            domain_reason=domain_result.reason,
            robots_checks=robots_checks,
            terms_results=terms_results,
            reason=reason,
        )

    async def aclose(self) -> None:
        """Close internally owned network resources."""
        if self._owns_robots_policy:
            await self._robots_policy.aclose()
        if self._owns_client:
            await self._client.aclose()
