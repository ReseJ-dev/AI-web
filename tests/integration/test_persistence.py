"""Integration tests for portable SQLAlchemy persistence."""

from datetime import UTC

from sqlalchemy.orm import Session

from app.core.database import create_database_engine
from app.models import (
    ComplianceStatus,
    RequestedField,
    ResearchRequest,
    ResearchRun,
)
from app.models.persistence import (
    Base,
    CompanyRecordRow,
    ComplianceDecisionRow,
    ResearchRunRow,
    SkippedSourceRow,
)


def test_sqlite_persists_structured_research_data() -> None:
    """SQLite stores UUIDs, UTC timestamps, enums, and structured JSON."""
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    domain_run = ResearchRun(
        request=ResearchRequest(
            query="data extraction providers",
            requested_fields=[RequestedField(name="Services")],
        )
    )

    with Session(engine) as session:
        run = ResearchRunRow(
            id=domain_run.id,
            request_payload=domain_run.request.model_dump(mode="json"),
            status=domain_run.status,
            created_at=domain_run.created_at,
            updated_at=domain_run.updated_at,
        )
        session.add(run)
        session.flush()
        company = CompanyRecordRow(
            research_run_id=domain_run.id,
            name="Example Ltd",
            website_url="https://example.com",
            services=["research", "data extraction"],
            extracted_fields=[{"name": "location", "value": "Nicosia"}],
        )
        session.add(company)
        session.flush()
        session.add_all(
            [
                ComplianceDecisionRow(
                    company_record_id=company.id,
                    status=ComplianceStatus.APPROVED,
                    reasons=["Relevant service offering"],
                    relevance={"total": 0.9},
                ),
                SkippedSourceRow(
                    research_run_id=domain_run.id,
                    url="https://example.com/robots-blocked",
                    reason="Robots policy denied access",
                ),
            ]
        )
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        stored_run = session.get(ResearchRunRow, run_id)

        assert stored_run is not None
        assert stored_run.request_payload["result_count"] == 10
        assert stored_run.created_at.tzinfo is UTC
        assert session.query(CompanyRecordRow).one().services == [
            "research",
            "data extraction",
        ]
        assert session.query(ComplianceDecisionRow).one().relevance == {"total": 0.9}
        assert session.query(SkippedSourceRow).count() == 1
