"""Pure dashboard presentation helpers with deterministic output."""

import csv
import io
import re
from collections.abc import Iterable, Sequence

from app.api.schemas import ResearchResultItem, ResearchRunResponse
from app.models import ResearchProgressStage, ResearchRunStatus

FIELD_OPTIONS = {
    "Company name": "company_name",
    "Website": "website_url",
    "Country": "country",
    "Services": "services",
    "Contact page": "contact_page_url",
    "Short summary": "summary",
    "Relevance score": "relevance_score",
}
DEFAULT_FIELDS = list(FIELD_OPTIONS)
DEFAULT_TOPIC = "Shopify agencies in the Netherlands"
DEFAULT_RESULT_COUNT = 30
DEFAULT_COUNTRY_HINT = "Netherlands"
DEFAULT_LANGUAGE_HINT = "en"
_COUNTRY_CODES = {
    "belgium": "BE",
    "canada": "CA",
    "france": "FR",
    "germany": "DE",
    "ireland": "IE",
    "netherlands": "NL",
    "spain": "ES",
    "united kingdom": "GB",
    "united states": "US",
}
_PARENTHETICAL_CODE = re.compile(r"\(([A-Za-z]{2})\)\s*$")
_PROGRESS_STAGES = [
    ResearchProgressStage.PLANNING,
    ResearchProgressStage.SEARCHING,
    ResearchProgressStage.VALIDATING,
    ResearchProgressStage.CHECKING_COMPLIANCE,
    ResearchProgressStage.CRAWLING,
    ResearchProgressStage.EXTRACTING,
    ResearchProgressStage.ENRICHING,
    ResearchProgressStage.DEDUPLICATING,
    ResearchProgressStage.SCORING,
    ResearchProgressStage.EXPORTING,
]


def country_code_from_hint(hint: str) -> str:
    """Resolve a two-letter search country code from a friendly hint."""
    compacted = " ".join(hint.split())
    if len(compacted) == 2 and compacted.isalpha():
        return compacted.upper()
    parenthetical = _PARENTHETICAL_CODE.search(compacted)
    if parenthetical is not None:
        return parenthetical.group(1).upper()
    code = _COUNTRY_CODES.get(compacted.casefold())
    if code is None:
        raise ValueError("Use a supported country name or a two-letter country code.")
    return code


def filter_results(
    items: Sequence[ResearchResultItem],
    minimum_score: int,
) -> list[ResearchResultItem]:
    """Keep records meeting a deterministic relevance threshold."""
    return [
        item
        for item in items
        if (item.relevance_score if item.relevance_score is not None else 0)
        >= minimum_score
    ]


def result_rows(
    items: Iterable[ResearchResultItem],
    selected_labels: Sequence[str],
) -> list[dict[str, str | int]]:
    """Convert typed result objects into a user-selected table projection."""
    rows: list[dict[str, str | int]] = []
    for item in items:
        values: dict[str, str | int] = {
            "Company name": item.company_name,
            "Website": str(item.website or ""),
            "Country": item.country or "",
            "Services": ", ".join(item.services),
            "Contact page": str(item.contact_page or ""),
            "Short summary": item.short_summary or "",
            "Relevance score": item.relevance_score or 0,
        }
        rows.append({label: values[label] for label in selected_labels})
    return rows


def rows_to_csv(rows: Sequence[dict[str, str | int]]) -> bytes:
    """Serialize visible rows without page fragments or internal metadata."""
    if not rows:
        return b""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(
        {
            key: (
                f"'{value}"
                if isinstance(value, str)
                and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    )
    return output.getvalue().encode("utf-8-sig")


def progress_fraction(run: ResearchRunResponse) -> float:
    """Return a stable zero-to-one value from terminal state or latest stage."""
    if run.status in {
        ResearchRunStatus.COMPLETED,
        ResearchRunStatus.FAILED,
        ResearchRunStatus.CANCELLED,
    }:
        return 1.0
    if run.progress_stage is None:
        return 0.02
    try:
        index = _PROGRESS_STAGES.index(run.progress_stage)
    except ValueError:
        return 0.02
    return min(0.95, (index + 1) / (len(_PROGRESS_STAGES) + 1))
