"""Tests for bounded domain inspection and confirmed auditable CLI decisions."""

import hashlib
from pathlib import Path

import httpx
import pytest
import yaml

from app.cli.main import main
from app.models import (
    DomainInspection,
    PreflightDecision,
    RedirectObservation,
    ReviewDecision,
    RobotsPolicyRecord,
)
from app.services import SourcePolicyDecision, SourcePolicyService
from app.services.domain_review import (
    DomainReviewEvidenceError,
    DomainReviewInspectionService,
    DomainReviewStore,
)

ROBOTS_TEXT = b"User-agent: *\nAllow: /\n"
ROBOTS_HASH = hashlib.sha256(ROBOTS_TEXT).hexdigest()


def _write_policy_files(
    config_dir: Path,
    *,
    approved: list[str] | None = None,
    blocked: list[str] | None = None,
    candidates: list[str] | None = None,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "approved_domains.yaml").write_text(
        yaml.safe_dump(
            {
                "exact_domains": approved or [],
                "include_subdomains": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "blocked_domains.yaml").write_text(
        yaml.safe_dump(
            {
                "exact_domains": blocked or [],
                "include_subdomains": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "source_policies.yaml").write_text(
        yaml.safe_dump(
            {
                "candidate_review": {
                    "exact_domains": candidates or [],
                    "include_subdomains": [],
                },
                "unknown_domain_decision": "manual_review_required",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "domain_reviews.yaml").write_text(
        "reviews: []\n",
        encoding="utf-8",
    )


def _inspection(domain: str = "agency.example") -> DomainInspection:
    return DomainInspection(
        normalized_domain=domain,
        source_policy_status="manual_review_required",
        source_policy_reason="Candidate domains require manual review.",
        robots=RobotsPolicyRecord(
            robots_url=f"https://{domain}/robots.txt",
            http_status=200,
            requested_path="/",
            decision=PreflightDecision.APPROVED,
            response_hash=ROBOTS_HASH,
            reason="No robots rule disallows the homepage.",
        ),
        terms_page_candidates=[f"https://{domain}/terms"],
        automated_access_risk_signals=[
            f"potential_automated_access_prohibition: https://{domain}/terms"
        ],
        redirects=[
            RedirectObservation(
                url=f"https://{domain}/",
                http_status=200,
            )
        ],
        proposed_public_paths=[
            f"https://{domain}/",
            f"https://{domain}/about",
            f"https://{domain}/services",
        ],
        warnings=["Terms language requires human review."],
    )


@pytest.mark.anyio
async def test_inspection_reports_redirects_terms_risks_and_public_paths(
    tmp_path: Path,
) -> None:
    """Inspection gathers all required evidence without changing policy files."""
    _write_policy_files(tmp_path, candidates=["agency.example"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                content=ROBOTS_TEXT,
                headers={"Content-Type": "text/plain"},
            )
        if request.url.host == "agency.example":
            return httpx.Response(
                301,
                headers={"Location": "https://www.agency.example/"},
            )
        if request.url.path == "/terms":
            return httpx.Response(
                200,
                text=(
                    "<html><body>Automated access and scraping must not be "
                    "performed.</body></html>"
                ),
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(
            200,
            text=(
                "<html><head><title>Agency</title></head><body>"
                "<nav><a href='/about'>About</a>"
                "<a href='/services'>Services</a>"
                "<a href='/terms'>Terms</a></nav></body></html>"
            ),
            headers={"Content-Type": "text/html"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DomainReviewInspectionService(
        config_dir=tmp_path,
        client=client,
        user_agent="ReviewAgent/1.0",
    )

    inspection = await service.inspect("AGENCY.EXAMPLE")

    assert inspection.normalized_domain == "agency.example"
    assert inspection.source_policy_status == "manual_review_required"
    assert inspection.robots.response_hash == ROBOTS_HASH
    assert [str(url) for url in inspection.terms_page_candidates] == [
        "https://www.agency.example/terms"
    ]
    assert any(
        "potential_automated_access_prohibition" in signal
        for signal in inspection.automated_access_risk_signals
    )
    assert [hop.http_status for hop in inspection.redirects] == [301, 200]
    assert str(inspection.redirects[0].location) == "https://www.agency.example/"
    proposed = {str(url) for url in inspection.proposed_public_paths}
    assert "https://www.agency.example/about" in proposed
    assert "https://www.agency.example/services" in proposed
    assert (
        SourcePolicyService(tmp_path).evaluate("agency.example").decision
        is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    )
    await service.aclose()
    await client.aclose()


@pytest.mark.anyio
async def test_inspection_does_not_fetch_pages_when_robots_rejects(
    tmp_path: Path,
) -> None:
    """Review inspection never treats its purpose as a robots-policy bypass."""
    _write_policy_files(tmp_path, candidates=["agency.example"])
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(
            200,
            content=b"User-agent: *\nDisallow: /\n",
            headers={"Content-Type": "text/plain"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DomainReviewInspectionService(
        config_dir=tmp_path,
        client=client,
        user_agent="ReviewAgent/1.0",
    )

    inspection = await service.inspect("agency.example")

    assert requested_paths == ["/robots.txt"]
    assert inspection.robots.decision is PreflightDecision.REJECTED
    assert inspection.terms_page_candidates == []
    assert inspection.redirects == []
    assert any("were not fetched" in warning for warning in inspection.warnings)
    await service.aclose()
    await client.aclose()


def test_store_records_approval_rejection_removal_and_candidate_stays_manual(
    tmp_path: Path,
) -> None:
    """Every mutation is audited and removing it restores candidate review."""
    _write_policy_files(tmp_path, candidates=["agency.example"])
    store = DomainReviewStore(tmp_path)
    inspection = _inspection()

    approved = store.record_decision(
        inspection,
        decision=ReviewDecision.APPROVED,
        reviewer="reviewer@example.test",
        review_note="Public evidence and terms were reviewed.",
    )
    assert approved.domain == "agency.example"
    assert approved.robots_snapshot_hash == ROBOTS_HASH
    assert str(approved.terms_page_url) == "https://agency.example/terms"
    assert approved.timestamp.tzinfo is not None
    assert (
        SourcePolicyService(tmp_path).evaluate("agency.example").decision
        is SourcePolicyDecision.APPROVED
    )

    rejected = store.record_decision(
        inspection,
        decision=ReviewDecision.REJECTED,
        reviewer="security-reviewer",
        review_note="Automated-access restriction requires rejection.",
    )
    assert rejected.decision is ReviewDecision.REJECTED
    assert (
        SourcePolicyService(tmp_path).evaluate("agency.example").decision
        is SourcePolicyDecision.REJECTED
    )

    removed = store.record_decision(
        inspection,
        decision=ReviewDecision.REMOVED,
        reviewer="security-reviewer",
        review_note="Remove the explicit decision pending another review.",
    )
    assert removed.decision is ReviewDecision.REMOVED
    result = SourcePolicyService(tmp_path).evaluate("agency.example")
    assert result.decision is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    assert "candidate" in result.reason
    assert [record.decision for record in store.review_history()] == [
        ReviewDecision.APPROVED,
        ReviewDecision.REJECTED,
        ReviewDecision.REMOVED,
    ]


def test_store_requires_a_real_robots_snapshot_hash(tmp_path: Path) -> None:
    """Policy cannot change when inspection lacks the required robots snapshot."""
    _write_policy_files(tmp_path)
    inspection = _inspection().model_copy(
        update={
            "robots": _inspection().robots.model_copy(update={"response_hash": None})
        }
    )

    with pytest.raises(DomainReviewEvidenceError, match="snapshot hash"):
        DomainReviewStore(tmp_path).record_decision(
            inspection,
            decision=ReviewDecision.APPROVED,
            reviewer="reviewer",
            review_note="No hash is available.",
        )

    assert (
        SourcePolicyService(tmp_path).evaluate("agency.example").decision
        is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    )


class _FakeInspector:
    def __init__(self, inspection: DomainInspection) -> None:
        self.inspection = inspection
        self.closed = False

    async def inspect(self, source: str) -> DomainInspection:
        assert source
        return self.inspection

    async def aclose(self) -> None:
        self.closed = True


def test_cli_lists_and_inspects_without_mutating(tmp_path: Path) -> None:
    """Read-only commands show all evidence headings and candidate status."""
    _write_policy_files(
        tmp_path,
        blocked=["blocked.example"],
        candidates=["agency.example"],
    )
    output: list[str] = []

    list_code = main(
        ["--config-dir", str(tmp_path), "list-domains"],
        print_fn=output.append,
    )
    inspector = _FakeInspector(_inspection())
    inspect_code = main(
        ["--config-dir", str(tmp_path), "inspect-domain", "agency.example"],
        print_fn=output.append,
        inspection_factory=lambda config_dir: inspector,
    )

    rendered = "\n".join(output)
    assert list_code == 0
    assert inspect_code == 0
    assert "agency.example\tmanual_review_required\tcandidate review" in rendered
    assert "Normalized domain: agency.example" in rendered
    assert "Current source-policy status: manual_review_required" in rendered
    assert "robots.txt result:" in rendered
    assert "Terms-page candidates:" in rendered
    assert "Automated-access risk signals:" in rendered
    assert "Redirect behavior:" in rendered
    assert "Proposed public paths:" in rendered
    assert "Warnings:" in rendered
    assert "not legal advice" in rendered
    assert inspector.closed is True
    assert DomainReviewStore(tmp_path).review_history() == []


@pytest.mark.parametrize(
    ("command", "confirmation", "expected"),
    [
        ("approve-domain", "approve agency.example", SourcePolicyDecision.APPROVED),
        ("reject-domain", "reject agency.example", SourcePolicyDecision.REJECTED),
    ],
)
def test_cli_requires_typed_confirmation_and_records_decision(
    tmp_path: Path,
    command: str,
    confirmation: str,
    expected: SourcePolicyDecision,
) -> None:
    """Approval and rejection occur only after exact typed confirmation."""
    _write_policy_files(tmp_path, candidates=["agency.example"])
    inspector = _FakeInspector(_inspection())
    output: list[str] = []

    code = main(
        [
            "--config-dir",
            str(tmp_path),
            command,
            "agency.example",
            "--reviewer",
            "portfolio-reviewer",
            "--note",
            "Reviewed public robots and terms evidence.",
        ],
        input_fn=lambda prompt: confirmation,
        print_fn=output.append,
        inspection_factory=lambda config_dir: inspector,
    )

    assert code == 0
    assert SourcePolicyService(tmp_path).evaluate("agency.example").decision is expected
    history = DomainReviewStore(tmp_path).review_history()
    assert len(history) == 1
    assert history[0].reviewer == "portfolio-reviewer"
    assert history[0].review_note.startswith("Reviewed public")
    assert history[0].robots_snapshot_hash == ROBOTS_HASH


def test_cli_cancellation_and_remove_domain_decision(tmp_path: Path) -> None:
    """Mismatched confirmation is inert; removal is independently confirmed."""
    _write_policy_files(
        tmp_path,
        approved=["agency.example"],
        candidates=["agency.example"],
    )
    inspector = _FakeInspector(_inspection())
    common = [
        "--config-dir",
        str(tmp_path),
        "remove-domain-decision",
        "agency.example",
        "--reviewer",
        "portfolio-reviewer",
        "--note",
        "Return this candidate to manual review.",
    ]

    cancelled = main(
        common,
        input_fn=lambda prompt: "yes",
        print_fn=lambda message: None,
        inspection_factory=lambda config_dir: inspector,
    )
    assert cancelled == 1
    assert (
        SourcePolicyService(tmp_path).evaluate("agency.example").decision
        is SourcePolicyDecision.APPROVED
    )

    removed = main(
        common,
        input_fn=lambda prompt: "remove agency.example",
        print_fn=lambda message: None,
        inspection_factory=lambda config_dir: inspector,
    )
    assert removed == 0
    assert (
        SourcePolicyService(tmp_path).evaluate("agency.example").decision
        is SourcePolicyDecision.MANUAL_REVIEW_REQUIRED
    )
