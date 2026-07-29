"""Evidence-based deterministic, LLM, and composite company extraction."""

import re
from collections.abc import Iterable, Sequence
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit

from pydantic import JsonValue, ValidationError

from app.core.settings import get_settings
from app.models import (
    CompanyExtraction,
    ExtractedPageContent,
    ExtractionStatus,
    FactBasis,
    LLMCompanyResponse,
    LLMExtractionRequest,
    LLMPageInput,
    RequestedField,
    SupportedField,
)
from app.models.domain import normalize_field_name
from app.providers.llm import LLMProvider

_CORE_FIELDS = (
    "company_name",
    "website_url",
    "summary",
    "services",
    "contact_page_url",
)
_PERSONAL_FIELD_MARKERS = (
    "employee",
    "staff",
    "team_member",
    "founder",
    "owner",
    "ceo",
    "director",
    "personal_email",
    "personal_phone",
)
_EXPLICIT_ONLY_FIELDS = frozenset(
    {
        "company_name",
        "country",
        "services",
        "website_url",
        "contact_page_url",
    }
)
_SERVICE_HEADINGS = (
    "service",
    "services",
    "solutions",
    "expertise",
    "diensten",
    "oplossingen",
)
_TITLE_SEPARATOR = re.compile(r"\s+(?:[|\u2014\u2013-])\s+")
_SERVICE_SPLIT = re.compile(r"\s*(?:,|;|\band\b|\ben\b)\s*", re.IGNORECASE)
_SERVICE_PREFIX = re.compile(
    r"^.*?\b(?:include|includes|offer|offers|zijn|omvatten)\b\s*:?\s*",
    re.IGNORECASE,
)

_LLM_INSTRUCTIONS = """
Extract only the requested company fields from the supplied cleaned website text.
Return JSON matching the supplied schema exactly. Use null when a fact is not
supported; never guess a company name, country, service, website, or contact page.
Every non-null field must cite one or more supplied source URLs. Mark a field
"explicit" only when directly stated and "inference" when it is a conservative
conclusion from cited facts. Do not return employee, founder, executive, or other
personal data. Keep field values concise, do not copy long passages, and write an
original summary using only supported company facts.
""".strip()


@runtime_checkable
class StructuredDataExtractor(Protocol):
    """Replaceable asynchronous company extraction strategy."""

    async def extract(
        self,
        pages: Sequence[ExtractedPageContent],
        requested_fields: Sequence[RequestedField] = (),
        *,
        required_fields: Iterable[str] = (),
    ) -> CompanyExtraction:
        """Return evidence-bearing fields and an acceptance decision."""
        ...


def _is_personal_field(name: str) -> bool:
    """Identify requested fields that could expose employee personal data."""
    return any(marker in name for marker in _PERSONAL_FIELD_MARKERS)


def _expected_names(requested_fields: Sequence[RequestedField]) -> tuple[str, ...]:
    """Return stable core and user-requested field names."""
    return tuple(
        dict.fromkeys(
            (
                *_CORE_FIELDS,
                *(field.name for field in requested_fields),
            )
        )
    )


def _null_field(name: str) -> SupportedField:
    """Create an explicit unsupported-field placeholder."""
    return SupportedField(name=name, value=None)


def _finalize(
    fields: Iterable[SupportedField],
    *,
    requested_fields: Sequence[RequestedField],
    required_fields: Iterable[str],
    rejection_reasons: Iterable[str] = (),
) -> CompanyExtraction:
    """Fill absent fields with null and reject unsupported required values."""
    expected = _expected_names(requested_fields)
    by_name = {field.name: field for field in fields if field.name in expected}
    ordered = [by_name.get(name, _null_field(name)) for name in expected]
    required = {
        "company_name",
        *(field.name for field in requested_fields),
        *(normalize_field_name(name) for name in required_fields),
    }
    reasons = list(dict.fromkeys(rejection_reasons))
    for name in sorted(required):
        supported = by_name.get(name)
        if supported is None or supported.value is None or not supported.evidence_urls:
            reasons.append(f"Required field {name!r} lacks supporting evidence.")
    for requested in requested_fields:
        if _is_personal_field(requested.name):
            reasons.append(
                f"Requested field {requested.name!r} is excluded as "
                "employee personal data."
            )
    reasons = list(dict.fromkeys(reasons))
    return CompanyExtraction(
        status=(ExtractionStatus.REJECTED if reasons else ExtractionStatus.ACCEPTED),
        fields=ordered,
        rejection_reasons=reasons,
    )


def _field(
    name: str,
    value: JsonValue,
    page: ExtractedPageContent,
    *,
    basis: FactBasis = FactBasis.EXPLICIT,
) -> SupportedField:
    """Create a supported field cited to the page that supplied it."""
    return SupportedField(
        name=name,
        value=value,
        evidence_urls=[page.source_url],
        basis=basis,
    )


def _same_company_domain(first_url: str, second_url: str) -> bool:
    """Compare HTTP hosts while treating a leading ``www`` as equivalent."""
    first = (urlsplit(first_url).hostname or "").casefold().removeprefix("www.")
    second = (urlsplit(second_url).hostname or "").casefold().removeprefix("www.")
    return bool(first) and first == second


def _direct_json_value(data: dict[str, JsonValue], *keys: str) -> JsonValue | None:
    """Read a direct JSON-LD property without confusing nested object names."""
    normalized_keys = {normalize_field_name(key) for key in keys}
    for key, value in data.items():
        try:
            normalized_key = normalize_field_name(key)
        except ValueError:
            continue
        if normalized_key in normalized_keys:
            return value
    return None


def _explicit_value_is_present(
    field: SupportedField,
    pages_by_url: dict[str, ExtractedPageContent],
) -> bool:
    """Check that explicit model values occur in their cited cleaned sources."""
    cited_text = "\n".join(
        pages_by_url[str(url)].main_text
        for url in field.evidence_urls
        if str(url) in pages_by_url
    ).casefold()
    values = field.value if isinstance(field.value, list) else [field.value]
    strings = [value for value in values if isinstance(value, str)]
    return bool(strings) and all(value.casefold() in cited_text for value in strings)


def _llm_field_rejection(
    field: SupportedField,
    pages_by_url: dict[str, ExtractedPageContent],
) -> str | None:
    """Return a reason when a model field violates evidence constraints."""
    if any(str(url) not in pages_by_url for url in field.evidence_urls):
        return f"Field {field.name!r} cites a URL that was not supplied."
    if field.value is None:
        return None
    if field.name in _EXPLICIT_ONLY_FIELDS and field.basis is not FactBasis.EXPLICIT:
        return f"Field {field.name!r} may not be inferred."
    if field.name in {"website_url", "contact_page_url"}:
        if not isinstance(field.value, str) or field.value not in pages_by_url:
            return f"Field {field.name!r} contains a URL that was not supplied."
    elif (
        field.basis is FactBasis.EXPLICIT
        and field.name in _EXPLICIT_ONLY_FIELDS
        and not _explicit_value_is_present(field, pages_by_url)
    ):
        return f"Explicit field {field.name!r} is absent from its cited clean text."
    if (
        field.name == "summary"
        and isinstance(field.value, str)
        and len(field.value) >= 160
        and any(
            field.value.casefold() in pages_by_url[str(url)].main_text.casefold()
            for url in field.evidence_urls
        )
    ):
        return "Summary copies a long website passage instead of being original."
    return None


def _json_value(data: object, *keys: str) -> JsonValue | None:
    """Find the first matching key recursively in JSON-LD."""
    if isinstance(data, dict):
        normalized_keys = {normalize_field_name(key) for key in keys}
        for key, value in data.items():
            try:
                normalized_key = normalize_field_name(key)
            except ValueError:
                continue
            if normalized_key in normalized_keys:
                return cast(JsonValue, value)
        for value in data.values():
            found = _json_value(value, *keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _json_value(value, *keys)
            if found is not None:
                return found
    return None


def _string_list(value: JsonValue | None) -> list[str]:
    """Normalize compact string or list JSON-LD values."""
    candidates = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    for item in candidates:
        if (
            isinstance(item, str)
            and (text := " ".join(item.split()))
            and len(text) <= 300
            and text.casefold() not in {existing.casefold() for existing in normalized}
        ):
            normalized.append(text)
    return normalized


def _title_company_name(page: ExtractedPageContent) -> str | None:
    """Extract a conservative company-name candidate from page metadata."""
    organization_title = page.open_graph.get("og:site_name")
    if organization_title:
        return organization_title
    if not page.title:
        return None
    candidate = _TITLE_SEPARATOR.split(page.title, maxsplit=1)[0].strip()
    if not candidate or candidate.casefold() in {"home", "homepage", "welcome"}:
        return None
    return candidate[:300]


def _service_section_values(page: ExtractedPageContent) -> list[str]:
    """Read concise values immediately following service-oriented headings."""
    lines = [line.strip() for line in page.main_text.splitlines() if line.strip()]
    heading_names = {heading.casefold() for heading in page.headings}
    services: list[str] = []
    for index, line in enumerate(lines):
        normalized = line.casefold()
        if normalized not in heading_names or not any(
            marker in normalized for marker in _SERVICE_HEADINGS
        ):
            continue
        for candidate in lines[index + 1 : index + 4]:
            if candidate.casefold() in heading_names:
                break
            stripped = _SERVICE_PREFIX.sub("", candidate).strip(" .:")
            parts = _SERVICE_SPLIT.split(stripped)
            for part in parts:
                value = part.strip(" .:")
                if 1 < len(value) <= 100 and value.casefold() not in {
                    item.casefold() for item in services
                }:
                    services.append(value)
            if services:
                break
    return services[:20]


class DeterministicCompanyExtractor:
    """Extract explicit company facts without model calls or guessing."""

    async def extract(
        self,
        pages: Sequence[ExtractedPageContent],
        requested_fields: Sequence[RequestedField] = (),
        *,
        required_fields: Iterable[str] = (),
    ) -> CompanyExtraction:
        """Inspect structured metadata and cleaned company-page signals."""
        fields: dict[str, SupportedField] = {}
        for page in pages:
            fields.setdefault(
                "website_url",
                _field("website_url", str(page.canonical_url), page),
            )
            contact = next(
                (
                    candidate
                    for candidate in page.contact_page_candidates
                    if _same_company_domain(
                        str(page.canonical_url),
                        str(candidate.url),
                    )
                ),
                None,
            )
            if contact is not None:
                fields.setdefault(
                    "contact_page_url",
                    _field("contact_page_url", str(contact.url), page),
                )

            for organization in page.organization_data:
                name = _direct_json_value(organization, "name", "legalName")
                if isinstance(name, str) and name.strip():
                    fields.setdefault(
                        "company_name",
                        _field("company_name", name.strip()[:300], page),
                    )
                services = _string_list(
                    _json_value(
                        organization,
                        "knowsAbout",
                        "service",
                        "services",
                        "keywords",
                    )
                )
                if services:
                    fields.setdefault(
                        "services",
                        _field("services", cast(JsonValue, services), page),
                    )
                self._requested_json_ld_fields(
                    fields,
                    organization,
                    page,
                    requested_fields,
                )

            if "company_name" not in fields and (
                title_name := _title_company_name(page)
            ):
                fields["company_name"] = _field("company_name", title_name, page)
            if "services" not in fields and (
                service_values := _service_section_values(page)
            ):
                fields["services"] = _field(
                    "services",
                    cast(JsonValue, service_values),
                    page,
                )
            if page.meta_description and any(
                field.name == "description" for field in requested_fields
            ):
                fields.setdefault(
                    "description",
                    _field("description", page.meta_description, page),
                )

        return _finalize(
            fields.values(),
            requested_fields=requested_fields,
            required_fields=required_fields,
        )

    @staticmethod
    def _requested_json_ld_fields(
        fields: dict[str, SupportedField],
        organization: dict[str, JsonValue],
        page: ExtractedPageContent,
        requested_fields: Sequence[RequestedField],
    ) -> None:
        """Fill directly matching safe requested fields from Organization JSON-LD."""
        aliases = {
            "country": ("country", "addressCountry"),
            "company_name": ("name", "legalName"),
            "website_url": ("url",),
        }
        for requested in requested_fields:
            if requested.name in fields or _is_personal_field(requested.name):
                continue
            value = _json_value(
                organization,
                *(aliases.get(requested.name, (requested.name,))),
            )
            if value is not None and not isinstance(value, (dict, list)):
                fields[requested.name] = _field(
                    requested.name,
                    value,
                    page,
                )


class LLMCompanyExtractor:
    """Extract strict nullable fields from clean text through an LLM provider."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._model = model or get_settings().llm_model

    async def extract(
        self,
        pages: Sequence[ExtractedPageContent],
        requested_fields: Sequence[RequestedField] = (),
        *,
        required_fields: Iterable[str] = (),
    ) -> CompanyExtraction:
        """Request strict JSON, then independently validate evidence and privacy."""
        expected = _expected_names(requested_fields)
        safe_names = [name for name in expected if not _is_personal_field(name)]
        if not pages:
            return _finalize(
                (),
                requested_fields=requested_fields,
                required_fields=required_fields,
                rejection_reasons=("At least one cleaned source page is required.",),
            )
        request = LLMExtractionRequest(
            model=self._model,
            requested_fields=safe_names,
            pages=[
                LLMPageInput(
                    source_url=page.source_url,
                    cleaned_text=page.main_text,
                )
                for page in pages
            ],
            instructions=_LLM_INSTRUCTIONS,
        )
        schema = cast(dict[str, JsonValue], LLMCompanyResponse.model_json_schema())
        raw_response = await self._provider.generate_structured(
            request,
            response_schema=schema,
        )
        try:
            response = LLMCompanyResponse.model_validate(raw_response)
        except ValidationError as error:
            return _finalize(
                (),
                requested_fields=requested_fields,
                required_fields=required_fields,
                rejection_reasons=(
                    "LLM response did not match the strict extraction schema: "
                    f"{error.errors()[0]['msg']}",
                ),
            )

        pages_by_url = {str(page.source_url): page for page in pages}
        fields: list[SupportedField] = []
        reasons: list[str] = []
        for field in response.fields:
            if field.name not in safe_names:
                reasons.append(f"LLM returned unrequested field {field.name!r}.")
                continue
            if rejection := _llm_field_rejection(field, pages_by_url):
                reasons.append(rejection)
                continue
            fields.append(field)

        return _finalize(
            fields,
            requested_fields=requested_fields,
            required_fields=required_fields,
            rejection_reasons=reasons,
        )


class CompositeCompanyExtractor:
    """Run deterministic extraction first, then fill gaps with an LLM."""

    def __init__(
        self,
        deterministic: StructuredDataExtractor,
        llm: StructuredDataExtractor,
    ) -> None:
        self._deterministic = deterministic
        self._llm = llm

    async def extract(
        self,
        pages: Sequence[ExtractedPageContent],
        requested_fields: Sequence[RequestedField] = (),
        *,
        required_fields: Iterable[str] = (),
    ) -> CompanyExtraction:
        """Prefer deterministic facts and use model output only for missing values."""
        deterministic = await self._deterministic.extract(
            pages,
            requested_fields,
            required_fields=(),
        )
        llm = await self._llm.extract(
            pages,
            requested_fields,
            required_fields=(),
        )
        merged: dict[str, SupportedField] = {
            field.name: field
            for field in deterministic.fields
            if field.value is not None
        }
        for field in llm.fields:
            if field.value is not None:
                merged.setdefault(field.name, field)

        return _finalize(
            merged.values(),
            requested_fields=requested_fields,
            required_fields=required_fields,
        )
