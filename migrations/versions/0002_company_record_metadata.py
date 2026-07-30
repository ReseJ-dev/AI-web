"""Retain entity-resolution audit metadata on company records.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add portable structured metadata with a non-null empty default."""
    with op.batch_alter_table("company_records") as batch_op:
        batch_op.add_column(
            sa.Column(
                "record_metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    """Remove company record audit metadata."""
    with op.batch_alter_table("company_records") as batch_op:
        batch_op.drop_column("record_metadata")
