"""Create the initial research persistence schema.

Revision ID: 0001
Revises:
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create research runs, company records, decisions, and skipped sources."""
    op.create_table(
        "research_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                name="research_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_runs")),
    )
    op.create_index(
        op.f("ix_research_runs_status"),
        "research_runs",
        ["status"],
        unique=False,
    )

    op.create_table(
        "company_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_run_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("website_url", sa.String(length=2048), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("services", sa.JSON(), nullable=False),
        sa.Column("extracted_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["research_runs.id"],
            name=op.f("fk_company_records_research_run_id_research_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_records")),
    )
    op.create_index(
        op.f("ix_company_records_research_run_id"),
        "company_records",
        ["research_run_id"],
        unique=False,
    )

    op.create_table(
        "compliance_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_record_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "APPROVED",
                "REJECTED",
                "NEEDS_REVIEW",
                name="compliance_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("relevance", sa.JSON(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_record_id"],
            ["company_records.id"],
            name=op.f("fk_compliance_decisions_company_record_id_company_records"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compliance_decisions")),
    )
    op.create_index(
        op.f("ix_compliance_decisions_company_record_id"),
        "compliance_decisions",
        ["company_record_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_compliance_decisions_status"),
        "compliance_decisions",
        ["status"],
        unique=False,
    )

    op.create_table(
        "skipped_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_run_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["research_runs.id"],
            name=op.f("fk_skipped_sources_research_run_id_research_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skipped_sources")),
    )
    op.create_index(
        op.f("ix_skipped_sources_research_run_id"),
        "skipped_sources",
        ["research_run_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the initial research persistence schema."""
    op.drop_index(
        op.f("ix_skipped_sources_research_run_id"),
        table_name="skipped_sources",
    )
    op.drop_table("skipped_sources")
    op.drop_index(
        op.f("ix_compliance_decisions_status"),
        table_name="compliance_decisions",
    )
    op.drop_index(
        op.f("ix_compliance_decisions_company_record_id"),
        table_name="compliance_decisions",
    )
    op.drop_table("compliance_decisions")
    op.drop_index(
        op.f("ix_company_records_research_run_id"),
        table_name="company_records",
    )
    op.drop_table("company_records")
    op.drop_index(op.f("ix_research_runs_status"), table_name="research_runs")
    op.drop_table("research_runs")
