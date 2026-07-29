"""Auditable command-line workflow for manual source-domain review."""

import argparse
import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from app.core.settings import get_settings
from app.models import DomainInspection, ReviewDecision
from app.services.domain_review import (
    DomainReviewError,
    DomainReviewInspectionService,
    DomainReviewStore,
)

PrintFunction = Callable[[str], None]
InputFunction = Callable[[str], str]


class DomainInspector(Protocol):
    """Inspection behavior used by CLI commands and fakes."""

    async def inspect(self, source: str) -> DomainInspection:
        """Gather public review evidence."""
        ...

    async def aclose(self) -> None:
        """Close inspection resources."""
        ...


InspectionFactory = Callable[[Path], DomainInspector]


def _default_inspection_factory(config_dir: Path) -> DomainInspector:
    return DomainReviewInspectionService(config_dir=config_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-review",
        description=(
            "Inspect public domain-policy evidence and record explicit human "
            "decisions. Results are operational risk signals, not legal advice."
        ),
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=get_settings().source_policy_config_dir,
        help="Directory containing source-policy YAML files.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-domains", help="List effective configured policies.")

    inspect = commands.add_parser(
        "inspect-domain",
        help="Inspect public evidence without changing policy.",
    )
    inspect.add_argument("domain")

    for command, help_text in (
        ("approve-domain", "Explicitly approve one exact domain."),
        ("reject-domain", "Explicitly reject one exact domain."),
        ("remove-domain-decision", "Remove an explicit domain decision."),
    ):
        mutation = commands.add_parser(command, help=help_text)
        mutation.add_argument("domain")
        mutation.add_argument(
            "--reviewer",
            required=True,
            help="Human reviewer identifier stored in the audit record.",
        )
        mutation.add_argument(
            "--note",
            required=True,
            help="Review rationale stored in the audit record.",
        )
    return parser


def _render_inspection(
    inspection: DomainInspection,
    print_fn: PrintFunction,
) -> None:
    print_fn("Domain inspection")
    print_fn("=================")
    print_fn(f"Normalized domain: {inspection.normalized_domain}")
    print_fn(f"Current source-policy status: {inspection.source_policy_status}")
    print_fn(f"Source-policy reason: {inspection.source_policy_reason}")
    print_fn("")
    print_fn("robots.txt result:")
    print_fn(f"  URL: {inspection.robots.robots_url}")
    print_fn(f"  HTTP status: {inspection.robots.http_status}")
    print_fn(f"  Decision: {inspection.robots.decision.value}")
    print_fn(f"  Snapshot hash: {inspection.robots.response_hash or 'unavailable'}")
    print_fn(f"  Reason: {inspection.robots.reason}")
    print_fn("")
    print_fn("Terms-page candidates:")
    if inspection.terms_page_candidates:
        for url in inspection.terms_page_candidates:
            print_fn(f"  - {url}")
    else:
        print_fn("  - none identified")
    print_fn("")
    print_fn("Automated-access risk signals:")
    if inspection.automated_access_risk_signals:
        for signal in inspection.automated_access_risk_signals:
            print_fn(f"  - {signal}")
    else:
        print_fn("  - none identified")
    print_fn("")
    print_fn("Redirect behavior:")
    if inspection.redirects:
        for redirect in inspection.redirects:
            destination = (
                f" -> {redirect.location}" if redirect.location is not None else ""
            )
            print_fn(f"  - HTTP {redirect.http_status} {redirect.url}{destination}")
    else:
        print_fn("  - no homepage response recorded")
    print_fn("")
    print_fn("Proposed public paths:")
    if inspection.proposed_public_paths:
        for url in inspection.proposed_public_paths:
            print_fn(f"  - {url}")
    else:
        print_fn("  - none proposed")
    print_fn("")
    print_fn("Warnings:")
    if inspection.warnings:
        for warning in inspection.warnings:
            print_fn(f"  - {warning}")
    else:
        print_fn("  - none")
    print_fn("")
    print_fn(
        "This inspection provides operational compliance risk signals only; "
        "it is not legal advice."
    )


async def _inspect(
    domain: str,
    *,
    config_dir: Path,
    inspection_factory: InspectionFactory,
) -> DomainInspection:
    inspector = inspection_factory(config_dir)
    try:
        return await inspector.inspect(domain)
    finally:
        await inspector.aclose()


def _decision_for_command(command: str) -> ReviewDecision:
    return {
        "approve-domain": ReviewDecision.APPROVED,
        "reject-domain": ReviewDecision.REJECTED,
        "remove-domain-decision": ReviewDecision.REMOVED,
    }[command]


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: InputFunction = input,
    print_fn: PrintFunction = print,
    inspection_factory: InspectionFactory = _default_inspection_factory,
) -> int:
    """Execute one CLI command and return a process exit code."""
    arguments = _parser().parse_args(argv)
    config_dir = arguments.config_dir.resolve()
    store = DomainReviewStore(config_dir)
    try:
        if arguments.command == "list-domains":
            print_fn("DOMAIN\tSTATUS\tCONFIGURATION")
            for domain, status, origin in store.list_domains():
                print_fn(f"{domain}\t{status}\t{origin}")
            print_fn(
                "Policy statuses are operational controls and are not legal advice."
            )
            return 0

        inspection = asyncio.run(
            _inspect(
                arguments.domain,
                config_dir=config_dir,
                inspection_factory=inspection_factory,
            )
        )
        _render_inspection(inspection, print_fn)
        if arguments.command == "inspect-domain":
            return 0

        decision = _decision_for_command(arguments.command)
        action = {
            ReviewDecision.APPROVED: "approve",
            ReviewDecision.REJECTED: "reject",
            ReviewDecision.REMOVED: "remove",
        }[decision]
        confirmation = f"{action} {inspection.normalized_domain}"
        supplied = input_fn(
            f"Type '{confirmation}' to confirm this configuration change: "
        )
        if supplied.strip() != confirmation:
            print_fn("Confirmation did not match; configuration was not changed.")
            return 1
        record = store.record_decision(
            inspection,
            decision=decision,
            reviewer=arguments.reviewer,
            review_note=arguments.note,
        )
        print_fn(
            f"Recorded {record.decision.value} decision for {record.domain} "
            f"at {record.timestamp.isoformat()}."
        )
        print_fn(
            "This human policy decision is an operational control, not legal advice."
        )
        return 0
    except (DomainReviewError, ValueError) as error:
        print_fn(f"Error: {error}")
        return 2


def run() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


if __name__ == "__main__":
    run()
