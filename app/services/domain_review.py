"""Read-only domain inspection and confirmed audit-store mutations."""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.core.settings import get_settings
from app.core.yaml_config import load_yaml_mapping
from app.models import (
    DomainInspection,
    DomainReviewRecord,
    PreflightDecision,
    RedirectObservation,
    ReviewDecision,
)
from app.services.domain_normalization import normalize_domain
from app.services.outbound_safety import (
    UnsafeOutboundUrlError,
    ensure_public_http_url,
    validate_public_http_url,
)
from app.services.page_selection import PageSelectionService
from app.services.robots_policy import RobotsPolicyService
from app.services.source_policy import (
    DomainRuleSet,
    SourcePolicyService,
)
from app.services.terms_policy import TermsPolicyScanner

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_INSPECTION_BYTES = 1_000_000
_MAX_REDIRECTS = 5
_MAX_TERMS_DOCUMENTS = 3


class DomainReviewError(RuntimeError):
    """Base error for auditable domain review operations."""


class DomainReviewEvidenceError(DomainReviewError):
    """Raised when required review evidence is unavailable."""


class DomainReviewConflictError(DomainReviewError):
    """Raised when a requested policy mutation cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class _BoundedReviewResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    oversized: bool

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class _ReviewFile(BaseModel):
    """Validated append-only domain review file."""

    model_config = ConfigDict(extra="forbid")

    reviews: list[DomainReviewRecord] = Field(default_factory=list)


class DomainReviewInspectionService:
    """Gather bounded public evidence without changing source policy."""

    def __init__(
        self,
        *,
        config_dir: Path | None = None,
        client: httpx.AsyncClient | None = None,
        robots_policy: RobotsPolicyService | None = None,
        terms_scanner: TermsPolicyScanner | None = None,
        page_selector: PageSelectionService | None = None,
        user_agent: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self._config_dir = config_dir or settings.source_policy_config_dir
        self._user_agent = (user_agent or settings.project_user_agent).strip()
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.compliance_http_timeout_seconds
        )
        if not self._user_agent:
            raise ValueError("user_agent must not be blank")
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            follow_redirects=False,
        )
        self._owns_robots = robots_policy is None
        self._robots = robots_policy or RobotsPolicyService(
            client=self._client,
            user_agent=self._user_agent,
            timeout_seconds=self._timeout_seconds,
        )
        self._terms = terms_scanner or TermsPolicyScanner()
        self._pages = page_selector or PageSelectionService()

    async def inspect(self, source: str) -> DomainInspection:
        """Inspect one normalized public domain without making a decision."""
        domain = normalize_domain(source)
        homepage_url = f"https://{domain}/"
        policy = SourcePolicyService(self._config_dir).evaluate(domain)
        robots = await self._robots.check(homepage_url)
        warnings: list[str] = []
        if robots.decision is not PreflightDecision.APPROVED:
            warnings.append(
                f"robots.txt requires caution: {robots.decision.value}. {robots.reason}"
            )

        if robots.decision is PreflightDecision.APPROVED:
            html, final_url, redirects, fetch_warnings = await self._fetch_homepage(
                homepage_url,
                domain,
            )
            warnings.extend(fetch_warnings)
        else:
            html = None
            final_url = homepage_url
            redirects = []
            warnings.append(
                "Homepage and terms pages were not fetched because robots.txt "
                "did not approve the homepage path."
            )
        terms_candidates: list[HttpUrl] = []
        risk_signals: list[str] = []
        proposed_paths: list[HttpUrl] = []

        if html is not None:
            try:
                terms_links = self._terms.discover_links(final_url, html)
            except ValueError:
                terms_links = []
                warnings.append(
                    "Terms links could not be parsed from the public homepage."
                )
            terms_candidates = [link.url for link in terms_links]
            try:
                ranked = self._pages.discover(final_url, html, limit=5)
                proposed_paths = [page.url for page in ranked]
            except ValueError:
                warnings.append(
                    "Proposed public paths could not be parsed from the homepage."
                )
            for terms_link in terms_links[:_MAX_TERMS_DOCUMENTS]:
                terms_url = str(terms_link.url)
                if not self._same_company_domain(domain, terms_url):
                    warnings.append(
                        f"Terms candidate {terms_url} is cross-domain and was "
                        "not fetched."
                    )
                    continue
                terms_robots = await self._robots.check(terms_url)
                if terms_robots.decision is not PreflightDecision.APPROVED:
                    warnings.append(
                        f"Terms candidate {terms_url} was not fetched because "
                        f"robots returned {terms_robots.decision.value}."
                    )
                    continue
                terms_html = await self._fetch_terms(terms_url, warnings)
                if terms_html is None:
                    continue
                try:
                    result = self._terms.scan_document(terms_url, terms_html)
                except ValueError:
                    warnings.append(f"Terms candidate {terms_url} could not be parsed.")
                    continue
                risk_signals.extend(
                    f"{signal}: {terms_url}" for signal in result.signals
                )
                if result.decision is PreflightDecision.MANUAL_REVIEW_REQUIRED:
                    warnings.append(result.reason)

        return DomainInspection(
            normalized_domain=domain,
            source_policy_status=policy.decision.value,
            source_policy_reason=policy.reason,
            robots=robots,
            terms_page_candidates=terms_candidates,
            automated_access_risk_signals=list(dict.fromkeys(risk_signals)),
            redirects=redirects,
            proposed_public_paths=proposed_paths,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def aclose(self) -> None:
        """Close only resources created by this inspection service."""
        if self._owns_robots:
            await self._robots.aclose()
        if self._owns_client:
            await self._client.aclose()

    async def _fetch_homepage(
        self,
        initial_url: str,
        reviewed_domain: str,
    ) -> tuple[str | None, str, list[RedirectObservation], list[str]]:
        current_url = initial_url
        redirects: list[RedirectObservation] = []
        warnings: list[str] = []
        for redirect_index in range(_MAX_REDIRECTS + 1):
            try:
                validate_public_http_url(current_url)
                if self._owns_client:
                    await ensure_public_http_url(current_url)
            except UnsafeOutboundUrlError:
                warnings.append(
                    f"Homepage request stopped because {current_url} did not "
                    "pass outbound network safety."
                )
                return None, current_url, redirects, warnings
            if redirect_index:
                redirect_robots = await self._robots.check(current_url)
                if redirect_robots.decision is not PreflightDecision.APPROVED:
                    warnings.append(
                        f"Redirect target {current_url} was not fetched because "
                        f"robots returned {redirect_robots.decision.value}."
                    )
                    return None, current_url, redirects, warnings
            try:
                response = await self._bounded_get(current_url)
            except httpx.TransportError:
                warnings.append(f"Homepage request failed for {current_url}.")
                return None, current_url, redirects, warnings

            location: str | None = None
            if response.status_code in _REDIRECT_STATUSES:
                raw_location = response.headers.get("Location")
                if raw_location:
                    candidate = urljoin(current_url, raw_location)
                    try:
                        location = str(HttpUrl(candidate))
                    except ValueError:
                        warnings.append(
                            f"Redirect from {current_url} had an invalid location."
                        )
            redirects.append(
                RedirectObservation(
                    url=current_url,
                    http_status=response.status_code,
                    location=location,
                )
            )

            if response.status_code not in _REDIRECT_STATUSES:
                if not 200 <= response.status_code < 300:
                    warnings.append(f"Homepage returned HTTP {response.status_code}.")
                    return None, current_url, redirects, warnings
                if response.oversized:
                    warnings.append("Homepage exceeded the 1 MB inspection limit.")
                    return None, current_url, redirects, warnings
                content_type = response.headers.get("Content-Type", "").casefold()
                if content_type and not (
                    content_type.startswith("text/html")
                    or content_type.startswith("application/xhtml+xml")
                ):
                    warnings.append("Homepage did not return an HTML content type.")
                    return None, current_url, redirects, warnings
                return response.text, current_url, redirects, warnings

            if location is None:
                warnings.append("Redirect inspection stopped without a location.")
                return None, current_url, redirects, warnings
            if not self._same_company_domain(reviewed_domain, location):
                warnings.append(
                    f"Cross-domain redirect to {location} was recorded but not "
                    "followed."
                )
                return None, current_url, redirects, warnings
            if redirect_index == _MAX_REDIRECTS:
                warnings.append("Homepage exceeded the redirect inspection limit.")
                return None, current_url, redirects, warnings
            current_url = location
        raise AssertionError("unreachable redirect loop")

    async def _fetch_terms(
        self,
        terms_url: str,
        warnings: list[str],
    ) -> str | None:
        try:
            validate_public_http_url(terms_url)
            if self._owns_client:
                await ensure_public_http_url(terms_url)
        except UnsafeOutboundUrlError:
            warnings.append(
                f"Terms candidate {terms_url} did not pass outbound network safety."
            )
            return None
        try:
            response = await self._bounded_get(terms_url)
        except httpx.TransportError:
            warnings.append(f"Terms candidate {terms_url} could not be fetched.")
            return None
        if not 200 <= response.status_code < 300:
            warnings.append(
                f"Terms candidate {terms_url} returned HTTP {response.status_code}."
            )
            return None
        if response.oversized:
            warnings.append(f"Terms candidate {terms_url} exceeded the 1 MB limit.")
            return None
        content_type = response.headers.get("Content-Type", "").casefold()
        if content_type and not (
            content_type.startswith("text/html")
            or content_type.startswith("application/xhtml+xml")
        ):
            warnings.append(
                f"Terms candidate {terms_url} did not return an HTML content type."
            )
            return None
        return response.text

    async def _bounded_get(self, url: str) -> _BoundedReviewResponse:
        """Stream one inert inspection response without buffering beyond 1 MB."""
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
            content = bytearray()
            oversized = False
            async for chunk in response.aiter_bytes():
                remaining = _MAX_INSPECTION_BYTES + 1 - len(content)
                content.extend(chunk[:remaining])
                if len(content) > _MAX_INSPECTION_BYTES:
                    oversized = True
                    break
            return _BoundedReviewResponse(
                status_code=response.status_code,
                headers={key.title(): value for key, value in response.headers.items()},
                content=bytes(content),
                oversized=oversized,
            )

    @staticmethod
    def _same_company_domain(reviewed_domain: str, url: str) -> bool:
        try:
            target = normalize_domain(url)
        except ValueError:
            return False
        return target.removeprefix("www.") == reviewed_domain.removeprefix("www.")


class DomainReviewStore:
    """Apply exact-domain rules and append a durable human-review record."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self._config_dir = config_dir or get_settings().source_policy_config_dir
        self._approved_path = self._config_dir / "approved_domains.yaml"
        self._blocked_path = self._config_dir / "blocked_domains.yaml"
        self._reviews_path = self._config_dir / "domain_reviews.yaml"

    def list_domains(self) -> list[tuple[str, str, str]]:
        """List each configured domain with effective status and rule origins."""
        approved, blocked = self._rules()
        policies = load_yaml_mapping(self._config_dir / "source_policies.yaml")
        candidate_data = policies.get("candidate_review", {})
        candidates = DomainRuleSet.model_validate(candidate_data)
        domains = set(
            (
                *approved.exact_domains,
                *approved.include_subdomains,
                *blocked.exact_domains,
                *blocked.include_subdomains,
                *candidates.exact_domains,
                *candidates.include_subdomains,
            )
        )
        evaluator = SourcePolicyService(self._config_dir)
        rows: list[tuple[str, str, str]] = []
        for domain in sorted(domains):
            origins: list[str] = []
            if domain in approved.exact_domains:
                origins.append("approved exact")
            if domain in approved.include_subdomains:
                origins.append("approved recursive")
            if domain in blocked.exact_domains:
                origins.append("blocked exact")
            if domain in blocked.include_subdomains:
                origins.append("blocked recursive")
            if (
                domain in candidates.exact_domains
                or domain in candidates.include_subdomains
            ):
                origins.append("candidate review")
            rows.append(
                (
                    domain,
                    evaluator.evaluate(domain).decision.value,
                    ", ".join(origins),
                )
            )
        return rows

    def record_decision(
        self,
        inspection: DomainInspection,
        *,
        decision: ReviewDecision,
        reviewer: str,
        review_note: str,
    ) -> DomainReviewRecord:
        """Apply one exact rule and append its complete evidence metadata."""
        domain = inspection.normalized_domain
        snapshot_hash = inspection.robots.response_hash
        if snapshot_hash is None:
            raise DomainReviewEvidenceError(
                "A robots.txt snapshot hash is required before changing policy."
            )
        reviewer = reviewer.strip()
        review_note = review_note.strip()
        if not reviewer or not review_note:
            raise DomainReviewEvidenceError(
                "Reviewer and review note must not be blank."
            )
        approved, blocked = self._rules()
        if decision is ReviewDecision.APPROVED:
            inherited = [
                rule
                for rule in blocked.include_subdomains
                if domain.endswith(f".{rule}") and domain != rule
            ]
            if inherited:
                raise DomainReviewConflictError(
                    f"{domain} remains covered by blocked recursive rule "
                    f"{inherited[0]}; review that parent rule instead."
                )
            approved = self._add_exact(approved, domain)
            blocked = self._remove_domain(blocked, domain)
        elif decision is ReviewDecision.REJECTED:
            blocked = self._add_exact(blocked, domain)
            approved = self._remove_domain(approved, domain)
        elif decision is ReviewDecision.REMOVED:
            changed = domain in {
                *approved.exact_domains,
                *approved.include_subdomains,
                *blocked.exact_domains,
                *blocked.include_subdomains,
            }
            if not changed:
                raise DomainReviewConflictError(
                    f"No explicit source-policy decision exists for {domain}."
                )
            approved = self._remove_domain(approved, domain)
            blocked = self._remove_domain(blocked, domain)
        else:
            raise AssertionError("unsupported review decision")

        record = DomainReviewRecord(
            domain=domain,
            decision=decision,
            reviewer=reviewer,
            review_note=review_note,
            robots_snapshot_hash=snapshot_hash,
            terms_page_url=(
                inspection.terms_page_candidates[0]
                if inspection.terms_page_candidates
                else None
            ),
        )
        review_file = self._review_file()
        review_file.reviews.append(record)
        documents = {
            self._approved_path: approved.model_dump(mode="json"),
            self._blocked_path: blocked.model_dump(mode="json"),
            self._reviews_path: review_file.model_dump(mode="json"),
        }
        self._atomic_write_documents(documents)
        return record

    def review_history(self) -> list[DomainReviewRecord]:
        """Return validated append-only audit history."""
        return list(self._review_file().reviews)

    def _rules(self) -> tuple[DomainRuleSet, DomainRuleSet]:
        return (
            DomainRuleSet.model_validate(load_yaml_mapping(self._approved_path)),
            DomainRuleSet.model_validate(load_yaml_mapping(self._blocked_path)),
        )

    def _review_file(self) -> _ReviewFile:
        if not self._reviews_path.exists():
            return _ReviewFile()
        return _ReviewFile.model_validate(load_yaml_mapping(self._reviews_path))

    @staticmethod
    def _add_exact(rules: DomainRuleSet, domain: str) -> DomainRuleSet:
        return rules.model_copy(
            update={"exact_domains": tuple(sorted({*rules.exact_domains, domain}))}
        )

    @staticmethod
    def _remove_domain(rules: DomainRuleSet, domain: str) -> DomainRuleSet:
        return rules.model_copy(
            update={
                "exact_domains": tuple(
                    item for item in rules.exact_domains if item != domain
                ),
                "include_subdomains": tuple(
                    item for item in rules.include_subdomains if item != domain
                ),
            }
        )

    @staticmethod
    def _atomic_write_documents(
        documents: dict[Path, dict[str, object]],
    ) -> None:
        staged: list[tuple[Path, Path]] = []
        try:
            for target, document in documents.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    yaml.safe_dump(
                        document,
                        temporary,
                        sort_keys=False,
                        allow_unicode=True,
                    )
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    staged.append((Path(temporary.name), target))
            for staged_path, target in staged:
                os.replace(staged_path, target)
        finally:
            for staged_path, _ in staged:
                staged_path.unlink(missing_ok=True)
