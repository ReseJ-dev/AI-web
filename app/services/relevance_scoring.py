"""Deterministic evidence-based company relevance scoring."""

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence

from pydantic import JsonValue

from app.models import (
    CompanyExtraction,
    CompanyRecord,
    ComponentScore,
    FactBasis,
    RelevanceComponent,
    RelevanceScoreResult,
    RequestedField,
    ScorePenalty,
    SupportedField,
)
from app.services.company_deduplication import registrable_domain

_MAXIMUMS = {
    RelevanceComponent.TOPIC_MATCH: 30,
    RelevanceComponent.LOCATION_MATCH: 20,
    RelevanceComponent.RELEVANT_SERVICES: 15,
    RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE: 10,
    RelevanceComponent.CONTACT_PAGE: 10,
    RelevanceComponent.EVIDENCE_QUALITY: 10,
    RelevanceComponent.REQUESTED_FIELD_COMPLETENESS: 5,
}
_TOPIC_STOPWORDS = frozenset(
    {
        "a",
        "agencies",
        "agency",
        "and",
        "business",
        "businesses",
        "companies",
        "company",
        "firms",
        "for",
        "in",
        "of",
        "providers",
        "the",
    }
)
_SERVICE_FIELDS = frozenset({"service", "services", "offerings"})
_LOCATION_FIELDS = frozenset(
    {
        "address",
        "country",
        "headquarters",
        "headquarters_location",
        "location",
        "registered_address",
    }
)
_NETHERLANDS_COUNTRY_MARKERS = frozenset(
    {
        "nl",
        "nederland",
        "netherlands",
        "the netherlands",
    }
)
_NETHERLANDS_CITY_MARKERS = frozenset(
    {
        "amsterdam",
        "den haag",
        "eindhoven",
        "groningen",
        "rotterdam",
        "the hague",
        "tilburg",
        "utrecht",
    }
)
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _compact(value: str) -> str:
    """Normalize text for deterministic comparisons."""
    return " ".join(value.casefold().split())


def _flatten_strings(value: JsonValue) -> list[str]:
    """Collect string leaves from a structured field value."""
    if isinstance(value, str):
        return [_compact(value)]
    if isinstance(value, list):
        strings: list[str] = []
        for item in value:
            strings.extend(_flatten_strings(item))
        return strings
    if isinstance(value, dict):
        strings = []
        for item in value.values():
            strings.extend(_flatten_strings(item))
        return strings
    return []


def _topic_terms(topic: str, location: str) -> tuple[str, ...]:
    """Extract stable subject terms while excluding generic query language."""
    location_tokens = set(_TOKEN.findall(_compact(location)))
    terms = [
        token
        for token in _TOKEN.findall(_compact(topic))
        if token not in _TOPIC_STOPWORDS and token not in location_tokens
    ]
    return tuple(dict.fromkeys(terms))


def _supported_fields(extraction: CompanyExtraction) -> list[SupportedField]:
    """Return only non-null evidence-bearing fields."""
    return [
        field
        for field in extraction.fields
        if field.value is not None and field.evidence_urls
    ]


def _matching_fields(
    fields: Iterable[SupportedField],
    names: frozenset[str],
) -> list[SupportedField]:
    """Select supported fields by normalized name."""
    return [field for field in fields if field.name in names]


def _contains_topic(field: SupportedField, terms: tuple[str, ...]) -> bool:
    """Return whether a field value explicitly includes a topic term."""
    values = _flatten_strings(field.value)
    return bool(terms) and any(term in value for term in terms for value in values)


def _json_key(value: JsonValue) -> str:
    """Return a stable key for contradiction detection."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _contradictory_field_names(company: CompanyRecord) -> list[str]:
    """Return field names having multiple distinct evidenced values."""
    grouped: dict[str, set[str]] = defaultdict(set)
    for field in company.extracted_fields:
        if field.evidence:
            grouped[field.name].add(_json_key(field.value))
    return sorted(name for name, values in grouped.items() if len(values) > 1)


def _weak_field_names(company: CompanyRecord) -> list[str]:
    """Return extracted fields with low confidence or no evidence."""
    return sorted(
        {
            field.name
            for field in company.extracted_fields
            if (
                (field.confidence is not None and field.confidence < 0.5)
                or not field.evidence
            )
        }
    )


def _same_registrable_domain(first: str, second: str) -> bool:
    """Compare registrable domains, returning false for malformed values."""
    try:
        return registrable_domain(first) == registrable_domain(second)
    except ValueError:
        return False


class RelevanceScoringService:
    """Compute reproducible relevance points without any LLM scoring."""

    def score(
        self,
        company: CompanyRecord,
        extraction: CompanyExtraction,
        *,
        topic: str,
        location: str,
        requested_fields: Sequence[RequestedField] = (),
    ) -> RelevanceScoreResult:
        """Score one company from explicit, cited facts and deterministic rules."""
        normalized_topic = topic.strip()
        normalized_location = location.strip()
        if not normalized_topic:
            raise ValueError("topic must not be blank")
        if not normalized_location:
            raise ValueError("location must not be blank")

        supported = _supported_fields(extraction)
        terms = _topic_terms(normalized_topic, normalized_location)
        components: dict[RelevanceComponent, ComponentScore] = {}
        penalties: list[ScorePenalty] = []

        components[RelevanceComponent.TOPIC_MATCH] = self._topic_score(
            supported,
            terms,
            penalties,
        )
        components[RelevanceComponent.LOCATION_MATCH] = self._location_score(
            supported,
            normalized_location,
            penalties,
        )
        components[RelevanceComponent.RELEVANT_SERVICES] = self._services_score(
            supported,
            terms,
            penalties,
        )
        components[RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE] = (
            self._website_score(company, supported, penalties)
        )
        components[RelevanceComponent.CONTACT_PAGE] = self._contact_score(
            company,
            supported,
            penalties,
        )
        components[RelevanceComponent.EVIDENCE_QUALITY] = self._evidence_score(
            company,
            supported,
            penalties,
        )
        components[RelevanceComponent.REQUESTED_FIELD_COMPLETENESS] = (
            self._completeness_score(
                extraction,
                requested_fields,
                penalties,
            )
        )

        total = round(sum(component.score for component in components.values()), 2)
        explanation = [
            f"{component.value}: {details.score:g}/{details.maximum} — "
            f"{details.explanation}"
            for component, details in components.items()
        ]
        explanation.append(f"Total deterministic relevance score: {total:g}/100.")
        return RelevanceScoreResult(
            total_score=total,
            components=components,
            explanation=explanation,
            missing_evidence_penalties=penalties,
        )

    @staticmethod
    def _component(
        component: RelevanceComponent,
        score: float,
        explanation: str,
        penalties: list[ScorePenalty],
        penalty_reason: str | None = None,
    ) -> ComponentScore:
        """Create a component and record its transparent point gap."""
        maximum = _MAXIMUMS[component]
        rounded_score = round(score, 2)
        if rounded_score < maximum and penalty_reason:
            penalties.append(
                ScorePenalty(
                    component=component,
                    points=round(maximum - rounded_score, 2),
                    reason=penalty_reason,
                )
            )
        return ComponentScore(
            score=rounded_score,
            maximum=maximum,
            explanation=explanation,
        )

    def _topic_score(
        self,
        fields: list[SupportedField],
        terms: tuple[str, ...],
        penalties: list[ScorePenalty],
    ) -> ComponentScore:
        """Award strong topic points only for explicit evidenced services."""
        services = _matching_fields(fields, _SERVICE_FIELDS)
        explicit = [
            field
            for field in services
            if field.basis is FactBasis.EXPLICIT and _contains_topic(field, terms)
        ]
        if explicit:
            return self._component(
                RelevanceComponent.TOPIC_MATCH,
                30,
                "Explicit service evidence matches the research topic.",
                penalties,
            )
        inferred = [
            field
            for field in services
            if field.basis is FactBasis.INFERENCE and _contains_topic(field, terms)
        ]
        if inferred:
            return self._component(
                RelevanceComponent.TOPIC_MATCH,
                10,
                "Only inferred service evidence matches the research topic.",
                penalties,
                "Twenty topic points were withheld because the match is inferred.",
            )
        other_explicit = [
            field
            for field in fields
            if field.name not in _SERVICE_FIELDS
            and field.basis is FactBasis.EXPLICIT
            and _contains_topic(field, terms)
        ]
        if other_explicit:
            return self._component(
                RelevanceComponent.TOPIC_MATCH,
                8,
                "The topic is mentioned in evidence but not as an explicit service.",
                penalties,
                "Strong topic points require an explicitly supported relevant service.",
            )
        return self._component(
            RelevanceComponent.TOPIC_MATCH,
            0,
            "No evidence-bearing field supports the research topic.",
            penalties,
            "All topic points were withheld because topic support is missing.",
        )

    def _location_score(
        self,
        fields: list[SupportedField],
        location: str,
        penalties: list[ScorePenalty],
    ) -> ComponentScore:
        """Score explicit location facts without using country-code domains."""
        location_fields = _matching_fields(fields, _LOCATION_FIELDS)
        target = _compact(location)
        target_is_netherlands = target in _NETHERLANDS_COUNTRY_MARKERS
        for field in location_fields:
            values = _flatten_strings(field.value)
            country_match = any(
                value in _NETHERLANDS_COUNTRY_MARKERS
                or "netherlands" in value
                or "nederland" in value
                for value in values
            )
            exact_match = any(target == value or target in value for value in values)
            if field.basis is FactBasis.EXPLICIT and (
                exact_match or (target_is_netherlands and country_match)
            ):
                return self._component(
                    RelevanceComponent.LOCATION_MATCH,
                    20,
                    f"Explicit evidence supports the requested location {location!r}.",
                    penalties,
                )
            if field.basis is FactBasis.INFERENCE and (
                exact_match or (target_is_netherlands and country_match)
            ):
                return self._component(
                    RelevanceComponent.LOCATION_MATCH,
                    5,
                    f"The requested location {location!r} is only inferred.",
                    penalties,
                    "Fifteen location points were withheld because location "
                    "is inferred.",
                )
            if (
                target_is_netherlands
                and field.basis is FactBasis.EXPLICIT
                and any(
                    city in value
                    for city in _NETHERLANDS_CITY_MARKERS
                    for value in values
                )
            ):
                return self._component(
                    RelevanceComponent.LOCATION_MATCH,
                    10,
                    "A Dutch city is explicit, but the Netherlands is not "
                    "explicitly stated.",
                    penalties,
                    "Full location points require explicit Netherlands "
                    "country evidence.",
                )
        return self._component(
            RelevanceComponent.LOCATION_MATCH,
            0,
            f"No explicit evidence supports location {location!r}; domain suffixes "
            "are not location evidence.",
            penalties,
            "All location points were withheld because supported location evidence "
            "is missing.",
        )

    def _services_score(
        self,
        fields: list[SupportedField],
        terms: tuple[str, ...],
        penalties: list[ScorePenalty],
    ) -> ComponentScore:
        """Score relevant services independently from the broader topic signal."""
        services = _matching_fields(fields, _SERVICE_FIELDS)
        for field in services:
            if _contains_topic(field, terms) and field.basis is FactBasis.EXPLICIT:
                return self._component(
                    RelevanceComponent.RELEVANT_SERVICES,
                    15,
                    "Relevant services are explicitly stated and cited.",
                    penalties,
                )
            if _contains_topic(field, terms) and field.basis is FactBasis.INFERENCE:
                return self._component(
                    RelevanceComponent.RELEVANT_SERVICES,
                    5,
                    "Relevant services are inferred rather than explicitly stated.",
                    penalties,
                    "Ten service points were withheld because service support "
                    "is inferred.",
                )
        return self._component(
            RelevanceComponent.RELEVANT_SERVICES,
            0,
            "No cited service value matches the research topic.",
            penalties,
            "All relevant-service points were withheld for missing explicit evidence.",
        )

    def _website_score(
        self,
        company: CompanyRecord,
        fields: list[SupportedField],
        penalties: list[ScorePenalty],
    ) -> ComponentScore:
        """Score an explicitly supported official website."""
        website = next(
            (field for field in fields if field.name == "website_url"),
            None,
        )
        if (
            website is not None
            and website.basis is FactBasis.EXPLICIT
            and isinstance(website.value, str)
            and (
                company.website_url is None
                or _same_registrable_domain(
                    website.value,
                    str(company.website_url),
                )
            )
        ):
            return self._component(
                RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE,
                10,
                "The official website is explicit, cited, and domain-consistent.",
                penalties,
            )
        return self._component(
            RelevanceComponent.OFFICIAL_WEBSITE_CONFIDENCE,
            0,
            "No explicit evidence establishes an official website.",
            penalties,
            "All website-confidence points were withheld for missing "
            "official evidence.",
        )

    def _contact_score(
        self,
        company: CompanyRecord,
        fields: list[SupportedField],
        penalties: list[ScorePenalty],
    ) -> ComponentScore:
        """Score a cited same-domain contact page."""
        contact = next(
            (field for field in fields if field.name == "contact_page_url"),
            None,
        )
        if (
            contact is not None
            and contact.basis is FactBasis.EXPLICIT
            and isinstance(contact.value, str)
            and company.website_url is not None
            and _same_registrable_domain(
                contact.value,
                str(company.website_url),
            )
        ):
            return self._component(
                RelevanceComponent.CONTACT_PAGE,
                10,
                "An explicit cited contact page is on the company domain.",
                penalties,
            )
        return self._component(
            RelevanceComponent.CONTACT_PAGE,
            0,
            "No explicit cited same-domain contact page is available.",
            penalties,
            "All contact-page points were withheld for missing supported evidence.",
        )

    def _evidence_score(
        self,
        company: CompanyRecord,
        fields: list[SupportedField],
        penalties: list[ScorePenalty],
    ) -> ComponentScore:
        """Reduce evidence quality for weak, inferred, or contradictory facts."""
        if not fields:
            return self._component(
                RelevanceComponent.EVIDENCE_QUALITY,
                0,
                "No supported fields provide evidence to assess.",
                penalties,
                "All evidence-quality points were withheld because evidence is absent.",
            )
        unique_urls = {str(url) for field in fields for url in field.evidence_urls}
        score = 8.0 + (2.0 if len(unique_urls) >= 2 else 0.0)
        reasons = [
            f"{len(fields)} supported field(s) cite {len(unique_urls)} unique URL(s)."
        ]
        if any(field.basis is FactBasis.INFERENCE for field in fields):
            score -= 2
            reasons.append("Inference reduced evidence quality by 2 points.")
        contradictions = _contradictory_field_names(company)
        if contradictions:
            score -= 4
            reasons.append(
                "Contradictory values reduced quality by 4 points: "
                + ", ".join(contradictions)
                + "."
            )
        weak_fields = _weak_field_names(company)
        if weak_fields:
            weak_deduction = min(4, 2 * len(weak_fields))
            score -= weak_deduction
            reasons.append(
                f"Weak or uncited extracted fields reduced quality by "
                f"{weak_deduction} points: {', '.join(weak_fields)}."
            )
        score = max(0.0, score)
        return self._component(
            RelevanceComponent.EVIDENCE_QUALITY,
            score,
            " ".join(reasons),
            penalties,
            (
                "Evidence-quality points were withheld for limited source diversity, "
                "inference, contradiction, or weak evidence."
            ),
        )

    def _completeness_score(
        self,
        extraction: CompanyExtraction,
        requested_fields: Sequence[RequestedField],
        penalties: list[ScorePenalty],
    ) -> ComponentScore:
        """Score the share of requested fields carrying values and evidence."""
        if not requested_fields:
            return self._component(
                RelevanceComponent.REQUESTED_FIELD_COMPLETENESS,
                5,
                "No additional requested fields are missing.",
                penalties,
            )
        complete = [
            requested.name
            for requested in requested_fields
            if (
                (field := extraction.field(requested.name)) is not None
                and field.value is not None
                and bool(field.evidence_urls)
            )
        ]
        missing = [
            requested.name
            for requested in requested_fields
            if requested.name not in complete
        ]
        score = 5 * len(complete) / len(requested_fields)
        explanation = (
            f"{len(complete)} of {len(requested_fields)} requested fields have "
            "supported values."
        )
        if missing:
            explanation += " Missing: " + ", ".join(missing) + "."
        return self._component(
            RelevanceComponent.REQUESTED_FIELD_COMPLETENESS,
            score,
            explanation,
            penalties,
            (
                "Completeness points were withheld for unsupported requested fields: "
                + ", ".join(missing)
                + "."
                if missing
                else None
            ),
        )
