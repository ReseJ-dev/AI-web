"""Cached geographic validation and normalization through GeoNames."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr, TypeAdapter

from app.core.settings import get_settings
from app.models.domain import CompanyRecord, Evidence, ExtractedField
from app.models.orchestration import EnrichmentResult

GEONAMES_COUNTRY_INFO_URL = "https://secure.geonames.org/countryInfoJSON"
GEONAMES_SEARCH_URL = "https://secure.geonames.org/searchJSON"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_RATE_LIMIT_STATUS_CODES = {18, 19, 20}


class GeoNamesProviderError(RuntimeError):
    """Base error raised by the GeoNames provider."""


class GeoNamesConfigurationError(GeoNamesProviderError):
    """Raised when the required GeoNames username is absent."""


class GeoNamesAuthenticationError(GeoNamesProviderError):
    """Raised when GeoNames rejects the configured account."""


class GeoNamesRateLimitError(GeoNamesProviderError):
    """Raised after GeoNames credit-limit retries are exhausted."""


class GeoNamesUnavailableError(GeoNamesProviderError):
    """Raised after transient GeoNames failures are exhausted."""


class GeoNamesResponseError(GeoNamesProviderError):
    """Raised when GeoNames returns malformed JSON."""


class _GeoCountry(BaseModel):
    """Stable country normalization fields."""

    model_config = ConfigDict(extra="ignore")

    countryCode: str
    countryName: str
    isoAlpha3: str | None = None
    geonameId: int


class _GeoCountryResponse(BaseModel):
    """GeoNames country-info response."""

    model_config = ConfigDict(extra="ignore")

    geonames: list[_GeoCountry]


class _GeoPlace(BaseModel):
    """Allowlisted populated-place fields."""

    model_config = ConfigDict(extra="ignore")

    geonameId: int
    name: str
    toponymName: str | None = None
    countryCode: str
    countryName: str | None = None


class _GeoSearchResponse(BaseModel):
    """GeoNames populated-place search response."""

    model_config = ConfigDict(extra="ignore")

    geonames: list[_GeoPlace]


@dataclass(frozen=True, slots=True)
class _CacheEntry[CacheValue]:
    """One expiring geographic lookup result."""

    expires_at: float
    value: tuple[CacheValue, ...]


def _field(company: CompanyRecord, names: set[str]) -> ExtractedField | None:
    """Return the first evidence-bearing field from a set of aliases."""
    return next(
        (
            item
            for item in company.extracted_fields
            if item.name in names
            and isinstance(item.value, str)
            and item.value.strip()
            and item.evidence
        ),
        None,
    )


def _normalized(value: str) -> str:
    """Normalize geographic text for comparisons and cache keys."""
    normalized = " ".join(value.casefold().split())
    return normalized.removeprefix("the ")


class GeoNamesProvider:
    """Normalize and validate evidenced country and city values with GeoNames."""

    provider_name = "geonames"

    def __init__(
        self,
        *,
        username: SecretStr | str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        cache_ttl_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        settings = get_settings()
        configured_username = username or settings.geonames_username
        if configured_username is None:
            raise GeoNamesConfigurationError(
                "GEONAMES_USERNAME is required for geographic enrichment"
            )
        username_value = (
            configured_username.get_secret_value()
            if isinstance(configured_username, SecretStr)
            else configured_username
        )
        if not username_value.strip():
            raise GeoNamesConfigurationError("GEONAMES_USERNAME must not be blank")
        self._username = SecretStr(username_value.strip())
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.geonames_timeout_seconds
        )
        self._max_retries = (
            max_retries if max_retries is not None else settings.geonames_max_retries
        )
        self._backoff_seconds = (
            backoff_seconds
            if backoff_seconds is not None
            else settings.geonames_backoff_seconds
        )
        self._cache_ttl_seconds = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else settings.geonames_cache_ttl_seconds
        )
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not 0 <= self._max_retries <= 10:
            raise ValueError("max_retries must be between zero and ten")
        if self._backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")
        if self._cache_ttl_seconds < 60:
            raise ValueError("cache_ttl_seconds must be at least 60")
        self._sleep = sleep
        self._clock = clock
        self._country_cache: _CacheEntry[_GeoCountry] | None = None
        self._place_cache: dict[
            tuple[str, str],
            _CacheEntry[_GeoPlace],
        ] = {}
        self._cache_lock = asyncio.Lock()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
        )

    async def enrich(self, company: CompanyRecord) -> EnrichmentResult:
        """Normalize evidenced geography and warn about contradictions."""
        country_field = _field(company, {"country", "registered_country"})
        if country_field is None or not isinstance(country_field.value, str):
            return EnrichmentResult(
                company=company,
                warnings=[
                    "GeoNames normalization requires an evidence-backed country."
                ],
            )
        countries = await self._countries()
        country = self._match_country(country_field.value, countries)
        if country is None:
            return EnrichmentResult(
                company=company,
                warnings=[
                    f"GeoNames could not normalize country {country_field.value!r}."
                ],
            )

        values: list[tuple[str, JsonValue, str, int]] = [
            (
                "normalized_country",
                country.countryName,
                f"GeoNames normalizes {country_field.value!r} to "
                f"{country.countryName}.",
                country.geonameId,
            ),
            (
                "country_code",
                country.countryCode.upper(),
                f"GeoNames assigns ISO country code "
                f"{country.countryCode.upper()} to {country.countryName}.",
                country.geonameId,
            ),
        ]
        warnings: list[str] = []
        city_field = _field(
            company,
            {"city", "headquarters_city", "headquarters_location"},
        )
        if city_field is not None and isinstance(city_field.value, str):
            places = await self._places(
                city_field.value,
                country.countryCode.upper(),
            )
            place = next(
                (
                    candidate
                    for candidate in places
                    if _normalized(candidate.name) == _normalized(city_field.value)
                    or (
                        candidate.toponymName is not None
                        and _normalized(candidate.toponymName)
                        == _normalized(city_field.value)
                    )
                ),
                None,
            )
            if place is None:
                elsewhere = await self._places(city_field.value, None)
                other_codes = sorted(
                    {
                        candidate.countryCode.upper()
                        for candidate in elsewhere
                        if candidate.countryCode.upper() != country.countryCode.upper()
                    }
                )
                if other_codes:
                    warnings.append(
                        f"GeoNames places {city_field.value!r} in "
                        f"{', '.join(other_codes)}, contradicting the "
                        f"evidence-backed country {country.countryName}."
                    )
                else:
                    warnings.append(
                        f"GeoNames could not validate city "
                        f"{city_field.value!r} in {country.countryName}."
                    )
            else:
                city_name = (place.toponymName or place.name).strip()
                values.extend(
                    (
                        (
                            "normalized_city",
                            city_name,
                            f"GeoNames validates {city_field.value!r} as "
                            f"{city_name}, {country.countryCode.upper()}.",
                            place.geonameId,
                        ),
                        (
                            "geonames_city_id",
                            str(place.geonameId),
                            f"GeoNames identifier for {city_name}.",
                            place.geonameId,
                        ),
                    )
                )

        additions, conflict_warnings = self._additions(company, values)
        warnings.extend(conflict_warnings)
        return EnrichmentResult(
            company=company.model_copy(
                update={
                    "extracted_fields": [
                        *company.extracted_fields,
                        *additions,
                    ]
                },
                deep=True,
            ),
            warnings=warnings,
        )

    async def _countries(self) -> list[_GeoCountry]:
        """Return cached stable country reference data."""
        async with self._cache_lock:
            cached = self._country_cache
            if cached is not None and cached.expires_at > self._clock():
                return list(cached.value)
            payload = await self._request(
                GEONAMES_COUNTRY_INFO_URL,
                {"lang": "en"},
            )
            try:
                countries = _GeoCountryResponse.model_validate(payload).geonames
            except ValueError as error:
                raise GeoNamesResponseError(
                    "GeoNames returned invalid country information"
                ) from error
            self._country_cache = _CacheEntry(
                expires_at=self._clock() + self._cache_ttl_seconds,
                value=tuple(countries),
            )
            return countries

    async def _places(
        self,
        city: str,
        country_code: str | None,
    ) -> list[_GeoPlace]:
        """Return cached exact populated-place search results."""
        key = (_normalized(city), country_code or "*")
        async with self._cache_lock:
            cached = self._place_cache.get(key)
            if cached is not None and cached.expires_at > self._clock():
                return list(cached.value)
            params: dict[str, str | int] = {
                "name_equals": city.strip(),
                "featureClass": "P",
                "maxRows": 10,
                "lang": "en",
                "style": "FULL",
            }
            if country_code is not None:
                params["country"] = country_code
            payload = await self._request(GEONAMES_SEARCH_URL, params)
            try:
                places = _GeoSearchResponse.model_validate(payload).geonames
            except ValueError as error:
                raise GeoNamesResponseError(
                    "GeoNames returned invalid populated-place data"
                ) from error
            self._place_cache[key] = _CacheEntry(
                expires_at=self._clock() + self._cache_ttl_seconds,
                value=tuple(places),
            )
            return places

    @staticmethod
    def _match_country(
        value: str,
        countries: list[_GeoCountry],
    ) -> _GeoCountry | None:
        """Match country names and ISO alpha-2/alpha-3 codes exactly."""
        target = _normalized(value)
        return next(
            (
                country
                for country in countries
                if target
                in {
                    _normalized(country.countryName),
                    country.countryCode.casefold(),
                    (country.isoAlpha3 or "").casefold(),
                }
            ),
            None,
        )

    @staticmethod
    def _additions(
        company: CompanyRecord,
        values: list[tuple[str, JsonValue, str, int]],
    ) -> tuple[list[ExtractedField], list[str]]:
        """Preserve occupied fields and report contradictory normalized data."""
        occupied = {field.name: field for field in company.extracted_fields}
        additions: list[ExtractedField] = []
        warnings: list[str] = []
        for name, value, excerpt, geoname_id in values:
            existing = occupied.get(name)
            if existing is not None:
                if existing.value != value:
                    warnings.append(
                        f"GeoNames {name!r} contradicts stronger existing "
                        "evidence; the existing value was retained."
                    )
                continue
            additions.append(
                ExtractedField(
                    name=name,
                    value=value,
                    confidence=0.9,
                    evidence=[
                        Evidence(
                            urls=[f"https://www.geonames.org/{geoname_id}"],
                            excerpt=excerpt,
                            source_title="GeoNames",
                        )
                    ],
                )
            )
        return additions, warnings

    async def _request(
        self,
        url: str,
        parameters: dict[str, str | int],
    ) -> dict[str, JsonValue]:
        """Request one official JSON service with retry and API-status handling."""
        params = {
            **parameters,
            "username": self._username.get_secret_value(),
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=self._timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                if attempt >= self._max_retries:
                    raise GeoNamesUnavailableError(
                        "GeoNames request failed after retry attempts"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue
            if response.status_code == 200:
                try:
                    payload = _JSON_OBJECT.validate_python(response.json())
                except ValueError as error:
                    raise GeoNamesResponseError(
                        "GeoNames returned invalid JSON"
                    ) from error
                status = payload.get("status")
                if isinstance(status, dict):
                    code = status.get("value")
                    message = status.get("message")
                    if isinstance(code, int) and code in _RATE_LIMIT_STATUS_CODES:
                        if attempt < self._max_retries:
                            await self._sleep(self._backoff_delay(attempt))
                            continue
                        raise GeoNamesRateLimitError(
                            "GeoNames credit limit exceeded after retry attempts"
                        )
                    if code == 10:
                        raise GeoNamesAuthenticationError(
                            "GeoNames rejected the configured username"
                        )
                    raise GeoNamesProviderError(f"GeoNames API error {code}: {message}")
                return payload
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_delay(attempt))
                    continue
                if response.status_code == 429:
                    raise GeoNamesRateLimitError(
                        "GeoNames rate limit exceeded after retry attempts"
                    )
                raise GeoNamesUnavailableError(
                    "GeoNames service failed after retry attempts"
                )
            if response.status_code in {401, 403}:
                raise GeoNamesAuthenticationError(
                    "GeoNames rejected the configured username"
                )
            raise GeoNamesProviderError(
                f"GeoNames API returned HTTP {response.status_code}"
            )
        raise AssertionError("GeoNames retry loop exited unexpectedly")

    def _backoff_delay(self, attempt: int) -> float:
        """Return a bounded exponential delay."""
        return min(self._backoff_seconds * (2.0**attempt), 60.0)

    async def aclose(self) -> None:
        """Close the internally managed HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GeoNamesProvider":
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
