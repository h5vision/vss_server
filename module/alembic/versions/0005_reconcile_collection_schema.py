"""Reconcile the deployed legacy collection schema with the current models.

Revision ID: 0005_reconcile_collection
Revises: 0004_collection_core
Create Date: 2026-09-02 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_reconcile_collection"
down_revision: str | None = "0004_collection_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")


def _is_legacy_collection_schema() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(
        "repository_sync_runs",
        schema=SCHEMA,
    )
    return "request_id" not in {column["name"] for column in columns}


def upgrade() -> None:
    _require_postgresql()
    if not _is_legacy_collection_schema():
        return

    _upgrade_sync_runs()
    _upgrade_tracked_branches()
    _upgrade_head_history()
    _upgrade_snapshots()


def _upgrade_sync_runs() -> None:
    op.drop_index(
        "uq_repository_sync_runs_running_per_repository",
        table_name="repository_sync_runs",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_repository_sync_runs_repo_started",
        table_name="repository_sync_runs",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_repository_sync_runs_trigger",
        "repository_sync_runs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_repository_sync_runs_state",
        "repository_sync_runs",
        schema=SCHEMA,
        type_="check",
    )

    op.alter_column(
        "repository_sync_runs",
        "trigger",
        existing_type=sa.String(16),
        type_=sa.String(32),
        schema=SCHEMA,
    )
    op.alter_column(
        "repository_sync_runs",
        "state",
        existing_type=sa.String(16),
        type_=sa.String(32),
        schema=SCHEMA,
    )
    op.add_column(
        "repository_sync_runs",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "repository_sync_runs",
        sa.Column(
            "retryable",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "repository_sync_runs",
        sa.Column("result_json", sa.JSON(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "repository_sync_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )

    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.repository_sync_runs
            SET request_id = sync_run_id,
                trigger = CASE WHEN trigger = 'startup' THEN 'periodic' ELSE trigger END,
                state = CASE WHEN state = 'running' THEN 'failed' ELSE state END,
                reason = COALESCE(
                    reason,
                    CASE
                        WHEN state = 'running' THEN 'COLLECTION_SYNC_LEASE_EXPIRED'
                        ELSE 'COLLECTION_SYNC_LEGACY'
                    END
                ),
                detail = COALESCE(detail, 'Migrated legacy repository sync run.'),
                retryable = (state = 'running'),
                lease_expires_at = COALESCE(finished_at, started_at, now()),
                finished_at = CASE
                    WHEN state = 'running' OR finished_at IS NULL
                    THEN COALESCE(finished_at, started_at, now())
                    ELSE finished_at
                END
            """
        )
    )

    op.alter_column(
        "repository_sync_runs",
        "request_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        schema=SCHEMA,
    )
    op.alter_column(
        "repository_sync_runs",
        "reason",
        existing_type=sa.String(255),
        nullable=False,
        schema=SCHEMA,
    )
    op.alter_column(
        "repository_sync_runs",
        "detail",
        existing_type=sa.Text(),
        nullable=False,
        schema=SCHEMA,
    )
    op.alter_column(
        "repository_sync_runs",
        "lease_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_repository_sync_runs_trigger",
        "repository_sync_runs",
        "trigger IN ('manual', 'periodic')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_repository_sync_runs_state",
        "repository_sync_runs",
        "state IN ('running', 'succeeded', 'failed')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_repository_sync_runs_finished_state",
        "repository_sync_runs",
        "(state = 'running' AND finished_at IS NULL) OR "
        "(state <> 'running' AND finished_at IS NOT NULL)",
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


def _upgrade_tracked_branches() -> None:
    op.drop_constraint(
        "uq_tracked_branches_repo_ref",
        "tracked_branches",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "ck_tracked_branches_branch_ref_prefix",
        "tracked_branches",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_tracked_branches_head_sha_length",
        "tracked_branches",
        schema=SCHEMA,
        type_="check",
    )
    op.create_unique_constraint(
        "uq_tracked_branches_repository_ref",
        "tracked_branches",
        ["repository_id", "branch_ref"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_tracked_branches_vss_project_id",
        "tracked_branches",
        ["vss_project_id"],
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_tracked_branches_current_head_length",
        "tracked_branches",
        "current_head_sha IS NULL OR length(current_head_sha) = 40",
        schema=SCHEMA,
    )


def _upgrade_head_history() -> None:
    op.drop_constraint(
        "fk_branch_head_history_sync_run",
        "branch_head_history",
        schema=SCHEMA,
        type_="foreignkey",
    )
    for name in (
        "ck_branch_head_history_change_type",
        "ck_branch_head_history_previous_sha_length",
        "ck_branch_head_history_observed_sha_length",
        "ck_branch_head_history_observed_sha_presence",
    ):
        op.drop_constraint(
            name,
            "branch_head_history",
            schema=SCHEMA,
            type_="check",
        )

    op.execute(
        sa.text(
            f"""
            INSERT INTO {SCHEMA}.repository_sync_runs (
                sync_run_id,
                request_id,
                repository_id,
                trigger,
                state,
                reason,
                detail,
                retryable,
                result_json,
                started_at,
                lease_expires_at,
                finished_at
            )
            SELECT history.history_id,
                   history.history_id,
                   branch.repository_id,
                   'periodic',
                   'succeeded',
                   'COLLECTION_SYNC_LEGACY',
                   'Migrated legacy branch history without a sync run.',
                   false,
                   NULL,
                   history.observed_at,
                   history.observed_at,
                   history.observed_at
            FROM {SCHEMA}.branch_head_history AS history
            JOIN {SCHEMA}.tracked_branches AS branch
              ON branch.tracked_branch_id = history.tracked_branch_id
            WHERE history.sync_run_id IS NULL
            ON CONFLICT (sync_run_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.branch_head_history
            SET sync_run_id = history_id
            WHERE sync_run_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.branch_head_history
            SET change_type = CASE change_type
                WHEN 'initial' THEN 'created'
                WHEN 'branch_deleted' THEN 'deleted'
                ELSE change_type
            END
            """
        )
    )

    op.alter_column(
        "branch_head_history",
        "sync_run_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_branch_head_history_sync_run_id",
        "branch_head_history",
        "repository_sync_runs",
        ["sync_run_id"],
        ["sync_run_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_branch_head_history_previous_length",
        "branch_head_history",
        "previous_head_sha IS NULL OR length(previous_head_sha) = 40",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_branch_head_history_observed_length",
        "branch_head_history",
        "observed_head_sha IS NULL OR length(observed_head_sha) = 40",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_branch_head_history_change_type",
        "branch_head_history",
        "change_type IN ('created', 'fast_forward', 'rewind', 'deleted', 'recreated')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_branch_head_history_has_revision",
        "branch_head_history",
        "NOT (previous_head_sha IS NULL AND observed_head_sha IS NULL)",
        schema=SCHEMA,
    )


def _upgrade_snapshots() -> None:
    op.drop_index(
        "ix_snapshots_tracked_branch_target",
        table_name="snapshots",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "fk_snapshots_tracked_branch",
        "snapshots",
        schema=SCHEMA,
        type_="foreignkey",
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
    # Revision 0004 in this source tree already defines the reconciled schema. A
    # downgrade therefore only moves Alembic's version marker and keeps that schema.
