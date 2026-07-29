"""Structured metadata and clean visible-text extraction from local HTML."""

import json
import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from pydantic import JsonValue, TypeAdapter, ValidationError

from app.core.settings import get_settings
from app.models import ExtractedPageContent, NavigationLink

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_WHITESPACE = re.compile(r"\s+")
_HIDDEN_STYLE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:[;\s]|$))",
    re.IGNORECASE,
)
_COOKIE_MARKERS = (
    "cookie",
    "consent",
    "gdpr",
    "onetrust",
    "privacy-banner",
    "cmp-banner",
)
_NAVIGATION_MARKERS = (
    "menu",
    "navbar",
    "navigation",
    "site-nav",
    "breadcrumb",
    "sidebar",
)
_HIDDEN_MARKERS = frozenset(
    {"hidden", "d-none", "visually-hidden", "sr-only", "invisible"}
)
_CONTACT_MARKERS = (
    "contact",
    "contact us",
    "get in touch",
    "contacteer",
    "contactgegevens",
)
_ORGANIZATION_TYPES = frozenset(
    {
        "organization",
        "corporation",
        "localbusiness",
        "professionalservice",
    }
)
_TEXT_BLOCK_TAGS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "dt",
    "dd",
    "blockquote",
    "address",
)


def _compact(value: str) -> str:
    """Collapse whitespace in extracted metadata and visible text."""
    return _WHITESPACE.sub(" ", value).strip()


def _tag_tokens(tag: Tag) -> str:
    """Return lowercase id/class tokens for noise classification."""
    identifier = tag.get("id", "")
    classes = tag.get("class", [])
    identifier_text = identifier if isinstance(identifier, str) else ""
    if isinstance(classes, str):
        class_text = classes
    elif isinstance(classes, list):
        class_text = " ".join(str(item) for item in classes)
    else:
        class_text = ""
    return f"{identifier_text} {class_text}".casefold()


def _remove_tags(tags: Iterable[Tag]) -> None:
    """Remove live nodes while tolerating parents removed earlier."""
    for tag in list(tags):
        if tag.parent is not None:
            tag.decompose()


def _http_link(base_url: str, href: object) -> str | None:
    """Resolve and normalize a public HTTP(S) link."""
    if not isinstance(href, str) or not href.strip():
        return None
    resolved = urljoin(base_url, href.strip())
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


def _links_from_tags(
    base_url: str,
    anchors: Iterable[Tag],
) -> list[NavigationLink]:
    """Resolve, compact, and deduplicate anchors."""
    links: list[NavigationLink] = []
    seen: set[str] = set()
    for anchor in anchors:
        resolved = _http_link(base_url, anchor.get("href"))
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        links.append(
            NavigationLink(
                url=resolved,
                text=_compact(anchor.get_text(" ", strip=True))[:1_000],
            )
        )
    return links


def _is_contact_link(link: NavigationLink) -> bool:
    """Recognize English and Dutch contact candidates."""
    label = link.text.casefold().replace("-", " ")
    path = urlsplit(str(link.url)).path.casefold().replace("-", " ")
    return any(marker in label or marker in path for marker in _CONTACT_MARKERS)


def _json_ld_organizations(value: JsonValue) -> list[dict[str, JsonValue]]:
    """Recursively collect JSON-LD nodes representing organizations."""
    organizations: list[dict[str, JsonValue]] = []
    if isinstance(value, list):
        for item in value:
            organizations.extend(_json_ld_organizations(item))
        return organizations
    if not isinstance(value, dict):
        return organizations

    type_value = value.get("@type")
    types: list[str]
    if isinstance(type_value, str):
        types = [type_value]
    elif isinstance(type_value, list):
        types = [item for item in type_value if isinstance(item, str)]
    else:
        types = []
    normalized_types = {item.casefold() for item in types}
    if any(
        item in _ORGANIZATION_TYPES or item.endswith("organization")
        for item in normalized_types
    ):
        organizations.append(value)

    for nested in value.values():
        if isinstance(nested, (dict, list)):
            organizations.extend(_json_ld_organizations(nested))
    return organizations


def _extract_json_ld(soup: BeautifulSoup) -> list[dict[str, JsonValue]]:
    """Parse organization nodes from JSON-LD without retaining raw scripts."""
    organizations: list[dict[str, JsonValue]] = []
    for script in soup.find_all("script"):
        script_type = script.get("type")
        if not isinstance(script_type, str) or (
            script_type.casefold() != "application/ld+json"
        ):
            continue
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            value = _JSON_VALUE_ADAPTER.validate_python(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            continue
        organizations.extend(_json_ld_organizations(value))
    return organizations


def _canonical_url(soup: BeautifulSoup, source_url: str) -> str:
    """Resolve the first valid canonical link or fall back to the source URL."""
    for link in soup.find_all("link"):
        rel = link.get("rel", [])
        rel_values = [rel] if isinstance(rel, str) else rel
        if not isinstance(rel_values, list) or not any(
            str(value).casefold() == "canonical" for value in rel_values
        ):
            continue
        resolved = _http_link(source_url, link.get("href"))
        if resolved is not None:
            return resolved
    return source_url


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    """Read a case-insensitive named meta value."""
    for meta in soup.find_all("meta"):
        meta_name = meta.get("name")
        if isinstance(meta_name, str) and meta_name.casefold() == name.casefold():
            content = meta.get("content")
            if isinstance(content, str) and (compacted := _compact(content)):
                return compacted[:5_000]
    return None


def _open_graph(soup: BeautifulSoup) -> dict[str, str]:
    """Collect compact Open Graph properties."""
    metadata: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        property_name = meta.get("property")
        content = meta.get("content")
        if (
            isinstance(property_name, str)
            and property_name.casefold().startswith("og:")
            and isinstance(content, str)
            and (compacted := _compact(content))
        ):
            metadata[property_name.casefold()] = compacted
    return metadata


def _navigation_links(
    soup: BeautifulSoup,
    source_url: str,
) -> list[NavigationLink]:
    """Collect navigation/header links before visual chrome is removed."""
    anchors: list[Tag] = []
    for container in soup.find_all(["nav", "header"]):
        anchors.extend(container.find_all("a"))
    for container in soup.find_all(attrs={"role": "navigation"}):
        anchors.extend(container.find_all("a"))
    return _links_from_tags(source_url, anchors)


def _remove_noise(soup: BeautifulSoup) -> None:
    """Remove executable, hidden, cookie, menu, footer, and repeated chrome."""
    _remove_tags(
        soup.find_all(
            ["script", "style", "noscript", "template", "svg", "canvas", "iframe"]
        )
    )
    _remove_tags(soup.find_all(["nav", "footer"]))
    _remove_tags(soup.find_all(attrs={"role": "navigation"}))

    noisy: list[Tag] = []
    hidden: list[Tag] = []
    for tag in soup.find_all(True):
        tokens = _tag_tokens(tag)
        if any(marker in tokens for marker in _COOKIE_MARKERS):
            noisy.append(tag)
            continue
        if any(marker in tokens for marker in _NAVIGATION_MARKERS):
            noisy.append(tag)
            continue

        style = tag.get("style", "")
        aria_hidden = tag.get("aria-hidden", "")
        classes = tag.get("class", [])
        class_values = (
            {classes.casefold()}
            if isinstance(classes, str)
            else {str(value).casefold() for value in classes}
            if isinstance(classes, list)
            else set()
        )
        if (
            tag.has_attr("hidden")
            or (isinstance(aria_hidden, str) and aria_hidden.casefold() == "true")
            or (isinstance(style, str) and _HIDDEN_STYLE.search(style))
            or bool(_HIDDEN_MARKERS.intersection(class_values))
            or (tag.name == "input" and str(tag.get("type", "")).casefold() == "hidden")
        ):
            hidden.append(tag)
    _remove_tags(noisy)
    _remove_tags(hidden)


def _deduplicated_text(root: Tag | BeautifulSoup) -> str:
    """Extract semantic text blocks while removing exact repeated chrome."""
    lines: list[str] = []
    seen: set[str] = set()
    visible_blocks = (*_TEXT_BLOCK_TAGS, "div", "section")
    for block in root.find_all(visible_blocks):
        if block.name in {"div", "section"} and block.find(visible_blocks) is not None:
            continue
        text = _compact(block.get_text(" ", strip=True))
        fingerprint = text.casefold()
        if not text or fingerprint in seen:
            continue
        seen.add(fingerprint)
        lines.append(text)
    if lines:
        return "\n".join(lines)
    return _compact(root.get_text(" ", strip=True))


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Bound extracted text at a nearby word boundary for future LLM input."""
    if len(text) <= limit:
        return text, False
    candidate = text[:limit]
    boundary = max(candidate.rfind("\n"), candidate.rfind(" "))
    if boundary >= int(limit * 0.8):
        candidate = candidate[:boundary]
    return candidate.rstrip(), True


class HtmlContentExtractor:
    """Extract structured page metadata and clean, length-bounded text."""

    def __init__(self, max_text_chars: int | None = None) -> None:
        configured_limit = get_settings().html_content_max_chars
        self._max_text_chars = (
            max_text_chars if max_text_chars is not None else configured_limit
        )
        if not 1_000 <= self._max_text_chars <= 200_000:
            raise ValueError("max_text_chars must be between 1000 and 200000")

    def extract(self, source_url: str, html: str) -> ExtractedPageContent:
        """Extract a local HTML document without performing network access."""
        soup = BeautifulSoup(html, "html.parser")
        canonical_url = _canonical_url(soup, source_url)
        title_tag = soup.find("title")
        title = (
            _compact(title_tag.get_text(" ", strip=True))[:2_000]
            if title_tag is not None
            else None
        )
        meta_description = _meta_content(soup, "description")
        open_graph = _open_graph(soup)
        organization_data = _extract_json_ld(soup)
        navigation_links = _navigation_links(soup, source_url)
        all_links = _links_from_tags(source_url, soup.find_all("a"))
        contact_candidates = [link for link in all_links if _is_contact_link(link)]

        _remove_noise(soup)
        main: Tag | BeautifulSoup = soup
        for candidate in (
            soup.find("main"),
            soup.find("article"),
            soup.find(attrs={"role": "main"}),
            soup.body,
        ):
            if isinstance(candidate, Tag):
                main = candidate
                break
        headings = []
        seen_headings: set[str] = set()
        for heading in main.find_all(re.compile(r"^h[1-6]$")):
            text = _compact(heading.get_text(" ", strip=True))
            fingerprint = text.casefold()
            if text and fingerprint not in seen_headings:
                seen_headings.add(fingerprint)
                headings.append(text)

        full_text = _deduplicated_text(main)
        bounded_text, truncated = _truncate_text(
            full_text,
            self._max_text_chars,
        )
        return ExtractedPageContent(
            source_url=source_url,
            canonical_url=canonical_url,
            title=title or None,
            meta_description=meta_description,
            open_graph=open_graph,
            organization_data=organization_data,
            main_text=bounded_text,
            headings=headings,
            navigation_links=navigation_links,
            contact_page_candidates=contact_candidates,
            source_html_length=len(html),
            extracted_text_length=len(bounded_text),
            truncated=truncated,
        )
