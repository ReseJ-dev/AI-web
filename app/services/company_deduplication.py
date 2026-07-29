"""Conservative, explainable company deduplication and entity resolution."""

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from typing import cast
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, JsonValue
from rapidfuzz import fuzz
from tldextract import TLDExtract

from app.models import (
    CompanyEntity,
    CompanyRecord,
    EntityResolutionOutcome,
    EntityResolutionResult,
    Evidence,
    ExtractedField,
    MergeMetadata,
    OfficialIdentifier,
    OfficialIdentifierSource,
)
from app.services.domain_normalization import InvalidDomainError, normalize_domain

_PUBLIC_SUFFIX_EXTRACTOR = TLDExtract(
    cache_dir=None,
    suffix_list_urls=(),
    include_psl_private_domains=True,
)
_TRACKING_PARAMETERS = frozenset(
    {
        "_ga",
        "_gl",
        "dclid",
        "fbclid",
        "gad_source",
        "gclid",
        "hscid",
        "hsctatracking",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "ref",
        "referrer",
        "srsltid",
        "source",
        "vero_id",
        "yclid",
    }
)
_TRACKING_PREFIXES = ("utm_", "pk_")
_LEGAL_SUFFIXES = tuple(
    tuple(suffix.split())
    for suffix in (
        "limited liability company",
        "public limited company",
        "societe a responsabilite limitee",
        "sp z o o",
        "naamloze vennootschap",
        "besloten vennootschap",
        "b v b a",
        "incorporated",
        "corporation",
        "company",
        "limited",
        "s l u",
        "b v",
        "n v",
        "s a r l",
        "s a s",
        "s p a",
        "s r l",
        "s a",
        "s r o",
        "s l",
        "a s",
        "p c",
        "s c",
        "o ü",
        "p t e",
        "p t y",
        "l l c",
        "l l p",
        "p l c",
        "g m b h",
        "inc",
        "corp",
        "ltd",
        "llc",
        "llp",
        "plc",
        "gmbh",
        "sarl",
        "sas",
        "spa",
        "srl",
        "sa",
        "sro",
        "slu",
        "oü",
        "ou",
        "pte",
        "pty",
        "kft",
        "ag",
        "bv",
        "nv",
        "oy",
        "ab",
        "as",
        "aps",
        "lp",
        "co",
    )
)
_WIKIDATA_ID = re.compile(r"^q\d+$", re.IGNORECASE)
_OPENCORPORATES_JURISDICTION = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_IDENTIFIER_FIELD_SOURCES = {
    "opencorporates_id": OfficialIdentifierSource.OPENCORPORATES,
    "wikidata_id": OfficialIdentifierSource.WIKIDATA,
}
_CORE_FIELD_ALIASES = {
    "name": frozenset({"company_name", "name"}),
    "website_url": frozenset({"website", "website_url"}),
    "description": frozenset({"description", "summary"}),
    "services": frozenset({"service", "services"}),
}


def normalize_company_url(source: str) -> str:
    """Normalize a company URL without retaining tracking or fragments."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("company URL must be a non-blank string")
    candidate = source.strip()
    if "://" not in candidate and not candidate.startswith("//"):
        candidate = f"https://{candidate}"
    elif candidate.startswith("//"):
        candidate = f"https:{candidate}"

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid company URL: {source!r}") from error
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("company URL must use HTTP or HTTPS")
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"invalid company URL: {source!r}")

    try:
        host = normalize_domain(parsed.hostname).removeprefix("www.")
    except InvalidDomainError as error:
        raise ValueError(str(error)) from error
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    authority = host if port is None or default_port else f"{host}:{port}"
    path = re.sub(r"/+$", "", parsed.path) or ""
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_parameter(key)
    ]
    query_items.sort(key=lambda item: (item[0].casefold(), item[1]))
    return urlunsplit(
        (
            "https",
            authority,
            path,
            urlencode(query_items, doseq=True),
            "",
        )
    )


def _is_tracking_parameter(name: str) -> bool:
    """Return whether a query parameter is used for common tracking."""
    normalized = name.casefold()
    return normalized in _TRACKING_PARAMETERS or normalized.startswith(
        _TRACKING_PREFIXES
    )


def registrable_domain(source: str) -> str:
    """Return an offline Public-Suffix-List registrable domain."""
    normalized_url = normalize_company_url(source)
    host = urlsplit(normalized_url).hostname
    if host is None:  # pragma: no cover - guaranteed by URL normalization
        raise ValueError("normalized company URL has no host")
    extracted = _PUBLIC_SUFFIX_EXTRACTOR(host)
    return extracted.top_domain_under_public_suffix or host


def normalize_company_name(name: str) -> str:
    """Normalize casing, punctuation, whitespace, and trailing legal suffixes."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("company name must be a non-blank string")
    normalized = unicodedata.normalize("NFKC", name).casefold().replace("&", " and ")
    characters = [
        character if character.isalnum() or character.isspace() else " "
        for character in normalized
    ]
    tokens = "".join(characters).split()
    changed = True
    while tokens and changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if len(tokens) >= len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                del tokens[-len(suffix) :]
                changed = True
                break
    if not tokens:
        raise ValueError("company name contains only a legal suffix")
    return " ".join(tokens)


def normalize_official_identifier(
    identifier: OfficialIdentifier,
) -> OfficialIdentifier:
    """Normalize Wikidata and OpenCorporates identifier representations."""
    value = identifier.value.strip()
    if identifier.source is OfficialIdentifierSource.WIKIDATA:
        if "://" in value:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").casefold()
            path_parts = parsed.path.strip("/").split("/")
            if (
                host not in {"wikidata.org", "www.wikidata.org"}
                or len(path_parts) != 2
                or path_parts[0].casefold() not in {"entity", "wiki"}
            ):
                raise ValueError(f"invalid Wikidata identifier: {identifier.value!r}")
            value = path_parts[1]
        if not _WIKIDATA_ID.fullmatch(value):
            raise ValueError(f"invalid Wikidata identifier: {identifier.value!r}")
        value = value.upper()
    else:
        if "://" in value:
            parsed = urlsplit(value)
            if (parsed.hostname or "").casefold() not in {
                "api.opencorporates.com",
                "opencorporates.com",
                "www.opencorporates.com",
            }:
                raise ValueError(
                    f"invalid OpenCorporates identifier: {identifier.value!r}"
                )
            path = unquote(parsed.path).strip("/")
            marker = "companies/"
            if marker not in path.casefold():
                raise ValueError(
                    f"invalid OpenCorporates identifier: {identifier.value!r}"
                )
            marker_index = path.casefold().index(marker) + len(marker)
            value = path[marker_index:]
        value = re.sub(r"\s+", "", value).strip("/").casefold()
        parts = value.split("/")
        if (
            len(parts) != 2
            or _OPENCORPORATES_JURISDICTION.fullmatch(parts[0]) is None
            or not parts[1]
            or len(parts[1]) > 300
        ):
            raise ValueError(
                "OpenCorporates identifiers must include jurisdiction and number"
            )
    return OfficialIdentifier(source=identifier.source, value=value)


def _field_key(value: JsonValue) -> str:
    """Return a stable key for comparing structured field values."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _field_confidence(record: CompanyRecord, core_name: str) -> float:
    """Return the best confidence attached to a core company value."""
    aliases = _CORE_FIELD_ALIASES[core_name]
    confidences = [
        field.confidence
        for field in record.extracted_fields
        if field.name in aliases and field.confidence is not None
    ]
    return max(confidences, default=0.5)


def _record_confidence(record: CompanyRecord) -> float:
    """Score a record for stable ID and timestamp selection during merging."""
    confidences = [
        field.confidence
        for field in record.extracted_fields
        if field.confidence is not None
    ]
    return max(confidences, default=0.5)


def _all_evidence_urls(entities: Iterable[CompanyEntity]) -> list[HttpUrl]:
    """Combine website, canonical, and extracted-field evidence URLs."""
    urls: list[HttpUrl] = []
    seen: set[str] = set()
    for entity in entities:
        candidates: list[HttpUrl] = []
        if entity.record.website_url is not None:
            candidates.append(entity.record.website_url)
        if entity.canonical_url is not None:
            candidates.append(entity.canonical_url)
        for field in entity.record.extracted_fields:
            for evidence in field.evidence:
                candidates.extend(evidence.urls)
        for url in candidates:
            exact_url = str(url)
            if exact_url not in seen:
                seen.add(exact_url)
                urls.append(url)
    return urls


def _append_alternative(
    alternatives: dict[str, list[JsonValue]],
    name: str,
    value: JsonValue | None,
    selected: JsonValue | None,
) -> None:
    """Retain a distinct non-null losing value in merge metadata."""
    if value is None or _field_key(value) == _field_key(selected):
        return
    existing = alternatives.setdefault(name, [])
    key = _field_key(value)
    if all(_field_key(candidate) != key for candidate in existing):
        existing.append(value)


def _merge_extracted_fields(
    left: CompanyRecord,
    right: CompanyRecord,
    alternatives: dict[str, list[JsonValue]],
) -> list[ExtractedField]:
    """Keep the highest-confidence value per field and same-value evidence."""
    grouped: dict[str, list[ExtractedField]] = defaultdict(list)
    for field in (*left.extracted_fields, *right.extracted_fields):
        grouped[field.name].append(field)

    merged: list[ExtractedField] = []
    for name in sorted(grouped):
        candidates = grouped[name]
        selected = max(
            enumerate(candidates),
            key=lambda item: (
                item[1].confidence if item[1].confidence is not None else 0.0,
                -item[0],
            ),
        )[1]
        selected_key = _field_key(selected.value)
        evidence: list[Evidence] = []
        evidence_keys: set[str] = set()
        for candidate in candidates:
            if _field_key(candidate.value) != selected_key:
                _append_alternative(
                    alternatives,
                    name,
                    candidate.value,
                    selected.value,
                )
                continue
            for item in candidate.evidence:
                key = item.model_dump_json()
                if key not in evidence_keys:
                    evidence_keys.add(key)
                    evidence.append(item)
        merged.append(selected.model_copy(update={"evidence": evidence}, deep=True))
    return merged


class CompanyDeduplicationService:
    """Resolve company pairs using ordered, conservative identity signals."""

    def __init__(self, *, fuzzy_name_threshold: float = 92.0) -> None:
        if not 80 <= fuzzy_name_threshold <= 100:
            raise ValueError("fuzzy_name_threshold must be between 80 and 100")
        self._fuzzy_name_threshold = fuzzy_name_threshold

    def resolve(
        self,
        left: CompanyEntity | CompanyRecord,
        right: CompanyEntity | CompanyRecord,
    ) -> EntityResolutionResult:
        """Resolve and optionally merge a pair in the configured signal order."""
        left_entity = self._as_entity(left)
        right_entity = self._as_entity(right)
        left_record = left_entity.record
        right_record = right_entity.record
        explanation: list[str] = []

        identifiers_left, invalid_left = self._identifiers(left_entity)
        identifiers_right, invalid_right = self._identifiers(right_entity)
        invalid_identifiers = [*invalid_left, *invalid_right]
        if invalid_identifiers:
            explanation.append(
                "Malformed official identifier data requires human verification: "
                + "; ".join(invalid_identifiers)
                + "."
            )
            return self._decision(
                left_entity,
                right_entity,
                EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED,
                0.5,
                explanation,
            )
        ambiguous_source = self._ambiguous_identifier_source(
            identifiers_left,
            identifiers_right,
        )
        if ambiguous_source is not None:
            explanation.append(
                f"Multiple {ambiguous_source.value} identifiers occur within one "
                "record; automatic entity resolution requires human verification."
            )
            return self._decision(
                left_entity,
                right_entity,
                EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED,
                0.5,
                explanation,
            )
        conflicting_source = self._conflicting_identifier_source(
            identifiers_left,
            identifiers_right,
        )
        if conflicting_source is not None:
            explanation.append(
                f"Distinct {conflicting_source.value} identifiers prove the records "
                "refer to separate official entities."
            )
            return self._decision(
                left_entity,
                right_entity,
                EntityResolutionOutcome.KEEP_SEPARATE,
                0.99,
                explanation,
            )

        left_domain = self._website_domain(left_record)
        right_domain = self._website_domain(right_record)
        if left_domain and right_domain and left_domain == right_domain:
            explanation.append(f"Exact registrable-domain match: {left_domain!r}.")
            return self._merge_result(
                left_entity,
                right_entity,
                0.99,
                explanation,
            )
        explanation.append(
            "No exact registrable-domain match between the supplied website URLs."
        )

        left_canonical = self._canonical_domain(left_entity)
        right_canonical = self._canonical_domain(right_entity)
        if left_canonical and right_canonical and left_canonical == right_canonical:
            explanation.append(
                f"Redirect-derived canonical domains match: {left_canonical!r}."
            )
            return self._merge_result(
                left_entity,
                right_entity,
                0.98,
                explanation,
            )
        explanation.append("No redirect-derived canonical-domain match.")

        left_name = normalize_company_name(left_record.name)
        right_name = normalize_company_name(right_record.name)
        exact_name_match = left_name == right_name
        if exact_name_match:
            explanation.append(f"Exact normalized company-name match: {left_name!r}.")
        else:
            explanation.append(
                f"Normalized company names differ: {left_name!r} versus {right_name!r}."
            )

        fuzzy_score = float(
            fuzz.WRatio(
                left_name,
                right_name,
                score_cutoff=self._fuzzy_name_threshold,
            )
        )
        shared_identifiers = identifiers_left.intersection(identifiers_right)
        if shared_identifiers:
            formatted = ", ".join(
                f"{source.value}:{value}"
                for source, value in sorted(
                    shared_identifiers,
                    key=lambda item: (item[0].value, item[1]),
                )
            )
            explanation.append(f"Shared official identifier: {formatted}.")
            return self._merge_result(
                left_entity,
                right_entity,
                0.99,
                explanation,
                fuzzy_score=fuzzy_score or None,
            )

        if exact_name_match:
            explanation.append(
                "An exact normalized name without domain or official-identifier "
                "corroboration may represent a namesake."
            )
            explanation.append(
                "Company names alone are insufficient for an automatic merge."
            )
            return self._decision(
                left_entity,
                right_entity,
                EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED,
                0.94,
                explanation,
                fuzzy_score=100.0,
            )

        if fuzzy_score >= self._fuzzy_name_threshold:
            explanation.append(
                f"High fuzzy name similarity ({fuzzy_score:.1f}/100) has no "
                "corroborating domain or official identifier."
            )
            explanation.append(
                "Similar names alone are insufficient for an automatic merge."
            )
            return self._decision(
                left_entity,
                right_entity,
                EntityResolutionOutcome.MANUAL_REVIEW_REQUIRED,
                fuzzy_score / 100,
                explanation,
                fuzzy_score=fuzzy_score,
            )

        actual_score = float(fuzz.WRatio(left_name, right_name))
        explanation.append(
            f"Fuzzy name similarity is below the review threshold "
            f"({actual_score:.1f}/100)."
        )
        explanation.append("No authoritative identity signal matched.")
        return self._decision(
            left_entity,
            right_entity,
            EntityResolutionOutcome.KEEP_SEPARATE,
            max(0.5, 1 - (actual_score / 100)),
            explanation,
            fuzzy_score=actual_score,
        )

    @staticmethod
    def _as_entity(value: CompanyEntity | CompanyRecord) -> CompanyEntity:
        """Wrap plain records in an entity-resolution context."""
        return (
            value if isinstance(value, CompanyEntity) else CompanyEntity(record=value)
        )

    @staticmethod
    def _website_domain(record: CompanyRecord) -> str | None:
        """Return the record's registrable website domain, if available."""
        if record.website_url is None:
            return None
        return registrable_domain(str(record.website_url))

    @staticmethod
    def _canonical_domain(entity: CompanyEntity) -> str | None:
        """Return the registrable redirect-derived canonical domain."""
        if entity.canonical_url is None:
            return None
        return registrable_domain(str(entity.canonical_url))

    @staticmethod
    def _identifiers(
        entity: CompanyEntity,
    ) -> tuple[set[tuple[OfficialIdentifierSource, str]], list[str]]:
        """Collect normalized explicit and extracted official identifiers."""
        identifiers = list(entity.official_identifiers)
        for field in entity.record.extracted_fields:
            source = _IDENTIFIER_FIELD_SOURCES.get(field.name)
            if source is not None and isinstance(field.value, str):
                identifiers.append(OfficialIdentifier(source=source, value=field.value))
        normalized_identifiers: set[tuple[OfficialIdentifierSource, str]] = set()
        invalid: list[str] = []
        for item in identifiers:
            try:
                normalized = normalize_official_identifier(item)
            except ValueError:
                invalid.append(f"{item.source.value}:{item.value}")
                continue
            normalized_identifiers.add((normalized.source, normalized.value))
        return normalized_identifiers, invalid

    @staticmethod
    def _ambiguous_identifier_source(
        left: set[tuple[OfficialIdentifierSource, str]],
        right: set[tuple[OfficialIdentifierSource, str]],
    ) -> OfficialIdentifierSource | None:
        """Detect multiple identifiers from one source within either record."""
        for source in OfficialIdentifierSource:
            left_values = {
                value for item_source, value in left if item_source is source
            }
            right_values = {
                value for item_source, value in right if item_source is source
            }
            if len(left_values) > 1 or len(right_values) > 1:
                return source
        return None

    @staticmethod
    def _conflicting_identifier_source(
        left: set[tuple[OfficialIdentifierSource, str]],
        right: set[tuple[OfficialIdentifierSource, str]],
    ) -> OfficialIdentifierSource | None:
        """Detect different identifiers from the same authoritative source."""
        for source in OfficialIdentifierSource:
            left_values = {
                value for item_source, value in left if item_source is source
            }
            right_values = {
                value for item_source, value in right if item_source is source
            }
            if left_values and right_values and left_values.isdisjoint(right_values):
                return source
        return None

    def _merge_result(
        self,
        left: CompanyEntity,
        right: CompanyEntity,
        confidence: float,
        explanation: list[str],
        *,
        fuzzy_score: float | None = None,
    ) -> EntityResolutionResult:
        """Create a merge decision with values and provenance preserved."""
        merged_company, metadata = self._merge_records(left, right, explanation)
        return EntityResolutionResult(
            left_record_id=left.record.id,
            right_record_id=right.record.id,
            outcome=EntityResolutionOutcome.MERGE,
            confidence=confidence,
            fuzzy_name_score=fuzzy_score,
            explanation=metadata.explanation,
            merged_company=merged_company,
            merge_metadata=metadata,
        )

    @staticmethod
    def _decision(
        left: CompanyEntity,
        right: CompanyEntity,
        outcome: EntityResolutionOutcome,
        confidence: float,
        explanation: list[str],
        *,
        fuzzy_score: float | None = None,
    ) -> EntityResolutionResult:
        """Create a non-merge decision."""
        return EntityResolutionResult(
            left_record_id=left.record.id,
            right_record_id=right.record.id,
            outcome=outcome,
            confidence=confidence,
            fuzzy_name_score=fuzzy_score,
            explanation=explanation,
        )

    @staticmethod
    def _merge_records(
        left: CompanyEntity,
        right: CompanyEntity,
        explanation: list[str],
    ) -> tuple[CompanyRecord, MergeMetadata]:
        """Merge records by confidence while retaining every alternative."""
        left_record = left.record
        right_record = right.record
        alternatives: dict[str, list[JsonValue]] = {}

        primary, secondary = (
            (left_record, right_record)
            if _record_confidence(left_record) >= _record_confidence(right_record)
            else (right_record, left_record)
        )

        def select_core(name: str, left_value: object, right_value: object) -> object:
            left_confidence = _field_confidence(left_record, name)
            right_confidence = _field_confidence(right_record, name)
            if left_value is None:
                return right_value
            if right_value is None:
                return left_value
            selected, losing = (
                (left_value, right_value)
                if left_confidence >= right_confidence
                else (right_value, left_value)
            )
            _append_alternative(
                alternatives,
                name,
                cast(JsonValue, losing),
                cast(JsonValue, selected),
            )
            return selected

        selected_name = cast(
            str,
            select_core("name", left_record.name, right_record.name),
        )
        selected_website = select_core(
            "website_url",
            (
                normalize_company_url(str(left_record.website_url))
                if left_record.website_url is not None
                else None
            ),
            (
                normalize_company_url(str(right_record.website_url))
                if right_record.website_url is not None
                else None
            ),
        )
        selected_description = select_core(
            "description",
            left_record.description,
            right_record.description,
        )
        selected_services = select_core(
            "services",
            left_record.services or None,
            right_record.services or None,
        )
        merged_fields = _merge_extracted_fields(
            left_record,
            right_record,
            alternatives,
        )

        merged = CompanyRecord(
            id=primary.id,
            research_run_id=primary.research_run_id,
            name=selected_name,
            website_url=cast(str | None, selected_website),
            description=cast(str | None, selected_description),
            services=cast(list[str] | None, selected_services) or [],
            extracted_fields=merged_fields,
            created_at=min(left_record.created_at, right_record.created_at),
            updated_at=max(left_record.updated_at, right_record.updated_at),
        )
        explanation_with_merge = [
            *explanation,
            (
                f"Merged record {secondary.id} into {primary.id}; core and extracted "
                "values were selected by confidence, with alternatives retained."
            ),
        ]
        metadata = MergeMetadata(
            source_record_ids=[left_record.id, right_record.id],
            alternative_values=alternatives,
            evidence_urls=_all_evidence_urls((left, right)),
            explanation=explanation_with_merge,
        )
        return merged, metadata
