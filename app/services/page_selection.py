"""Deterministic same-domain company-page discovery and ranking."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from app.models import NavigationLink, PageCandidate, PageCategory, RankedPage

_PATH_MARKERS: dict[PageCategory, frozenset[str]] = {
    PageCategory.ABOUT: frozenset({"about", "about-us", "company", "over-ons"}),
    PageCategory.SERVICES: frozenset({"services", "diensten"}),
    PageCategory.SOLUTIONS: frozenset({"solutions", "oplossingen"}),
    PageCategory.EXPERTISE: frozenset({"expertise"}),
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
    PageCategory.SERVICES: ("service", "services", "diensten"),
    PageCategory.SOLUTIONS: ("solution", "solutions", "oplossingen"),
    PageCategory.EXPERTISE: ("expertise", "specialisms", "specialismen"),
    PageCategory.CONTACT: (
        "contact",
        "contact us",
        "get in touch",
        "contacteer",
        "contactgegevens",
    ),
}
_PAGE_TYPE_ORDER = (
    PageCategory.ABOUT,
    PageCategory.SERVICES,
    PageCategory.SOLUTIONS,
    PageCategory.EXPERTISE,
    PageCategory.CONTACT,
)
_CATEGORY_BASE_SCORE = {
    PageCategory.HOMEPAGE: 1_400,
    PageCategory.ABOUT: 1_200,
    PageCategory.SERVICES: 1_000,
    PageCategory.SOLUTIONS: 800,
    PageCategory.EXPERTISE: 600,
    PageCategory.CONTACT: 400,
    PageCategory.RELEVANT: 200,
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


def _compact_text(value: str) -> str:
    """Collapse HTML-derived text without retaining layout whitespace."""
    return _SPACE.sub(" ", value).strip()


def _is_navigation_anchor(anchor: Tag) -> bool:
    """Return whether an anchor appears in site navigation or a header."""
    return any(
        parent.name in {"nav", "header"}
        or (
            isinstance(parent.get("role"), str)
            and str(parent.get("role")).casefold() == "navigation"
        )
        for parent in anchor.parents
        if isinstance(parent, Tag)
    )


class PageSelectionService:
    """Discover and rank a compact set of useful company pages."""

    def discover(
        self,
        homepage_url: str,
        html_pages: str | Mapping[str, str],
        *,
        relevant_terms: Iterable[str] = (),
        limit: int = 5,
    ) -> list[RankedPage]:
        """Discover links from supplied HTML and rank same-domain pages.

        This method performs no network requests. A mapping may contain HTML for
        multiple already-approved pages so their titles and headings can enrich
        candidates. Form actions, scripts, and external-domain documents are
        never considered.
        """
        canonical_homepage = _canonical_page_url(homepage_url)
        approved_host = _company_host(urlsplit(canonical_homepage).hostname)
        documents: Mapping[str, str] = (
            {canonical_homepage: html_pages}
            if isinstance(html_pages, str)
            else html_pages
        )
        candidates: list[PageCandidate] = []
        navigation_position = 0

        for source_url, html in documents.items():
            try:
                canonical_source = _canonical_page_url(source_url)
            except ValueError:
                continue
            if _company_host(urlsplit(canonical_source).hostname) != approved_host:
                continue

            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            title = (
                _compact_text(title_tag.get_text(" ", strip=True))[:1_000]
                if isinstance(title_tag, Tag)
                else ""
            )
            headings = [
                compacted[:1_000]
                for heading in soup.find_all(re.compile(r"^h[1-6]$"))
                if (compacted := _compact_text(heading.get_text(" ", strip=True)))
            ]
            candidates.append(
                PageCandidate(
                    url=canonical_source,
                    title=title,
                    headings=headings,
                )
            )

            for anchor in soup.find_all("a", href=True):
                href = anchor.get("href")
                if not isinstance(href, str) or not href.strip():
                    continue
                try:
                    target = _canonical_page_url(
                        urljoin(canonical_source, href.strip())
                    )
                except ValueError:
                    continue
                position: int | None = None
                if _is_navigation_anchor(anchor):
                    position = navigation_position
                    navigation_position += 1
                candidates.append(
                    PageCandidate(
                        url=target,
                        anchor_text=_compact_text(anchor.get_text(" ", strip=True))[
                            :1_000
                        ],
                        navigation_position=position,
                    )
                )

        return self.select(
            canonical_homepage,
            candidates,
            relevant_terms=relevant_terms,
            limit=limit,
        )

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
            unique[identity] = (
                normalized_candidate
                if existing is None
                else self._merge_candidates(existing, normalized_candidate)
            )

        ranked = [
            self._rank_candidate(
                candidate,
                canonical_homepage=canonical_homepage,
                relevant_terms=terms,
            )
            for candidate in unique.values()
        ]
        ranked.sort(key=lambda page: (-page.score, str(page.url)))
        return ranked[:limit]

    @staticmethod
    def _merge_candidates(
        existing: PageCandidate,
        incoming: PageCandidate,
    ) -> PageCandidate:
        """Combine link context and document metadata for a single URL."""
        positions = [
            position
            for position in (
                existing.navigation_position,
                incoming.navigation_position,
            )
            if position is not None
        ]
        return PageCandidate(
            url=existing.url,
            anchor_text=max(
                (existing.anchor_text, incoming.anchor_text),
                key=len,
            ),
            title=max((existing.title, incoming.title), key=len),
            headings=list(dict.fromkeys((*existing.headings, *incoming.headings))),
            navigation_position=min(positions) if positions else None,
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
        navigation_bonus = 0
        reasons = list(classification.reasons)
        if candidate.navigation_position is not None:
            navigation_bonus = max(1, 25 - min(candidate.navigation_position, 24))
            reasons.append(
                "Link appears in site navigation at position "
                f"{candidate.navigation_position + 1}."
            )
        return RankedPage(
            url=candidate.url,
            category=classification.category,
            score=(
                _CATEGORY_BASE_SCORE[classification.category]
                + classification.bonus
                + navigation_bonus
            ),
            reasons=reasons,
            anchor_text=candidate.anchor_text,
            title=candidate.title,
            headings=candidate.headings,
            navigation_position=candidate.navigation_position,
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

        for category in _PAGE_TYPE_ORDER:
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

        for category in _PAGE_TYPE_ORDER:
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
