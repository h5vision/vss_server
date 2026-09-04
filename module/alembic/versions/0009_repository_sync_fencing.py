"""Add fencing token lease_generation to repository_sync_runs.

Revision ID: 0009_repository_sync_fencing
Revises: 0008_repository_tags
Create Date: 2026-09-03 16:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_repository_sync_fencing"
down_revision: str | None = "0008_repository_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")


def upgrade() -> None:
    _require_postgresql()
    op.add_column(
        "repository_sync_runs",
        sa.Column(
            "lease_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_repository_sync_runs_lease_generation",
        "repository_sync_runs",
        "lease_generation >= 1",
        schema=SCHEMA,
    )


def downgrade() -> None:
    _require_postgresql()
    op.drop_constraint(
        "ck_repository_sync_runs_lease_generation",
        "repository_sync_runs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column(
        "repository_sync_runs",
        "lease_generation",
        schema=SCHEMA,
    )
