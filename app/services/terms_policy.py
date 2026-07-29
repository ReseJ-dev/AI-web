"""Advisory terms-link discovery and automated-access risk scanning."""

import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from app.models import PreflightDecision, TermsLink, TermsPolicyResult

_TERMS_MARKERS = (
    "terms",
    "terms-of-use",
    "legal",
    "acceptable-use",
    "voorwaarden",
    "gebruiksvoorwaarden",
)
_AUTOMATION_PATTERN = re.compile(
    r"\b(?:automated\s+(?:access|requests?)|scrap(?:e|ing|ers?)|"
    r"spiders?|robots?|data\s+extraction)\b",
    re.IGNORECASE,
)
_PROHIBITION_PATTERN = re.compile(
    r"\b(?:prohibited|forbidden|not\s+permitted|may\s+not|must\s+not|"
    r"shall\s+not|do\s+not)\b",
    re.IGNORECASE,
)
_RISK_WINDOW = 180
_DISCLAIMER = "This is a compliance risk signal only and is not legal advice."


class _HtmlTextAndLinkParser(HTMLParser):
    """Collect visible text and anchors without executing page content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """Track anchors and suppress script/style text."""
        normalized_tag = tag.lower()
        if normalized_tag in {"script", "style", "noscript"}:
            self._hidden_depth += 1
            return
        if normalized_tag != "a" or self._hidden_depth:
            return
        attributes = dict(attrs)
        self._anchor_href = attributes.get("href")
        self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        """Finish anchors and hidden elements."""
        normalized_tag = tag.lower()
        if normalized_tag in {"script", "style", "noscript"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)
            return
        if (
            normalized_tag == "a"
            and self._hidden_depth == 0
            and self._anchor_href is not None
        ):
            label = " ".join(" ".join(self._anchor_text).split())
            self.links.append((self._anchor_href, label))
            self._anchor_href = None
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        """Collect visible document and active-anchor text."""
        if self._hidden_depth:
            return
        compacted = " ".join(data.split())
        if not compacted:
            return
        self.text_parts.append(compacted)
        if self._anchor_href is not None:
            self._anchor_text.append(compacted)


def _parse_html(html: str) -> _HtmlTextAndLinkParser:
    """Parse HTML into inert text and link collections."""
    parser = _HtmlTextAndLinkParser()
    parser.feed(html)
    parser.close()
    return parser


def _contains_terms_marker(label: str, url: str) -> bool:
    """Return whether link label or decoded path contains a configured marker."""
    label_text = label.casefold()
    path_text = unquote(urlsplit(url).path).casefold()
    return any(marker in label_text or marker in path_text for marker in _TERMS_MARKERS)


def _public_http_url(page_url: str, href: str) -> str | None:
    """Resolve an HTTP(S) link and remove its fragment."""
    resolved = urljoin(page_url, href.strip())
    parsed = urlsplit(resolved)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _has_explicit_prohibition(text: str) -> bool:
    """Find automation and prohibition language in the same local context."""
    automation_matches = list(_AUTOMATION_PATTERN.finditer(text))
    prohibition_matches = list(_PROHIBITION_PATTERN.finditer(text))
    return any(
        abs(automation.start() - prohibition.start()) <= _RISK_WINDOW
        for automation in automation_matches
        for prohibition in prohibition_matches
    )


class TermsPolicyScanner:
    """Find public terms links and report non-legal automated-access signals."""

    def discover_links(self, page_url: str, html: str) -> list[TermsLink]:
        """Return deduplicated public links whose labels or paths look legal."""
        parser = _parse_html(html)
        discovered: list[TermsLink] = []
        seen: set[str] = set()
        for href, label in parser.links:
            public_url = _public_http_url(page_url, href)
            if (
                public_url is None
                or public_url in seen
                or not _contains_terms_marker(label, public_url)
            ):
                continue
            seen.add(public_url)
            discovered.append(TermsLink(url=public_url, label=label))
        return discovered

    def scan_document(self, terms_url: str, html: str) -> TermsPolicyResult:
        """Classify terms text as clear or manual-review risk, never legal advice."""
        parser = _parse_html(html)
        text = " ".join(parser.text_parts)
        automation_signal = _AUTOMATION_PATTERN.search(text)

        if automation_signal is None:
            return TermsPolicyResult(
                terms_url=terms_url,
                decision=PreflightDecision.APPROVED,
                signals=[],
                reason=(
                    "No automated-access restriction phrase was identified. "
                    f"{_DISCLAIMER}"
                ),
            )

        if _has_explicit_prohibition(text):
            return TermsPolicyResult(
                terms_url=terms_url,
                decision=PreflightDecision.MANUAL_REVIEW_REQUIRED,
                signals=["potential_automated_access_prohibition"],
                reason=(
                    "Terms text may explicitly prohibit automated access, "
                    "scraping, spiders, robots, or data extraction; a human "
                    f"must review it. {_DISCLAIMER}"
                ),
            )

        return TermsPolicyResult(
            terms_url=terms_url,
            decision=PreflightDecision.MANUAL_REVIEW_REQUIRED,
            signals=["ambiguous_automation_language"],
            reason=(
                "Terms text mentions automated access, scraping, spiders, "
                "robots, or data extraction without an unambiguous policy "
                f"interpretation. {_DISCLAIMER}"
            ),
        )
