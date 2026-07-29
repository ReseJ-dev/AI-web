"""Tests for advisory terms link and language scanning."""

from app.models import PreflightDecision
from app.services import TermsPolicyScanner


def test_discovers_configured_english_and_dutch_terms_links() -> None:
    """Labels and paths match every configured terms marker."""
    html = """
    <footer>
      <a href="/terms">Terms</a>
      <a href="/terms-of-use">Policy</a>
      <a href="/legal">Legal notice</a>
      <a href="/acceptable-use">Usage policy</a>
      <a href="/voorwaarden">Voorwaarden</a>
      <a href="/gebruiksvoorwaarden">Gebruiksvoorwaarden</a>
      <a href="/privacy">Privacy</a>
      <a href="mailto:terms@example.com">Terms email</a>
    </footer>
    """

    links = TermsPolicyScanner().discover_links(
        "https://example.com/",
        html,
    )

    assert [str(link.url) for link in links] == [
        "https://example.com/terms",
        "https://example.com/terms-of-use",
        "https://example.com/legal",
        "https://example.com/acceptable-use",
        "https://example.com/voorwaarden",
        "https://example.com/gebruiksvoorwaarden",
    ]


def test_explicit_automation_prohibition_is_manual_risk_signal() -> None:
    """Potential prohibitions never become automated legal conclusions."""
    result = TermsPolicyScanner().scan_document(
        "https://example.com/terms",
        """
        <h1>Terms</h1>
        <p>You must not use scraping, spiders, robots, or data extraction.</p>
        """,
    )

    assert result.decision is PreflightDecision.MANUAL_REVIEW_REQUIRED
    assert result.signals == ["potential_automated_access_prohibition"]
    assert "not legal advice" in result.reason


def test_ambiguous_automation_language_requires_manual_review() -> None:
    """Automation vocabulary without clear prohibition remains ambiguous."""
    result = TermsPolicyScanner().scan_document(
        "https://example.com/legal",
        "<p>Our robots and automated access systems organize public pages.</p>",
    )

    assert result.decision is PreflightDecision.MANUAL_REVIEW_REQUIRED
    assert result.signals == ["ambiguous_automation_language"]
    assert "not legal advice" in result.reason


def test_terms_without_automation_language_have_no_risk_signal() -> None:
    """A clear scan means only that no configured signal was found."""
    result = TermsPolicyScanner().scan_document(
        "https://example.com/voorwaarden",
        "<p>Please use this service responsibly and respect copyright.</p>",
    )

    assert result.decision is PreflightDecision.APPROVED
    assert result.signals == []
    assert "not legal advice" in result.reason
