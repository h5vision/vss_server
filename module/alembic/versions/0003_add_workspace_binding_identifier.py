"""Add the explicit Frontend workspace-name binding identifier.

Revision ID: 0003_workspace_id
Revises: 0002_harden
Create Date: 2026-08-27 18:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_workspace_id"
down_revision: str | None = "0002_harden"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")
    op.add_column(
        "branch_bindings",
        sa.Column("frontend_workspace_name", sa.String(255), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_branch_bindings_active_workspace_name",
        "branch_bindings",
        ["frontend_workspace_name"],
        unique=True,
        postgresql_where=sa.text("active = true AND frontend_workspace_name IS NOT NULL"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")
    op.drop_index(
        "uq_branch_bindings_active_workspace_name",
        table_name="branch_bindings",
        schema=SCHEMA,
    )
    op.drop_column("branch_bindings", "frontend_workspace_name", schema=SCHEMA)
