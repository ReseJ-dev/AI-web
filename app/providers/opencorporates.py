"""OpenCorporates API-only company verification and enrichment provider."""

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr, ValidationError

from app.core.settings import get_settings
from app.models.domain import CompanyRecord, Evidence, ExtractedField
from app.models.entity_resolution import (
    OfficialIdentifier,
    OfficialIdentifierSource,
)
from app.models.orchestration import EnrichmentResult

OPENCORPORATES_COMPANY_SEARCH_URL = (
    "https://api.opencorporates.com/v0.4/companies/search"
)
_LEGAL_SUFFIXES = (
    "b v",
    "bv",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "ltd",
    "n v",
    "nv",
    "plc",
)
_COUNTRY_CODES = {
    "belgium": "be",
    "france": "fr",
    "germany": "de",
    "netherlands": "nl",
    "the netherlands": "nl",
    "united kingdom": "gb",
    "united states": "us",
}


class OpenCorporatesProviderError(RuntimeError):
    """Base error raised by the OpenCorporates provider."""


class OpenCorporatesConfigurationError(OpenCorporatesProviderError):
    """Raised when API credentials or licensed-use permission are absent."""


class OpenCorporatesAuthenticationError(OpenCorporatesProviderError):
    """Raised when OpenCorporates rejects the configured API token."""


class OpenCorporatesRateLimitError(OpenCorporatesProviderError):
    """Raised after rate-limit retries are exhausted."""


class OpenCorporatesUnavailableError(OpenCorporatesProviderError):
    """Raised after transient request retries are exhausted."""


class OpenCorporatesResponseError(OpenCorporatesProviderError):
    """Raised when the official API returns an invalid success payload."""


class _OpenCorporatesSource(BaseModel):
    """Minimal registry-source attribution returned by OpenCorporates."""

    model_config = ConfigDict(extra="ignore")

    publisher: str | None = None


class _OpenCorporatesCompany(BaseModel):
    """Allowlisted company fields used for verification and enrichment."""

    model_config = ConfigDict(extra="ignore")

    name: str
    jurisdiction_code: str
    company_number: str
    current_status: str | None = None
    registered_address_in_full: str | None = None
    opencorporates_url: str
    registry_url: str | None = None
    source: _OpenCorporatesSource | None = None


class _OpenCorporatesCompanyItem(BaseModel):
    """One wrapped company result."""

    model_config = ConfigDict(extra="ignore")

    company: _OpenCorporatesCompany


class _OpenCorporatesResults(BaseModel):
    """Company search result collection."""

    model_config = ConfigDict(extra="ignore")

    companies: list[_OpenCorporatesCompanyItem]


class _OpenCorporatesResponse(BaseModel):
    """Minimal official API search response."""

    model_config = ConfigDict(extra="ignore")

    results: _OpenCorporatesResults


def _normalized_name(value: str, *, remove_suffix: bool) -> str:
    """Normalize a company name for conservative registry matching."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).split()
    if remove_suffix:
        changed = True
        while tokens and changed:
            changed = False
            compact = " ".join(tokens)
            for suffix in _LEGAL_SUFFIXES:
                if compact == suffix:
                    tokens = []
                    changed = True
                    break
                marker = f" {suffix}"
                if compact.endswith(marker):
                    tokens = compact[: -len(marker)].split()
                    changed = True
                    break
    return " ".join(tokens)


def _name_matches(requested: str, candidate: str) -> bool:
    """Require exact normalized name equality, allowing only legal suffix drift."""
    requested_full = _normalized_name(requested, remove_suffix=False)
    candidate_full = _normalized_name(candidate, remove_suffix=False)
    requested_base = _normalized_name(requested, remove_suffix=True)
    candidate_base = _normalized_name(candidate, remove_suffix=True)
    return bool(requested_full) and (
        requested_full == candidate_full
        or (
            bool(requested_base)
            and bool(candidate_base)
            and requested_base == candidate_base
        )
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read a delta-seconds Retry-After value without guessing HTTP dates."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _valid_http_url(value: str | None) -> str | None:
    """Return a validated public HTTP URL, or null for malformed source data."""
    if value is None:
        return None
    try:
        return str(HttpUrl(value))
    except ValidationError:
        return None


def _valid_opencorporates_url(value: str) -> str | None:
    """Accept evidence links only from the official OpenCorporates web origin."""
    url = _valid_http_url(value)
    if url is None:
        return None
    host = (urlsplit(url).hostname or "").casefold()
    return url if host in {"opencorporates.com", "www.opencorporates.com"} else None


class OpenCorporatesProvider:
    """Verify and enrich companies through the official OpenCorporates API."""

    provider_name = "opencorporates"

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None = None,
        licensed_data_use_allowed: bool | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        max_retry_after_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        settings = get_settings()
        configured_key = api_key or settings.opencorporates_api_key
        if configured_key is None:
            raise OpenCorporatesConfigurationError(
                "OPENCORPORATES_API_KEY is required for enrichment"
            )
        key_value = (
            configured_key.get_secret_value()
            if isinstance(configured_key, SecretStr)
            else configured_key
        )
        if not key_value.strip():
            raise OpenCorporatesConfigurationError(
                "OPENCORPORATES_API_KEY must not be blank"
            )
        permitted = (
            licensed_data_use_allowed
            if licensed_data_use_allowed is not None
            else settings.opencorporates_licensed_data_use_allowed
        )
        if not permitted:
            raise OpenCorporatesConfigurationError(
                "OpenCorporates enrichment is disabled until "
                "OPENCORPORATES_LICENSED_DATA_USE_ALLOWED is true"
            )

        self._api_key = SecretStr(key_value.strip())
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.opencorporates_timeout_seconds
        )
        self._max_retries = (
            max_retries
            if max_retries is not None
            else settings.opencorporates_max_retries
        )
        self._backoff_seconds = (
            backoff_seconds
            if backoff_seconds is not None
            else settings.opencorporates_backoff_seconds
        )
        self._max_retry_after_seconds = (
            max_retry_after_seconds
            if max_retry_after_seconds is not None
            else settings.opencorporates_max_retry_after_seconds
        )
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not 0 <= self._max_retries <= 10:
            raise ValueError("max_retries must be between zero and ten")
        if self._backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")
        if self._max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must not be negative")

        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def enrich(self, company: CompanyRecord) -> EnrichmentResult:
        """Add only unoccupied registry fields from one unambiguous name match."""
        response = await self._request(company)
        try:
            payload = _OpenCorporatesResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise OpenCorporatesResponseError(
                "OpenCorporates returned an invalid company search payload"
            ) from error

        candidates = [
            item.company
            for item in payload.results.companies
            if _name_matches(company.name, item.company.name)
            and _valid_opencorporates_url(item.company.opencorporates_url) is not None
        ]
        if not candidates:
            return EnrichmentResult(
                company=company,
                warnings=[
                    "No exact normalized OpenCorporates company match was found."
                ],
            )
        unique_ids = {
            (
                candidate.jurisdiction_code.strip().casefold(),
                candidate.company_number.strip().casefold(),
            )
            for candidate in candidates
        }
        if len(unique_ids) != 1:
            return EnrichmentResult(
                company=company,
                warnings=[
                    "Multiple exact normalized OpenCorporates matches were found; "
                    "registry enrichment requires manual review."
                ],
            )

        match = candidates[0]
        evidence_url = _valid_opencorporates_url(match.opencorporates_url)
        if evidence_url is None:
            raise OpenCorporatesResponseError(
                "OpenCorporates returned an invalid evidence URL"
            )
        additions = self._fields(match, evidence_url=evidence_url)
        occupied = {field.name for field in company.extracted_fields}
        accepted = [field for field in additions if field.name not in occupied]
        warnings = (
            [
                "Existing evidence-backed fields were retained instead of "
                "OpenCorporates values."
            ]
            if len(accepted) != len(additions)
            else []
        )
        enriched = company.model_copy(
            update={"extracted_fields": [*company.extracted_fields, *accepted]},
            deep=True,
        )
        identifier = OfficialIdentifier(
            source=OfficialIdentifierSource.OPENCORPORATES,
            value=(f"{match.jurisdiction_code.strip()}/{match.company_number.strip()}"),
        )
        return EnrichmentResult(
            company=enriched,
            official_identifiers=[identifier],
            warnings=warnings,
        )

    def _fields(
        self,
        match: _OpenCorporatesCompany,
        *,
        evidence_url: str,
    ) -> list[ExtractedField]:
        """Convert the allowlisted API fields without retaining the raw payload."""
        publisher = (
            match.source.publisher.strip()
            if match.source is not None and match.source.publisher
            else None
        )
        source_title = (
            f"OpenCorporates — {publisher}" if publisher else "OpenCorporates"
        )
        values: tuple[tuple[str, str | None], ...] = (
            ("official_company_name", match.name.strip() or None),
            ("jurisdiction", match.jurisdiction_code.strip() or None),
            ("company_number", match.company_number.strip() or None),
            (
                "current_status",
                match.current_status.strip() if match.current_status else None,
            ),
            (
                "registered_location",
                (
                    match.registered_address_in_full.strip()
                    if match.registered_address_in_full
                    else None
                ),
            ),
            ("official_registry_url", _valid_http_url(match.registry_url)),
        )
        return [
            ExtractedField(
                name=name,
                value=value,
                confidence=0.95,
                evidence=[
                    Evidence(
                        urls=[evidence_url],
                        excerpt=f"OpenCorporates API reports {name}: {value}"[:500],
                        source_title=source_title,
                    )
                ],
            )
            for name, value in values
            if value is not None
        ]

    async def _request(self, company: CompanyRecord) -> httpx.Response:
        """Search the official API with bounded rate-limit and failure retries."""
        params: dict[str, str | int] = {
            "q": company.name,
            "order": "score",
            "per_page": 10,
        }
        country_code = self._country_code(company.extracted_fields)
        if country_code is not None:
            params["country_code"] = country_code
        headers = {
            "Accept": "application/json",
            "X-API-TOKEN": self._api_key.get_secret_value(),
        }

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    OPENCORPORATES_COMPANY_SEARCH_URL,
                    params=params,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                if attempt >= self._max_retries:
                    raise OpenCorporatesUnavailableError(
                        "OpenCorporates API failed after retry attempts"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code == 200:
                return response
            if response.status_code == 401:
                raise OpenCorporatesAuthenticationError(
                    "OpenCorporates rejected the configured API token"
                )
            if response.status_code in {403, 429} or response.status_code >= 500:
                if attempt < self._max_retries:
                    delay = self._backoff_delay(attempt)
                    retry_after = _retry_after_seconds(response)
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                    await self._sleep(min(delay, self._max_retry_after_seconds))
                    continue
                if response.status_code in {403, 429}:
                    raise OpenCorporatesRateLimitError(
                        "OpenCorporates API rate limit exceeded after retry attempts"
                    )
                raise OpenCorporatesUnavailableError(
                    "OpenCorporates API failed after retry attempts"
                )
            raise OpenCorporatesProviderError(
                f"OpenCorporates API returned HTTP {response.status_code}"
            )
        raise AssertionError("OpenCorporates retry loop exited unexpectedly")

    def _backoff_delay(self, attempt: int) -> float:
        """Return a bounded exponential delay."""
        return min(
            self._backoff_seconds * (2.0**attempt),
            self._max_retry_after_seconds,
        )

    @staticmethod
    def _country_code(fields: Iterable[ExtractedField]) -> str | None:
        """Use an explicit website country field to narrow registry matching."""
        for field in fields:
            if (
                field.name == "country"
                and isinstance(field.value, str)
                and field.evidence
            ):
                return _COUNTRY_CODES.get(field.value.strip().casefold())
        return None

    async def aclose(self) -> None:
        """Close the internally managed HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OpenCorporatesProvider":
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
