"""Deterministic same-domain company-page discovery and ranking."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from app.models import (
    NavigationLink,
    PageCandidate,
    PageCategory,
    RankedPage,
)

_PATH_MARKERS: dict[PageCategory, frozenset[str]] = {
    PageCategory.ABOUT: frozenset({"about", "about-us", "company", "over-ons"}),
    PageCategory.SERVICES: frozenset(
        {
            "services",
            "solutions",
            "expertise",
            "diensten",
            "oplossingen",
        }
    ),
    PageCategory.CONTACT: frozenset(
        {
            "contact",
            "contact-us",
            "get-in-touch",
            "contacteer",
            "contactgegevens",
        }
    ),
}
_TEXT_MARKERS: dict[PageCategory, tuple[str, ...]] = {
    PageCategory.ABOUT: ("about", "about us", "over ons"),
    PageCategory.SERVICES: (
        "services",
        "solutions",
        "expertise",
        "diensten",
        "oplossingen",
    ),
    PageCategory.CONTACT: (
        "contact",
        "contact us",
        "get in touch",
        "contacteer",
        "contactgegevens",
    ),
}
_CATEGORY_BASE_SCORE = {
    PageCategory.HOMEPAGE: 1_000,
    PageCategory.ABOUT: 800,
    PageCategory.SERVICES: 700,
    PageCategory.CONTACT: 600,
    PageCategory.RELEVANT: 500,
    PageCategory.OTHER: 0,
}
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class _Classification:
    """Internal page classification with its evidence."""

    category: PageCategory
    reasons: tuple[str, ...]
    bonus: int


def _company_host(hostname: str | None) -> str:
    """Normalize a hostname while treating a leading www as equivalent."""
    if hostname is None:
        return ""
    normalized = hostname.encode("idna").decode("ascii").lower()
    return normalized.removeprefix("www.")


def _canonical_page_url(url: str) -> str:
    """Normalize a page URL for same-domain deduplication."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or hostname is None:
        raise ValueError("page URLs must be absolute HTTP(S) URLs")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("page URLs must not contain credentials")

    normalized_host = hostname.encode("idna").decode("ascii").lower()
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    authority = (
        normalized_host if port is None or default_port else f"{normalized_host}:{port}"
    )
    path = parsed.path or "/"
    return urlunsplit((scheme, authority, path, "", ""))


def _page_identity(url: str) -> tuple[str, str]:
    """Return a key that treats bare and www page URLs as equivalent."""
    parsed = urlsplit(url)
    return (_company_host(parsed.hostname), parsed.path.rstrip("/") or "/")


def _normalized_text(value: str) -> str:
    """Normalize human text for keyword comparisons."""
    return _SPACE.sub(" ", value.casefold().replace("-", " ")).strip()


def _path_segments(url: str) -> tuple[str, ...]:
    """Return normalized decoded path segments."""
    return tuple(
        segment.casefold()
        for segment in unquote(urlsplit(url).path).strip("/").split("/")
        if segment
    )


class PageSelectionService:
    """Select a compact, explainable set of useful company pages."""

    def select(
        self,
        homepage_url: str,
        candidates: Iterable[PageCandidate | NavigationLink],
        *,
        relevant_terms: Iterable[str] = (),
        limit: int = 5,
    ) -> list[RankedPage]:
        """Rank same-domain pages in company-research priority order."""
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")

        canonical_homepage = _canonical_page_url(homepage_url)
        homepage_host = _company_host(urlsplit(canonical_homepage).hostname)
        terms = tuple(
            normalized
            for term in relevant_terms
            if (normalized := _normalized_text(term))
        )

        unique: dict[tuple[str, str], PageCandidate] = {
            _page_identity(canonical_homepage): PageCandidate(url=canonical_homepage)
        }
        for item in candidates:
            candidate = (
                item
                if isinstance(item, PageCandidate)
                else PageCandidate(url=item.url, anchor_text=item.text)
            )
            normalized_url = _canonical_page_url(str(candidate.url))
            if _company_host(urlsplit(normalized_url).hostname) != homepage_host:
                continue
            normalized_candidate = candidate.model_copy(update={"url": normalized_url})
            identity = _page_identity(normalized_url)
            existing = unique.get(identity)
            if existing is None or self._richness(
                normalized_candidate
            ) > self._richness(existing):
                unique[identity] = normalized_candidate

        ranked = [
            self._rank_candidate(
                candidate,
                canonical_homepage=canonical_homepage,
                relevant_terms=terms,
            )
            for candidate in unique.values()
        ]
        ranked.sort(
            key=lambda page: (
                -page.score,
                str(page.url),
            )
        )
        return ranked[:limit]

    @staticmethod
    def _richness(candidate: PageCandidate) -> int:
        """Measure how much useful ranking context a candidate contains."""
        return (
            len(candidate.anchor_text)
            + len(candidate.title)
            + sum(len(heading) for heading in candidate.headings)
        )

    def _rank_candidate(
        self,
        candidate: PageCandidate,
        *,
        canonical_homepage: str,
        relevant_terms: tuple[str, ...],
    ) -> RankedPage:
        """Classify and score one normalized candidate."""
        classification = self._classify(
            candidate,
            canonical_homepage=canonical_homepage,
            relevant_terms=relevant_terms,
        )
        return RankedPage(
            url=candidate.url,
            category=classification.category,
            score=(
                _CATEGORY_BASE_SCORE[classification.category] + classification.bonus
            ),
            reasons=list(classification.reasons),
            anchor_text=candidate.anchor_text,
            title=candidate.title,
            headings=candidate.headings,
        )

    @staticmethod
    def _classify(
        candidate: PageCandidate,
        *,
        canonical_homepage: str,
        relevant_terms: tuple[str, ...],
    ) -> _Classification:
        """Apply path, anchor, title, heading, and topic evidence."""
        candidate_url = _canonical_page_url(str(candidate.url))
        if _page_identity(candidate_url) == _page_identity(canonical_homepage):
            return _Classification(
                category=PageCategory.HOMEPAGE,
                reasons=("URL is the company homepage.",),
                bonus=0,
            )

        segments = _path_segments(candidate_url)
        anchor = _normalized_text(candidate.anchor_text)
        title = _normalized_text(candidate.title)
        headings = _normalized_text(" ".join(candidate.headings))

        for category in (
            PageCategory.ABOUT,
            PageCategory.SERVICES,
            PageCategory.CONTACT,
        ):
            if any(segment in _PATH_MARKERS[category] for segment in segments):
                reasons = [f"URL path matches the {category.value} page pattern."]
                bonus = 40
                markers = _TEXT_MARKERS[category]
                if any(marker in anchor for marker in markers):
                    reasons.append(f"Anchor text indicates a {category.value} page.")
                    bonus += 20
                if any(marker in title for marker in markers):
                    reasons.append(f"Page title indicates a {category.value} page.")
                    bonus += 15
                if any(marker in headings for marker in markers):
                    reasons.append(f"Page headings indicate a {category.value} page.")
                    bonus += 10
                return _Classification(
                    category=category,
                    reasons=tuple(reasons),
                    bonus=bonus,
                )

        for category in (
            PageCategory.ABOUT,
            PageCategory.SERVICES,
            PageCategory.CONTACT,
        ):
            reasons = []
            bonus = 0
            markers = _TEXT_MARKERS[category]
            if any(marker in anchor for marker in markers):
                reasons.append(f"Anchor text indicates a {category.value} page.")
                bonus += 20
            if any(marker in title for marker in markers):
                reasons.append(f"Page title indicates a {category.value} page.")
                bonus += 15
            if any(marker in headings for marker in markers):
                reasons.append(f"Page headings indicate a {category.value} page.")
                bonus += 10
            if reasons:
                return _Classification(
                    category=category,
                    reasons=tuple(reasons),
                    bonus=bonus,
                )

        searchable = _normalized_text(
            " ".join(
                [
                    unquote(urlsplit(candidate_url).path),
                    candidate.anchor_text,
                    candidate.title,
                    *candidate.headings,
                ]
            )
        )
        matched_terms = [term for term in relevant_terms if term in searchable]
        if matched_terms:
            return _Classification(
                category=PageCategory.RELEVANT,
                reasons=(
                    "Page metadata matches relevant platform or industry terms: "
                    + ", ".join(matched_terms)
                    + ".",
                ),
                bonus=min(40, 10 * len(matched_terms)),
            )
        return _Classification(
            category=PageCategory.OTHER,
            reasons=("No preferred company-page signal matched.",),
            bonus=0,
        )
