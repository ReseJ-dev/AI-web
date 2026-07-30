"""Deterministic query planning for candidate discovery."""

import re

_TRAILING_SERVICE_TERM = re.compile(
    r"\s+(?:agency|agencies|company|companies|provider|providers|services?)$",
    re.IGNORECASE,
)
_COUNTRY_TLD = re.compile(r"^[a-z]{2,63}$")


def _compact(value: str, field_name: str) -> str:
    """Collapse whitespace and reject blank planning inputs."""
    compacted = " ".join(value.split())
    if not compacted:
        raise ValueError(f"{field_name} must not be blank")
    return compacted


class QueryPlanner:
    """Generate varied discovery queries from a research topic and market."""

    def plan(
        self,
        topic: str,
        *,
        location: str,
        city: str | None = None,
        country_tld: str | None = None,
    ) -> list[str]:
        """Return deduplicated market, service, city, and local-domain queries."""
        normalized_topic = _compact(topic, "topic")
        normalized_location = _compact(location, "location")
        normalized_city = (
            _compact(city, "city") if city is not None else normalized_location
        )
        location_suffix = re.compile(
            rf"\s+in\s+(?:the\s+)?{re.escape(normalized_location)}$",
            re.IGNORECASE,
        )
        topic_without_location = location_suffix.sub("", normalized_topic).strip()
        subject = _TRAILING_SERVICE_TERM.sub("", topic_without_location).strip()
        if not subject:
            subject = topic_without_location or normalized_topic

        topic_has_location = topic_without_location != normalized_topic
        queries = [
            normalized_topic
            if topic_has_location
            else f"{normalized_topic} {normalized_location}"
        ]
        if subject.casefold() == "shopify":
            queries.append(f"{subject} Plus agency {normalized_location}")
        else:
            queries.append(f"{subject} specialist agency {normalized_location}")
        queries.extend(
            [
                f"{subject} development company {normalized_location}",
                f"{subject} ecommerce agency {normalized_city}",
            ]
        )

        if country_tld is not None:
            tld = country_tld.strip().lower().removeprefix(".")
            if not _COUNTRY_TLD.fullmatch(tld):
                raise ValueError("country_tld must contain only letters")
            queries.append(f"site:.{tld} {subject} agency")

        return list(dict.fromkeys(queries))
