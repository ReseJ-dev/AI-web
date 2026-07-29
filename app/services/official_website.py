"""Deterministic identification of likely official company websites."""

from urllib.parse import urlsplit, urlunsplit

from app.models import OfficialWebsiteAssessment, SearchCandidate
from app.services.company_deduplication import normalize_company_url
from app.services.domain_normalization import InvalidDomainError, normalize_domain

_NON_OFFICIAL_PATH_MARKERS = frozenset(
    {
        "companies",
        "company-profile",
        "directory",
        "listing",
        "marketplace",
        "profile",
        "search",
    }
)
_NON_OFFICIAL_TITLE_MARKERS = (
    "business directory",
    "company directory",
    "find companies",
    "marketplace listing",
)


class OfficialWebsiteIdentificationService:
    """Identify likely official roots without trusting search snippets."""

    def assess(self, candidate: SearchCandidate) -> OfficialWebsiteAssessment:
        """Normalize and assess one transient search candidate."""
        try:
            normalized_url = normalize_company_url(str(candidate.url))
            candidate_domain = normalize_domain(candidate.domain).removeprefix("www.")
            url_domain = normalize_domain(normalized_url).removeprefix("www.")
        except (InvalidDomainError, ValueError) as error:
            return OfficialWebsiteAssessment(
                likely_official=False,
                reason=f"Candidate URL or domain is malformed: {error}.",
            )
        if candidate_domain != url_domain:
            return OfficialWebsiteAssessment(
                likely_official=False,
                reason=(
                    "Candidate metadata domain does not match the candidate URL host."
                ),
            )

        parsed = urlsplit(normalized_url)
        path_segments = {
            segment.casefold()
            for segment in parsed.path.strip("/").split("/")
            if segment
        }
        if path_segments.intersection(_NON_OFFICIAL_PATH_MARKERS):
            return OfficialWebsiteAssessment(
                likely_official=False,
                reason="Candidate path resembles a directory or third-party profile.",
            )
        normalized_title = " ".join(candidate.title.casefold().split())
        if any(marker in normalized_title for marker in _NON_OFFICIAL_TITLE_MARKERS):
            return OfficialWebsiteAssessment(
                likely_official=False,
                reason="Candidate title resembles a directory or marketplace listing.",
            )

        website_root = urlunsplit(("https", parsed.netloc, "/", "", ""))
        return OfficialWebsiteAssessment(
            likely_official=True,
            website_url=website_root,
            reason=(
                "Candidate has a consistent domain and no directory or profile signals."
            ),
        )
