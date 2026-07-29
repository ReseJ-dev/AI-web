"""Evidence-based deterministic, LLM, and composite company extraction."""

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from pydantic import JsonValue, ValidationError

from app.core.settings import get_settings
from app.models import (
    CompanyExtraction,
    ExtractedPageContent,
    ExtractionMethod,
    ExtractionStatus,
    FactBasis,
    LLMCompanyResponse,
    LLMExtractionRequest,
    LLMPageInput,
    NavigationLink,
    RequestedField,
    SupportedField,
    TextBlockKind,
)
from app.models.domain import normalize_field_name
from app.providers.llm import LLMProvider, LLMProviderResponseError

_CORE_FIELDS = (
    "company_name",
    "website_url",
    "summary",
    "services",
    "contact_page_url",
    "country",
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
    "contact_person",
    "email",
    "mobile",
    "phone",
    "telephone",
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
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,}\d)")
_NON_ALPHANUMERIC = re.compile(r"[^\w]+", re.UNICODE)
_GENERIC_HEADING_WORDS = frozenset(
    {
        "about",
        "agency",
        "commerce",
        "company",
        "contact",
        "digital",
        "ecommerce",
        "expertise",
        "home",
        "services",
        "solutions",
        "specialists",
        "welcome",
    }
)
_BUSINESS_NAME_WORDS = frozenset(
    {
        "agency",
        "bureau",
        "bv",
        "commerce",
        "company",
        "consulting",
        "collective",
        "corp",
        "corporation",
        "digital",
        "group",
        "inc",
        "labs",
        "llc",
        "ltd",
        "network",
        "partners",
        "studio",
        "solutions",
        "technologies",
    }
)
_COUNTRY_MARKERS = (
    ("the netherlands", "Netherlands"),
    ("netherlands", "Netherlands"),
    ("nederland", "Netherlands"),
    ("united kingdom", "United Kingdom"),
    ("great britain", "United Kingdom"),
    ("united states", "United States"),
    ("duitsland", "Germany"),
    ("germany", "Germany"),
    ("belgië", "Belgium"),
    ("belgie", "Belgium"),
    ("belgium", "Belgium"),
    ("frankrijk", "France"),
    ("france", "France"),
)
_SUMMARY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "based",
        "by",
        "company",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "provides",
        "the",
        "their",
        "to",
        "with",
    }
)
_WORD_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

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


@dataclass(frozen=True, slots=True)
class _FieldCandidate:
    """One deterministic value before conflict resolution."""

    name: str
    value: JsonValue
    evidence_url: str
    evidence_fragment: str
    extraction_method: ExtractionMethod
    confidence: float


def _compact_fragment(value: object) -> str:
    """Create compact evidence without retaining email addresses or phone numbers."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    text = _EMAIL.sub("[email removed]", text)
    text = _PHONE.sub("[phone removed]", text)
    return text[:500].strip()


def _contains_personal_contact(value: str) -> bool:
    """Return whether text contains an email address or phone-like number."""
    return bool(_EMAIL.search(value) or _PHONE.search(value))


def _looks_like_person_name(value: str) -> bool:
    """Conservatively reject short title-cased names lacking business markers."""
    words = [word for word in value.split() if word]
    normalized_words = {_NON_ALPHANUMERIC.sub("", word.casefold()) for word in words}
    name_particles = {"de", "den", "der", "van", "von"}
    return (
        2 <= len(words) <= 4
        and all(
            (
                word.casefold() in name_particles
                or (word[:1].isupper() and word[1:].islower())
            )
            and word.isalpha()
            for word in words
        )
        and not normalized_words.intersection(_BUSINESS_NAME_WORDS)
    )


def _plausible_unstructured_company_name(value: str) -> bool:
    """Reject page labels, slogans, and likely people as fallback identities."""
    words = [word for word in _NON_ALPHANUMERIC.split(value.casefold()) if word]
    page_label_words = {
        "about",
        "contact",
        "diensten",
        "expertise",
        "home",
        "oplossingen",
        "services",
        "solutions",
        "welcome",
    }
    if (
        not words
        or words[0] in page_label_words
        or set(words).issubset(_GENERIC_HEADING_WORDS | {"our", "team", "we"})
    ):
        return False
    return not (_contains_personal_contact(value) or _looks_like_person_name(value))


def _candidate(
    name: str,
    value: JsonValue,
    page: ExtractedPageContent,
    *,
    fragment: object,
    method: ExtractionMethod,
    confidence: float,
) -> _FieldCandidate:
    """Create one sanitized deterministic extraction candidate."""
    return _FieldCandidate(
        name=name,
        value=value,
        evidence_url=str(page.source_url),
        evidence_fragment=_compact_fragment(fragment),
        extraction_method=method,
        confidence=confidence,
    )


def _normalized_candidate_value(name: str, value: JsonValue) -> str:
    """Return a stable field-aware conflict key."""
    if isinstance(value, str):
        compacted = " ".join(value.casefold().split())
        if name in {"website_url", "contact_page_url"}:
            parsed = urlsplit(compacted)
            host = (parsed.hostname or "").removeprefix("www.")
            path = parsed.path.rstrip("/") or "/"
            return urlunsplit(("", host, path, parsed.query, ""))
        if name == "country":
            aliases = {
                "nl": "netherlands",
                "nederland": "netherlands",
                "the netherlands": "netherlands",
                "uk": "united kingdom",
                "gb": "united kingdom",
                "usa": "united states",
                "us": "united states",
            }
            return aliases.get(compacted, compacted)
        if name == "company_name":
            return _NON_ALPHANUMERIC.sub("", compacted)
        return compacted
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _supported_candidate(
    candidate: _FieldCandidate,
    *,
    evidence_urls: Sequence[str] | None = None,
    evidence_fragment: str | None = None,
    method: ExtractionMethod | None = None,
    confidence: float | None = None,
    value: JsonValue | None = None,
) -> SupportedField:
    """Convert a resolved deterministic candidate into a supported field."""
    return SupportedField(
        name=candidate.name,
        value=candidate.value if value is None else value,
        evidence_urls=list(evidence_urls or [candidate.evidence_url]),
        basis=FactBasis.EXPLICIT,
        evidence_fragment=(
            evidence_fragment
            if evidence_fragment is not None
            else candidate.evidence_fragment
        ),
        extraction_method=method or candidate.extraction_method,
        confidence=(confidence if confidence is not None else candidate.confidence),
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


def _cited_text(
    field: SupportedField,
    pages_by_url: dict[str, ExtractedPageContent],
) -> str:
    """Join only the clean text from pages explicitly cited by a field."""
    return "\n".join(
        pages_by_url[str(url)].main_text
        for url in field.evidence_urls
        if str(url) in pages_by_url
    )


def _content_tokens(value: str) -> list[str]:
    """Return normalized non-stopword tokens for grounding checks."""
    return [
        token
        for token in _WORD_TOKEN.findall(value.casefold())
        if token not in _SUMMARY_STOPWORDS and len(token) > 1
    ]


def _summary_rejection(
    field: SupportedField,
    pages_by_url: dict[str, ExtractedPageContent],
) -> str | None:
    """Reject copied or weakly grounded model-written summaries."""
    if not isinstance(field.value, str):
        return "Summary must be a string or null."
    if field.basis is not FactBasis.INFERENCE:
        return "Summary must be marked as an inference from supported facts."
    cited = _cited_text(field, pages_by_url)
    if not cited.strip():
        return "Summary has no cited clean source text."
    normalized_summary = " ".join(field.value.casefold().split())
    normalized_cited = " ".join(cited.casefold().split())
    if len(field.value) >= 160 and normalized_summary in normalized_cited:
        return "Summary copies a long website passage instead of being original."
    summary_numbers = {
        token for token in _WORD_TOKEN.findall(field.value) if token.isdigit()
    }
    cited_numbers = {token for token in _WORD_TOKEN.findall(cited) if token.isdigit()}
    if not summary_numbers.issubset(cited_numbers):
        return "Summary contains a numeric fact absent from its cited evidence."
    summary_tokens = _content_tokens(field.value)
    cited_tokens = set(_content_tokens(cited))
    if summary_tokens:
        supported = sum(token in cited_tokens for token in summary_tokens)
        if supported / len(summary_tokens) < 0.6:
            return "Summary contains claims weakly grounded in its cited evidence."
    return None


def _inference_is_grounded(
    field: SupportedField,
    pages_by_url: dict[str, ExtractedPageContent],
) -> bool:
    """Require conservative lexical support for non-summary inferred values."""
    cited_tokens = set(_content_tokens(_cited_text(field, pages_by_url)))
    values = field.value if isinstance(field.value, list) else [field.value]
    value_tokens = [
        token
        for value in values
        if isinstance(value, str)
        for token in _content_tokens(value)
    ]
    return bool(value_tokens) and (
        sum(token in cited_tokens for token in value_tokens) / len(value_tokens) >= 0.6
    )


def _json_string_values(value: JsonValue) -> list[str]:
    """Collect all string leaves from a structured model field."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [string for item in value for string in _json_string_values(item)]
    if isinstance(value, dict):
        return [
            string for item in value.values() for string in _json_string_values(item)
        ]
    return []


def _llm_field_rejection(
    field: SupportedField,
    pages_by_url: dict[str, ExtractedPageContent],
) -> str | None:
    """Return a reason when a model field violates evidence constraints."""
    if any(str(url) not in pages_by_url for url in field.evidence_urls):
        return f"Field {field.name!r} cites a URL that was not supplied."
    if field.value is None:
        return None
    if any(
        _contains_personal_contact(value) for value in _json_string_values(field.value)
    ):
        return f"Field {field.name!r} contains personal contact data."
    if (
        field.extraction_method is not None
        and field.extraction_method is not ExtractionMethod.LLM
    ):
        return f"Field {field.name!r} claims a non-LLM extraction method."
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
    if field.name == "summary":
        return _summary_rejection(field, pages_by_url)
    if field.basis is FactBasis.INFERENCE and not _inference_is_grounded(
        field,
        pages_by_url,
    ):
        return f"Inferred field {field.name!r} is not grounded in cited evidence."
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
            and not _contains_personal_contact(text)
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


def _heading_company_name(page: ExtractedPageContent) -> str | None:
    """Return a conservative organization-like heading rather than a slogan."""
    for heading in page.headings:
        candidate = " ".join(heading.split())
        words = {word for word in _NON_ALPHANUMERIC.split(candidate.casefold()) if word}
        if (
            1 <= len(words) <= 8
            and len(candidate) <= 300
            and words.difference(_GENERIC_HEADING_WORDS)
            and _plausible_unstructured_company_name(candidate)
            and (len(words) == 1 or bool(words.intersection(_BUSINESS_NAME_WORDS)))
        ):
            return candidate
    return None


def _country_from_text(value: str) -> str | None:
    """Return only an explicitly named country from compact website text."""
    normalized = f" {' '.join(value.casefold().split())} "
    for marker, country in _COUNTRY_MARKERS:
        if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", normalized):
            return country
    return None


def _public_contact_candidate(
    page: ExtractedPageContent,
) -> NavigationLink | None:
    """Return a same-domain public business contact page, never personal data."""
    excluded_segments = {
        "account",
        "employee",
        "login",
        "people",
        "person",
        "profile",
        "staff",
        "team",
    }
    for link in page.contact_page_candidates:
        if not _same_company_domain(
            str(page.canonical_url),
            str(link.url),
        ):
            continue
        parsed = urlsplit(str(link.url))
        segments = {
            segment.casefold()
            for segment in parsed.path.strip("/").split("/")
            if segment
        }
        label_and_path = f"{link.text} {parsed.path}".casefold().replace("-", " ")
        if segments.intersection(excluded_segments):
            continue
        if any(
            marker in label_and_path
            for marker in (
                "contact",
                "get in touch",
                "contacteer",
                "contactgegevens",
            )
        ):
            return link
    return None


def _structured_service_values(page: ExtractedPageContent) -> list[str]:
    """Extract concise service names from structured service sections."""
    services: list[str] = []
    generic_headings = {
        "capabilities",
        "diensten",
        "expertise",
        "oplossingen",
        "our services",
        "services",
        "solutions",
        "what we do",
    }
    for section in page.service_sections:
        for block in section.text_blocks:
            if block.kind not in {
                TextBlockKind.HEADING,
                TextBlockKind.LIST_ITEM,
            }:
                continue
            value = " ".join(block.text.split()).strip(" .:")
            if (
                1 < len(value) <= 100
                and value.casefold() not in generic_headings
                and value.casefold() not in {service.casefold() for service in services}
            ):
                services.append(value)
    return services[:20]


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


def _combine_fragments(candidates: Sequence[_FieldCandidate]) -> str:
    """Combine unique compact fragments without exceeding the schema bound."""
    fragments = list(
        dict.fromkeys(
            candidate.evidence_fragment
            for candidate in candidates
            if candidate.evidence_fragment
        )
    )
    return _compact_fragment(" | ".join(fragments))


def _resolve_services(
    candidates: Sequence[_FieldCandidate],
) -> SupportedField | None:
    """Union supported service names across structured deterministic sources."""
    values: list[str] = []
    contributing: list[_FieldCandidate] = []
    for candidate in sorted(candidates, key=lambda item: -item.confidence):
        candidate_values = _string_list(candidate.value)
        added = False
        for value in candidate_values:
            if value.casefold() not in {item.casefold() for item in values}:
                values.append(value)
                added = True
        if added:
            contributing.append(candidate)
    if not values or not contributing:
        return None
    methods = {candidate.extraction_method for candidate in contributing}
    method = (
        next(iter(methods))
        if len(methods) == 1
        else ExtractionMethod.COMBINED_DETERMINISTIC
    )
    evidence_urls = list(
        dict.fromkeys(candidate.evidence_url for candidate in contributing)
    )
    confidence = min(
        0.99,
        max(candidate.confidence for candidate in contributing)
        + (0.01 if len(evidence_urls) > 1 else 0),
    )
    return _supported_candidate(
        contributing[0],
        value=cast(JsonValue, values[:20]),
        evidence_urls=evidence_urls,
        evidence_fragment=_combine_fragments(contributing),
        method=method,
        confidence=confidence,
    )


def _resolve_website(
    candidates: Sequence[_FieldCandidate],
) -> tuple[SupportedField | None, str | None]:
    """Prefer a root canonical URL while rejecting authoritative domain conflicts."""
    ordered = sorted(candidates, key=lambda item: -item.confidence)
    if not ordered:
        return None, None
    top = ordered[0].confidence
    authoritative = [
        candidate for candidate in ordered if candidate.confidence >= top - 0.02
    ]
    domains = {
        (urlsplit(str(candidate.value)).hostname or "").casefold().removeprefix("www.")
        for candidate in authoritative
        if isinstance(candidate.value, str)
    }
    if len(domains) > 1:
        return None, "Conflicting authoritative canonical website domains."
    chosen = min(
        authoritative,
        key=lambda candidate: (
            len(urlsplit(str(candidate.value)).path.strip("/")),
            len(str(candidate.value)),
        ),
    )
    matching = [
        candidate
        for candidate in authoritative
        if _normalized_candidate_value("website_url", candidate.value)
        == _normalized_candidate_value("website_url", chosen.value)
    ]
    return (
        _supported_candidate(
            chosen,
            evidence_urls=list(
                dict.fromkeys(candidate.evidence_url for candidate in matching)
            ),
            evidence_fragment=_combine_fragments(matching),
            confidence=min(0.99, chosen.confidence + 0.01 * (len(matching) > 1)),
        ),
        None,
    )


def _resolve_contact(
    candidates: Sequence[_FieldCandidate],
) -> tuple[SupportedField | None, str | None]:
    """Choose one public business contact page without merging domains."""
    ordered = sorted(candidates, key=lambda item: -item.confidence)
    domains = {
        (urlsplit(str(candidate.value)).hostname or "").casefold().removeprefix("www.")
        for candidate in ordered
        if isinstance(candidate.value, str)
    }
    if len(domains) > 1:
        return None, "Conflicting business contact-page domains."
    chosen = min(
        ordered,
        key=lambda candidate: (
            len(urlsplit(str(candidate.value)).path.strip("/").split("/")),
            len(str(candidate.value)),
        ),
    )
    matching = [
        candidate
        for candidate in ordered
        if _normalized_candidate_value("contact_page_url", candidate.value)
        == _normalized_candidate_value("contact_page_url", chosen.value)
    ]
    return (
        _supported_candidate(
            chosen,
            evidence_urls=list(
                dict.fromkeys(candidate.evidence_url for candidate in matching)
            ),
            evidence_fragment=_combine_fragments(matching),
            confidence=min(0.99, chosen.confidence + 0.01 * (len(matching) > 1)),
        ),
        None,
    )


def _resolve_candidates(
    candidates_by_name: dict[str, list[_FieldCandidate]],
) -> tuple[list[SupportedField], list[str]]:
    """Resolve deterministic candidates by authority and report true conflicts."""
    fields: list[SupportedField] = []
    conflicts: list[str] = []
    for name, candidates in candidates_by_name.items():
        if not candidates:
            continue
        if name == "services":
            if field := _resolve_services(candidates):
                fields.append(field)
            continue
        if name == "website_url":
            field, conflict = _resolve_website(candidates)
            if field is not None:
                fields.append(field)
            if conflict is not None:
                conflicts.append(conflict)
            continue
        if name == "contact_page_url":
            field, conflict = _resolve_contact(candidates)
            if field is not None:
                fields.append(field)
            if conflict is not None:
                conflicts.append(conflict)
            continue

        ordered = sorted(candidates, key=lambda item: -item.confidence)
        top = ordered[0].confidence
        authoritative = [
            candidate for candidate in ordered if candidate.confidence >= top - 0.02
        ]
        keys = {
            _normalized_candidate_value(name, candidate.value)
            for candidate in authoritative
        }
        if len(keys) > 1:
            conflicts.append(
                f"Conflicting authoritative values were found for {name!r}."
            )
            continue
        chosen = authoritative[0]
        matching = [
            candidate
            for candidate in ordered
            if _normalized_candidate_value(name, candidate.value)
            == _normalized_candidate_value(name, chosen.value)
        ]
        methods = {candidate.extraction_method for candidate in matching}
        method = (
            chosen.extraction_method
            if len(methods) == 1
            else ExtractionMethod.COMBINED_DETERMINISTIC
        )
        evidence_urls = list(
            dict.fromkeys(candidate.evidence_url for candidate in matching)
        )
        fields.append(
            _supported_candidate(
                chosen,
                evidence_urls=evidence_urls,
                evidence_fragment=_combine_fragments(matching),
                method=method,
                confidence=min(
                    0.99,
                    chosen.confidence + 0.01 * (len(evidence_urls) > 1),
                ),
            )
        )
    return fields, conflicts


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
        candidates: dict[str, list[_FieldCandidate]] = {}

        def add(candidate: _FieldCandidate) -> None:
            candidates.setdefault(candidate.name, []).append(candidate)

        for page in pages:
            add(
                _candidate(
                    "website_url",
                    str(page.canonical_url),
                    page,
                    fragment=f"Canonical URL: {page.canonical_url}",
                    method=ExtractionMethod.CANONICAL_URL,
                    confidence=0.98,
                )
            )
            contact = _public_contact_candidate(page)
            if contact is not None:
                add(
                    _candidate(
                        "contact_page_url",
                        str(contact.url),
                        page,
                        fragment=f"{contact.text}: {contact.url}",
                        method=ExtractionMethod.CONTACT_LINK,
                        confidence=0.96,
                    )
                )

            for organization in page.organization_data:
                name = _direct_json_value(organization, "name", "legalName")
                if (
                    isinstance(name, str)
                    and (compact_name := " ".join(name.split()))
                    and len(compact_name) <= 300
                    and not _contains_personal_contact(compact_name)
                ):
                    add(
                        _candidate(
                            "company_name",
                            compact_name,
                            page,
                            fragment=f"Organization name: {compact_name}",
                            method=ExtractionMethod.JSON_LD_ORGANIZATION,
                            confidence=0.98,
                        )
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
                    add(
                        _candidate(
                            "services",
                            cast(JsonValue, services),
                            page,
                            fragment={"Organization services": services},
                            method=ExtractionMethod.JSON_LD_ORGANIZATION,
                            confidence=0.95,
                        )
                    )
                country = _json_value(
                    organization,
                    "addressCountry",
                    "country",
                )
                if isinstance(country, str) and (country := " ".join(country.split())):
                    add(
                        _candidate(
                            "country",
                            country[:200],
                            page,
                            fragment=f"Organization country: {country[:200]}",
                            method=ExtractionMethod.JSON_LD_ORGANIZATION,
                            confidence=0.96,
                        )
                    )
                organization_url = _direct_json_value(organization, "url")
                if isinstance(organization_url, str):
                    try:
                        parsed_organization_url = urlsplit(organization_url)
                        if (
                            parsed_organization_url.scheme in {"http", "https"}
                            and parsed_organization_url.hostname is not None
                        ):
                            add(
                                _candidate(
                                    "website_url",
                                    organization_url,
                                    page,
                                    fragment=(f"Organization URL: {organization_url}"),
                                    method=(ExtractionMethod.JSON_LD_ORGANIZATION),
                                    confidence=0.95,
                                )
                            )
                    except ValueError:
                        pass
                self._requested_json_ld_candidates(
                    add,
                    organization,
                    page,
                    requested_fields,
                )

            site_name = page.open_graph.get("og:site_name")
            if (
                site_name
                and len(site_name) <= 300
                and _plausible_unstructured_company_name(site_name)
            ):
                add(
                    _candidate(
                        "company_name",
                        site_name,
                        page,
                        fragment=f"Open Graph site name: {site_name}",
                        method=ExtractionMethod.OPEN_GRAPH,
                        confidence=0.9,
                    )
                )
            open_graph_title = page.open_graph.get("og:title")
            if open_graph_title and not _contains_personal_contact(open_graph_title):
                open_graph_name = _TITLE_SEPARATOR.split(
                    open_graph_title,
                    maxsplit=1,
                )[0].strip()
                if (
                    open_graph_name
                    and len(open_graph_name) <= 300
                    and _plausible_unstructured_company_name(open_graph_name)
                ):
                    add(
                        _candidate(
                            "company_name",
                            open_graph_name,
                            page,
                            fragment=f"Open Graph title: {open_graph_title}",
                            method=ExtractionMethod.OPEN_GRAPH,
                            confidence=0.84,
                        )
                    )
            open_graph_url = page.open_graph.get("og:url")
            if open_graph_url:
                try:
                    parsed_open_graph_url = urlsplit(open_graph_url)
                    if (
                        parsed_open_graph_url.scheme in {"http", "https"}
                        and parsed_open_graph_url.hostname is not None
                    ):
                        add(
                            _candidate(
                                "website_url",
                                open_graph_url,
                                page,
                                fragment=f"Open Graph URL: {open_graph_url}",
                                method=ExtractionMethod.OPEN_GRAPH,
                                confidence=0.93,
                            )
                        )
                except ValueError:
                    pass

            if (title_name := _title_company_name(page)) and (
                _plausible_unstructured_company_name(title_name)
            ):
                add(
                    _candidate(
                        "company_name",
                        title_name,
                        page,
                        fragment=f"Page title: {page.title}",
                        method=ExtractionMethod.PAGE_TITLE,
                        confidence=0.78,
                    )
                )
            if heading_name := _heading_company_name(page):
                add(
                    _candidate(
                        "company_name",
                        heading_name,
                        page,
                        fragment=f"Page heading: {heading_name}",
                        method=ExtractionMethod.HEADING,
                        confidence=0.7,
                    )
                )

            service_values = _structured_service_values(page)
            if not service_values:
                service_values = _service_section_values(page)
            if service_values:
                add(
                    _candidate(
                        "services",
                        cast(JsonValue, service_values),
                        page,
                        fragment={"Service section": service_values},
                        method=ExtractionMethod.SERVICE_SECTION,
                        confidence=0.88,
                    )
                )

            if page.meta_description and any(
                field.name == "description" for field in requested_fields
            ):
                safe_description = _compact_fragment(page.meta_description)
                add(
                    _candidate(
                        "description",
                        safe_description,
                        page,
                        fragment=safe_description,
                        method=ExtractionMethod.META_DESCRIPTION,
                        confidence=0.86,
                    )
                )
            country_texts = [
                page.meta_description or "",
                page.open_graph.get("og:description", ""),
                *page.headings,
            ]
            for country_text in country_texts:
                if country := _country_from_text(country_text):
                    method = (
                        ExtractionMethod.META_DESCRIPTION
                        if country_text == page.meta_description
                        else (
                            ExtractionMethod.OPEN_GRAPH
                            if country_text == page.open_graph.get("og:description", "")
                            else ExtractionMethod.HEADING
                        )
                    )
                    add(
                        _candidate(
                            "country",
                            country,
                            page,
                            fragment=country_text,
                            method=method,
                            confidence=0.76,
                        )
                    )

        fields, conflicts = _resolve_candidates(candidates)
        return _finalize(
            fields,
            requested_fields=requested_fields,
            required_fields=required_fields,
            rejection_reasons=conflicts,
        )

    @staticmethod
    def _requested_json_ld_candidates(
        add: Callable[[_FieldCandidate], None],
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
            if _is_personal_field(requested.name):
                continue
            value = _json_value(
                organization,
                *(aliases.get(requested.name, (requested.name,))),
            )
            if value is not None and not isinstance(value, (dict, list)):
                if isinstance(value, str) and _contains_personal_contact(value):
                    continue
                add(
                    _candidate(
                        requested.name,
                        value,
                        page,
                        fragment={
                            f"Organization {requested.name}": value,
                        },
                        method=ExtractionMethod.JSON_LD_ORGANIZATION,
                        confidence=0.9,
                    )
                )


class LLMCompanyExtractor:
    """Extract strict nullable fields from clean text through an LLM provider."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str | None = None,
        max_response_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        max_input_chars: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        settings = get_settings()
        self._provider = provider
        self._model = model or settings.llm_model
        self._max_response_retries = (
            max_response_retries
            if max_response_retries is not None
            else settings.llm_response_max_retries
        )
        self._retry_backoff_seconds = (
            retry_backoff_seconds
            if retry_backoff_seconds is not None
            else settings.llm_retry_backoff_seconds
        )
        self._max_input_chars = (
            max_input_chars
            if max_input_chars is not None
            else settings.llm_max_input_chars
        )
        self._sleep = sleep
        if not 0 <= self._max_response_retries <= 5:
            raise ValueError("max_response_retries must be between 0 and 5")
        if not 0 <= self._retry_backoff_seconds <= 30:
            raise ValueError("retry_backoff_seconds must be between 0 and 30")
        if not 1_000 <= self._max_input_chars <= 200_000:
            raise ValueError("max_input_chars must be between 1000 and 200000")

    async def aclose(self) -> None:
        """Close an underlying HTTP-backed LLM provider when supported."""
        close = getattr(self._provider, "aclose", None)
        if callable(close):
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome

    def _bounded_pages(
        self,
        pages: Sequence[ExtractedPageContent],
    ) -> list[LLMPageInput]:
        """Bound aggregate clean content without exposing page metadata or HTML."""
        inputs: list[LLMPageInput] = []
        remaining = self._max_input_chars
        for page in pages:
            if remaining <= 0:
                break
            cleaned_text = page.main_text[:remaining]
            if len(page.main_text) > remaining:
                boundary = max(
                    cleaned_text.rfind("\n"),
                    cleaned_text.rfind(" "),
                )
                if boundary >= int(remaining * 0.8):
                    cleaned_text = cleaned_text[:boundary]
            cleaned_text = cleaned_text.rstrip()
            if not cleaned_text:
                continue
            inputs.append(
                LLMPageInput(
                    source_url=page.source_url,
                    cleaned_text=cleaned_text,
                )
            )
            remaining -= len(cleaned_text)
        return inputs

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
        bounded_pages = self._bounded_pages(pages)
        if not bounded_pages:
            return _finalize(
                (),
                requested_fields=requested_fields,
                required_fields=required_fields,
                rejection_reasons=(
                    "At least one page must contain clean text for LLM extraction.",
                ),
            )
        request = LLMExtractionRequest(
            model=self._model,
            requested_fields=safe_names,
            pages=bounded_pages,
            instructions=_LLM_INSTRUCTIONS,
        )
        schema = cast(dict[str, JsonValue], LLMCompanyResponse.model_json_schema())
        response: LLMCompanyResponse | None = None
        last_schema_error = "unknown schema error"
        active_request = request
        for attempt in range(self._max_response_retries + 1):
            try:
                raw_response = await self._provider.generate_structured(
                    active_request,
                    response_schema=schema,
                )
                response = LLMCompanyResponse.model_validate(raw_response)
                break
            except (ValidationError, LLMProviderResponseError) as error:
                if isinstance(error, ValidationError):
                    last_schema_error = str(error.errors()[0]["msg"])
                else:
                    last_schema_error = str(error)
                if attempt >= self._max_response_retries:
                    break
                await self._sleep(self._retry_backoff_seconds * (2**attempt))
                active_request = request.model_copy(
                    update={
                        "instructions": (
                            _LLM_INSTRUCTIONS
                            + "\nThe previous response was malformed. Return "
                            "only JSON matching the supplied schema; use null "
                            "for every unsupported field."
                        )
                    }
                )
        if response is None:
            return _finalize(
                (),
                requested_fields=requested_fields,
                required_fields=required_fields,
                rejection_reasons=(
                    "LLM response did not match the strict extraction schema: "
                    f"{last_schema_error}",
                ),
            )

        bounded_text_by_url = {
            str(page.source_url): page.cleaned_text for page in bounded_pages
        }
        pages_by_url = {
            str(page.source_url): page.model_copy(
                update={
                    "main_text": bounded_text_by_url[str(page.source_url)],
                    "extracted_text_length": len(
                        bounded_text_by_url[str(page.source_url)]
                    ),
                }
            )
            for page in pages
            if str(page.source_url) in bounded_text_by_url
        }
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

    async def aclose(self) -> None:
        """Close unique structured extractor resources when supported."""
        seen: set[int] = set()
        for extractor in (self._deterministic, self._llm):
            if id(extractor) in seen:
                continue
            seen.add(id(extractor))
            close = getattr(extractor, "aclose", None)
            if callable(close):
                outcome = close()
                if inspect.isawaitable(outcome):
                    await outcome

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
        if any(
            reason.startswith("Conflicting authoritative")
            or reason.startswith("Conflicting business contact")
            for reason in deterministic.rejection_reasons
        ):
            return deterministic
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
