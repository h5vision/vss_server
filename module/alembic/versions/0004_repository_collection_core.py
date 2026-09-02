"""Add user-selected Repository/Branch collection state and history.

Revision ID: 0004_collection_core
Revises: 0003_workspace_id
Create Date: 2026-09-01 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_collection_core"
down_revision: str | None = "0003_workspace_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")


def upgrade() -> None:
    _require_postgresql()

    repository_fk = f"{SCHEMA}.repositories.repository_id"
    op.create_table(
        "tracked_branches",
        sa.Column(
            "tracked_branch_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "repository_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(repository_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("branch_ref", sa.String(512), nullable=False),
        sa.Column("vss_project_id", sa.String(255), nullable=False),
        sa.Column("current_head_sha", sa.String(40), nullable=True),
        sa.Column("tracked", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "repository_id",
            "branch_ref",
            name="uq_tracked_branches_repository_ref",
        ),
        sa.UniqueConstraint(
            "vss_project_id",
            name="uq_tracked_branches_vss_project_id",
        ),
        sa.CheckConstraint(
            "current_head_sha IS NULL OR length(current_head_sha) = 40",
            name="ck_tracked_branches_current_head_length",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_tracked_branches_repository_tracked",
        "tracked_branches",
        ["repository_id", "tracked"],
        schema=SCHEMA,
    )

    op.create_table(
        "repository_sync_runs",
        sa.Column(
            "sync_run_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "repository_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(repository_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), server_default="running", nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('manual', 'periodic')",
            name="ck_repository_sync_runs_trigger",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')",
            name="ck_repository_sync_runs_state",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND finished_at IS NULL) OR "
            "(state <> 'running' AND finished_at IS NOT NULL)",
            name="ck_repository_sync_runs_finished_state",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_repository_sync_runs_active_repository",
        "repository_sync_runs",
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_repository_sync_runs_started",
        "repository_sync_runs",
        ["repository_id", "started_at"],
        schema=SCHEMA,
    )

    tracked_branch_fk = f"{SCHEMA}.tracked_branches.tracked_branch_id"
    sync_run_fk = f"{SCHEMA}.repository_sync_runs.sync_run_id"
    op.create_table(
        "branch_head_history",
        sa.Column(
            "history_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tracked_branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(tracked_branch_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "sync_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(sync_run_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("previous_head_sha", sa.String(40), nullable=True),
        sa.Column("observed_head_sha", sa.String(40), nullable=True),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "previous_head_sha IS NULL OR length(previous_head_sha) = 40",
            name="ck_branch_head_history_previous_length",
        ),
        sa.CheckConstraint(
            "observed_head_sha IS NULL OR length(observed_head_sha) = 40",
            name="ck_branch_head_history_observed_length",
        ),
        sa.CheckConstraint(
            "change_type IN ('created', 'fast_forward', 'rewind', 'deleted', 'recreated')",
            name="ck_branch_head_history_change_type",
        ),
        sa.CheckConstraint(
            "NOT (previous_head_sha IS NULL AND observed_head_sha IS NULL)",
            name="ck_branch_head_history_has_revision",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_branch_head_history_branch_observed",
        "branch_head_history",
        ["tracked_branch_id", "observed_at"],
        schema=SCHEMA,
    )

    op.alter_column("snapshots", "binding_id", nullable=True, schema=SCHEMA)
    op.alter_column("snapshots", "frontend_project_id", nullable=True, schema=SCHEMA)
    op.add_column(
        "snapshots",
        sa.Column("tracked_branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_snapshots_tracked_branch_id",
        "snapshots",
        "tracked_branches",
        ["tracked_branch_id"],
        ["tracked_branch_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_snapshots_exact_source_owner",
        "snapshots",
        "(binding_id IS NOT NULL AND tracked_branch_id IS NULL AND "
        "frontend_project_id IS NOT NULL) OR "
        "(binding_id IS NULL AND tracked_branch_id IS NOT NULL AND "
        "frontend_project_id IS NULL)",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_snapshots_tracked_branch",
        "snapshots",
        ["tracked_branch_id", "created_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    _require_postgresql()

    op.drop_index(
        "ix_snapshots_tracked_branch",
        table_name="snapshots",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_snapshots_exact_source_owner",
        "snapshots",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "fk_snapshots_tracked_branch_id",
        "snapshots",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("snapshots", "tracked_branch_id", schema=SCHEMA)
    op.alter_column("snapshots", "frontend_project_id", nullable=False, schema=SCHEMA)
    op.alter_column("snapshots", "binding_id", nullable=False, schema=SCHEMA)

    op.drop_table("branch_head_history", schema=SCHEMA)
    op.drop_table("repository_sync_runs", schema=SCHEMA)
    op.drop_table("tracked_branches", schema=SCHEMA)
