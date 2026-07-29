"""Evidence-preserving organization enrichment through Wikidata Query Service."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.settings import get_settings
from app.models.domain import CompanyRecord, Evidence, ExtractedField
from app.models.entity_resolution import (
    OfficialIdentifier,
    OfficialIdentifierSource,
)
from app.models.orchestration import EnrichmentResult

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
_WIKIDATA_ENTITY = re.compile(r"^https?://www\.wikidata\.org/entity/(Q\d+)$")


class WikidataProviderError(RuntimeError):
    """Base error raised by the Wikidata provider."""


class WikidataConfigurationError(WikidataProviderError):
    """Raised when Wikidata enrichment is not enabled."""


class WikidataRateLimitError(WikidataProviderError):
    """Raised after Wikidata throttling retries are exhausted."""


class WikidataUnavailableError(WikidataProviderError):
    """Raised after transient Wikidata failures are exhausted."""


class WikidataResponseError(WikidataProviderError):
    """Raised when the SPARQL service returns an invalid payload."""


class _BindingValue(BaseModel):
    """One SPARQL binding value."""

    model_config = ConfigDict(extra="ignore")

    value: str


class _WikidataBinding(BaseModel):
    """Allowlisted organization values from one SPARQL result row."""

    model_config = ConfigDict(extra="ignore")

    item: _BindingValue
    itemLabel: _BindingValue
    website: _BindingValue | None = None
    countryLabel: _BindingValue | None = None
    headquartersLabel: _BindingValue | None = None
    industryLabel: _BindingValue | None = None


class _WikidataResults(BaseModel):
    """SPARQL bindings collection."""

    model_config = ConfigDict(extra="ignore")

    bindings: list[_WikidataBinding]


class _WikidataResponse(BaseModel):
    """Minimal SPARQL JSON response."""

    model_config = ConfigDict(extra="ignore")

    results: _WikidataResults


@dataclass(slots=True)
class _OrganizationCandidate:
    """Aggregated rows for one Wikidata organization."""

    qid: str
    label: str
    websites: set[str] = field(default_factory=set)
    countries: set[str] = field(default_factory=set)
    headquarters: set[str] = field(default_factory=set)
    industries: set[str] = field(default_factory=set)


def _sparql_string(value: str) -> str:
    """Escape user-derived text as one SPARQL string literal."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _normalized_website(value: str) -> tuple[str, str] | None:
    """Normalize a website to host and meaningful path for validation."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or parsed.hostname is None:
        return None
    host = parsed.hostname.casefold().removeprefix("www.")
    if port is not None and port not in {80, 443}:
        host = f"{host}:{port}"
    return host, parsed.path.rstrip("/")


def _same_website(first: str, second: str) -> bool:
    """Validate two official URLs by host and compatible root path."""
    left = _normalized_website(first)
    right = _normalized_website(second)
    if left is None or right is None or left[0] != right[0]:
        return False
    return left[1] == right[1] or not left[1] or not right[1]


def _field_value(company: CompanyRecord, name: str) -> ExtractedField | None:
    """Return the first evidence-bearing field with a normalized name."""
    return next(
        (
            item
            for item in company.extracted_fields
            if item.name == name and item.evidence
        ),
        None,
    )


def _normalized_text(value: object) -> set[str]:
    """Normalize one string or string list for conflict comparisons."""
    candidates = value if isinstance(value, list) else [value]
    return {
        " ".join(item.casefold().split())
        for item in candidates
        if isinstance(item, str) and item.strip()
    }


class WikidataProvider:
    """Corroborate a company through the official Wikidata SPARQL endpoint."""

    provider_name = "wikidata"

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        user_agent: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        settings = get_settings()
        if not (enabled if enabled is not None else settings.wikidata_enabled):
            raise WikidataConfigurationError(
                "Wikidata enrichment requires WIKIDATA_ENABLED=true"
            )
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.wikidata_timeout_seconds
        )
        self._max_retries = (
            max_retries if max_retries is not None else settings.wikidata_max_retries
        )
        self._backoff_seconds = (
            backoff_seconds
            if backoff_seconds is not None
            else settings.wikidata_backoff_seconds
        )
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not 0 <= self._max_retries <= 10:
            raise ValueError("max_retries must be between zero and ten")
        if self._backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")
        configured_user_agent = (user_agent or settings.project_user_agent).strip()
        if not configured_user_agent:
            raise ValueError("user_agent must not be blank")
        self._headers = {
            "Accept": "application/sparql-results+json",
            "User-Agent": configured_user_agent,
            "Api-User-Agent": configured_user_agent,
        }
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
        )

    async def enrich(self, company: CompanyRecord) -> EnrichmentResult:
        """Add corroborated Wikidata values without changing website facts."""
        if company.website_url is None:
            return EnrichmentResult(
                company=company,
                warnings=["Wikidata identity validation requires an official website."],
            )
        response = await self._request(self._query(company.name))
        try:
            payload = _WikidataResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise WikidataResponseError(
                "Wikidata returned an invalid SPARQL response"
            ) from error
        candidates = self._candidates(payload)
        matches = [
            candidate
            for candidate in candidates
            if any(
                _same_website(website, str(company.website_url))
                for website in candidate.websites
            )
        ]
        if not matches:
            return EnrichmentResult(
                company=company,
                warnings=[
                    "Wikidata results did not contain an organization whose "
                    "official website matches the independently verified site."
                ],
            )
        if len(matches) > 1:
            return EnrichmentResult(
                company=company,
                warnings=[
                    "Multiple Wikidata organizations matched the official "
                    "website; enrichment requires manual review."
                ],
            )
        match = matches[0]
        fields, warnings = self._enrichment_fields(company, match)
        enriched = company.model_copy(
            update={"extracted_fields": [*company.extracted_fields, *fields]},
            deep=True,
        )
        return EnrichmentResult(
            company=enriched,
            official_identifiers=[
                OfficialIdentifier(
                    source=OfficialIdentifierSource.WIKIDATA,
                    value=match.qid,
                )
            ],
            warnings=warnings,
        )

    def _query(self, name: str) -> str:
        """Build a bounded exact-label query; no fuzzy SPARQL search is used."""
        literal = _sparql_string(name.strip())
        return f"""
SELECT ?item ?itemLabel ?website ?countryLabel ?headquartersLabel ?industryLabel
WHERE {{
  ?item (rdfs:label|skos:altLabel) ?candidateName .
  FILTER(LCASE(STR(?candidateName)) = LCASE({literal}))
  ?item wdt:P856 ?website .
  OPTIONAL {{ ?item wdt:P17 ?country . }}
  OPTIONAL {{ ?item wdt:P159 ?headquarters . }}
  OPTIONAL {{ ?item wdt:P452 ?industry . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 50
""".strip()

    @staticmethod
    def _candidates(payload: _WikidataResponse) -> list[_OrganizationCandidate]:
        """Aggregate the SPARQL cross-product without retaining raw bindings."""
        grouped: dict[str, _OrganizationCandidate] = {}
        for row in payload.results.bindings:
            identifier_match = _WIKIDATA_ENTITY.fullmatch(row.item.value)
            if identifier_match is None:
                continue
            qid = identifier_match.group(1)
            candidate = grouped.setdefault(
                qid,
                _OrganizationCandidate(qid=qid, label=row.itemLabel.value.strip()),
            )
            values = (
                (candidate.websites, row.website),
                (candidate.countries, row.countryLabel),
                (candidate.headquarters, row.headquartersLabel),
                (candidate.industries, row.industryLabel),
            )
            for target, binding in values:
                if binding is not None and binding.value.strip():
                    target.add(binding.value.strip())
        return list(grouped.values())

    def _enrichment_fields(
        self,
        company: CompanyRecord,
        match: _OrganizationCandidate,
    ) -> tuple[list[ExtractedField], list[str]]:
        """Build additions, preserving occupied official-site fields."""
        evidence_url = f"https://www.wikidata.org/wiki/{match.qid}"
        verified_website = next(
            website
            for website in sorted(match.websites)
            if _same_website(website, str(company.website_url))
        )
        values: dict[str, object] = {
            "wikidata_organization": {"id": match.qid, "label": match.label},
            "wikidata_official_website": verified_website,
        }
        if match.countries:
            countries = sorted(match.countries)
            values["country" if len(countries) == 1 else "wikidata_countries"] = (
                countries[0] if len(countries) == 1 else countries
            )
        if match.headquarters:
            headquarters = sorted(match.headquarters)
            values["headquarters_location"] = (
                headquarters[0] if len(headquarters) == 1 else headquarters
            )
        if match.industries:
            values["industry"] = sorted(match.industries)

        additions: list[ExtractedField] = []
        warnings: list[str] = []
        occupied_names = {field.name for field in company.extracted_fields}
        for name, value in values.items():
            existing = _field_value(company, name)
            if existing is not None:
                if _normalized_text(existing.value) != _normalized_text(value):
                    warnings.append(
                        f"Wikidata {name!r} contradicts stronger website evidence; "
                        "the website value was retained."
                    )
                continue
            if name in occupied_names:
                continue
            additions.append(
                ExtractedField(
                    name=name,
                    value=value,
                    confidence=0.85,
                    evidence=[
                        Evidence(
                            urls=[evidence_url],
                            excerpt=f"Wikidata reports {name}: {value}"[:500],
                            source_title="Wikidata",
                        )
                    ],
                )
            )
        if any(
            not _same_website(website, str(company.website_url))
            for website in match.websites
        ):
            warnings.append(
                "Wikidata also lists an official website that differs from the "
                "independently verified site; it was not used as identity evidence."
            )
        if len(match.countries) > 1:
            warnings.append(
                "Wikidata lists multiple countries for this organization; they "
                "were retained as attributed alternatives instead of replacing "
                "the website country."
            )
        return additions, warnings

    async def _request(self, query: str) -> httpx.Response:
        """Execute a serial SPARQL read with bounded exponential retries."""
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    WIKIDATA_SPARQL_URL,
                    params={"query": query, "format": "json"},
                    headers=self._headers,
                    timeout=self._timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                if attempt >= self._max_retries:
                    raise WikidataUnavailableError(
                        "Wikidata request failed after retry attempts"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue
            if response.status_code == 200:
                return response
            if response.status_code in {429, 503} or response.status_code >= 500:
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_delay(attempt))
                    continue
                if response.status_code == 429:
                    raise WikidataRateLimitError(
                        "Wikidata rate limit exceeded after retry attempts"
                    )
                raise WikidataUnavailableError(
                    "Wikidata service failed after retry attempts"
                )
            raise WikidataProviderError(
                f"Wikidata query service returned HTTP {response.status_code}"
            )
        raise AssertionError("Wikidata retry loop exited unexpectedly")

    def _backoff_delay(self, attempt: int) -> float:
        """Return a bounded exponential delay."""
        return min(self._backoff_seconds * (2.0**attempt), 60.0)

    async def aclose(self) -> None:
        """Close the internally managed HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "WikidataProvider":
        """Enter an async provider context."""
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """Close provider-owned network resources."""
        await self.aclose()
