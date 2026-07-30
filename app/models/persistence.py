"""SQLAlchemy persistence models."""

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    MetaData,
    String,
    Text,
    TypeDecorator,
    Uuid,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models.domain import ComplianceStatus, ResearchRunStatus, utc_now


class UtcDateTime(TypeDecorator[datetime]):
    """Persist aware timestamps and always return them normalized to UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Normalize values before sending them to the database."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include timezone information")
        return value.astimezone(UTC)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Restore UTC information that SQLite does not preserve."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative model base used by SQLAlchemy and Alembic."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class ResearchRunRow(Base):
    """Stored research run."""

    __tablename__ = "research_runs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON())
    )
    status: Mapped[ResearchRunStatus] = mapped_column(
        Enum(
            ResearchRunStatus,
            name="research_run_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default=ResearchRunStatus.PENDING,
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        default=utc_now,
        onupdate=utc_now,
    )


class CompanyRecordRow(Base):
    """Stored company research result."""

    __tablename__ = "company_records"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    research_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(300))
    website_url: Mapped[str | None] = mapped_column(String(2_048))
    description: Mapped[str | None] = mapped_column(Text)
    services: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON()),
        default=list,
    )
    extracted_fields: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON()),
        default=list,
    )
    record_metadata: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON()),
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        default=utc_now,
        onupdate=utc_now,
    )


class ComplianceDecisionRow(Base):
    """Stored compliance decision."""

    __tablename__ = "compliance_decisions"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    company_record_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("company_records.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[ComplianceStatus] = mapped_column(
        Enum(
            ComplianceStatus,
            name="compliance_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        index=True,
    )
    reasons: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON()),
        default=list,
    )
    relevance: Mapped[dict[str, Any] | None] = mapped_column(
        MutableDict.as_mutable(JSON())
    )
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)


class SkippedSourceRow(Base):
    """Stored source skipped during a research run."""

    __tablename__ = "skipped_sources"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    research_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str] = mapped_column(String(2_048))
    reason: Mapped[str] = mapped_column(String(1_000))
    skipped_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
