"""SQLAlchemy implementations of research persistence contracts."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import SessionLocal
from app.models import (
    CompanyRecord,
    ExtractedField,
    ResearchRequest,
    ResearchRun,
    SkippedSource,
)
from app.models.persistence import CompanyRecordRow, ResearchRunRow, SkippedSourceRow


class SqlAlchemyResearchRunRepository:
    """Persist research-run domain models in short-lived sessions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
    ) -> None:
        self._session_factory = session_factory

    def add(self, run: ResearchRun) -> ResearchRun:
        with self._session_factory() as session:
            session.add(
                ResearchRunRow(
                    id=run.id,
                    request_payload=run.request.model_dump(mode="json"),
                    status=run.status,
                    error_message=run.error_message,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
            )
            session.commit()
        return run

    def get(self, run_id: UUID) -> ResearchRun | None:
        with self._session_factory() as session:
            row = session.get(ResearchRunRow, run_id)
            return self._to_domain(row) if row is not None else None

    def update(self, run: ResearchRun) -> ResearchRun:
        with self._session_factory() as session:
            row = session.get(ResearchRunRow, run.id)
            if row is None:
                session.add(
                    ResearchRunRow(
                        id=run.id,
                        request_payload=run.request.model_dump(mode="json"),
                        status=run.status,
                        error_message=run.error_message,
                        created_at=run.created_at,
                        updated_at=run.updated_at,
                    )
                )
            else:
                row.request_payload = run.request.model_dump(mode="json")
                row.status = run.status
                row.error_message = run.error_message
                row.updated_at = run.updated_at
            session.commit()
        return run

    @staticmethod
    def _to_domain(row: ResearchRunRow) -> ResearchRun:
        return ResearchRun(
            id=row.id,
            request=ResearchRequest.model_validate(row.request_payload),
            status=row.status,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SqlAlchemyCompanyRecordRepository:
    """Persist final independently verified company records."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
    ) -> None:
        self._session_factory = session_factory

    def add(self, company: CompanyRecord) -> CompanyRecord:
        with self._session_factory() as session:
            session.add(
                CompanyRecordRow(
                    id=company.id,
                    research_run_id=company.research_run_id,
                    name=company.name,
                    website_url=(
                        str(company.website_url)
                        if company.website_url is not None
                        else None
                    ),
                    description=company.description,
                    services=company.services,
                    extracted_fields=[
                        field.model_dump(mode="json")
                        for field in company.extracted_fields
                    ],
                    created_at=company.created_at,
                    updated_at=company.updated_at,
                )
            )
            session.commit()
        return company

    def get(self, company_id: UUID) -> CompanyRecord | None:
        with self._session_factory() as session:
            row = session.get(CompanyRecordRow, company_id)
            return self._to_domain(row) if row is not None else None

    def list_for_run(self, run_id: UUID) -> list[CompanyRecord]:
        statement = (
            select(CompanyRecordRow)
            .where(CompanyRecordRow.research_run_id == run_id)
            .order_by(CompanyRecordRow.created_at, CompanyRecordRow.id)
        )
        with self._session_factory() as session:
            return [self._to_domain(row) for row in session.scalars(statement).all()]

    @staticmethod
    def _to_domain(row: CompanyRecordRow) -> CompanyRecord:
        return CompanyRecord(
            id=row.id,
            research_run_id=row.research_run_id,
            name=row.name,
            website_url=row.website_url,
            description=row.description,
            services=row.services,
            extracted_fields=[
                ExtractedField.model_validate(field) for field in row.extracted_fields
            ],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SqlAlchemySkippedSourceRepository:
    """Persist skipped-source audit records."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
    ) -> None:
        self._session_factory = session_factory

    def add(self, source: SkippedSource) -> SkippedSource:
        with self._session_factory() as session:
            session.add(
                SkippedSourceRow(
                    id=source.id,
                    research_run_id=source.research_run_id,
                    url=str(source.url),
                    reason=source.reason,
                    skipped_at=source.skipped_at,
                )
            )
            session.commit()
        return source

    def list_for_run(self, run_id: UUID) -> list[SkippedSource]:
        statement = (
            select(SkippedSourceRow)
            .where(SkippedSourceRow.research_run_id == run_id)
            .order_by(SkippedSourceRow.skipped_at, SkippedSourceRow.id)
        )
        with self._session_factory() as session:
            return [
                SkippedSource(
                    id=row.id,
                    research_run_id=row.research_run_id,
                    url=row.url,
                    reason=row.reason,
                    skipped_at=row.skipped_at,
                )
                for row in session.scalars(statement).all()
            ]
