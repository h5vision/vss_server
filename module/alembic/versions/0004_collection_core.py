"""Add the Repository·Branch collection core canonical tables.

Revision ID: 0004_collection_core
Revises: 0003_workspace_id
Create Date: 2026-08-31 10:00:00.000000

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


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")

    op.create_table(
        "repository_sync_runs",
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "repository_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.repositories.repository_id",
                ondelete="RESTRICT",
                name="fk_repository_sync_runs_repository",
            ),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="running"),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('manual', 'periodic', 'startup')",
            name="ck_repository_sync_runs_trigger",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')",
            name="ck_repository_sync_runs_state",
        ),
        # 수동·정기 동기화가 동시에 같은 Repository를 수집하지 않도록 실행 중 run을
        # Repository당 하나로 제한한다. PostgreSQL 부분 유니크 인덱스가 프로세스 간
        # 경쟁도 차단한다.
        sa.Index(
            "uq_repository_sync_runs_running_per_repository",
            "repository_id",
            unique=True,
            postgresql_where=sa.text("state = 'running'"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_repository_sync_runs_repo_started",
        "repository_sync_runs",
        ["repository_id", "started_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "tracked_branches",
        sa.Column(
            "tracked_branch_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "repository_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.repositories.repository_id",
                ondelete="RESTRICT",
                name="fk_tracked_branches_repository",
            ),
            nullable=False,
        ),
        sa.Column("branch_ref", sa.String(512), nullable=False),
        sa.Column("vss_project_id", sa.String(255), nullable=False),
        sa.Column("tracked", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("current_head_sha", sa.String(40), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "repository_id",
            "branch_ref",
            name="uq_tracked_branches_repo_ref",
        ),
        sa.CheckConstraint(
            "branch_ref LIKE 'refs/heads/%'",
            name="ck_tracked_branches_branch_ref_prefix",
        ),
        sa.CheckConstraint(
            "current_head_sha IS NULL OR length(current_head_sha) = 40",
            name="ck_tracked_branches_head_sha_length",
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
        "branch_head_history",
        sa.Column("history_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tracked_branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.tracked_branches.tracked_branch_id",
                ondelete="RESTRICT",
                name="fk_branch_head_history_tracked_branch",
            ),
            nullable=False,
        ),
        sa.Column("previous_head_sha", sa.String(40), nullable=True),
        sa.Column("observed_head_sha", sa.String(40), nullable=True),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "sync_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.repository_sync_runs.sync_run_id",
                ondelete="SET NULL",
                name="fk_branch_head_history_sync_run",
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "change_type IN ('initial', 'fast_forward', 'rewind', 'branch_deleted')",
            name="ck_branch_head_history_change_type",
        ),
        sa.CheckConstraint(
            "previous_head_sha IS NULL OR length(previous_head_sha) = 40",
            name="ck_branch_head_history_previous_sha_length",
        ),
        sa.CheckConstraint(
            "observed_head_sha IS NULL OR length(observed_head_sha) = 40",
            name="ck_branch_head_history_observed_sha_length",
        ),
        sa.CheckConstraint(
            "(change_type = 'branch_deleted' AND observed_head_sha IS NULL) OR "
            "(change_type <> 'branch_deleted' AND observed_head_sha IS NOT NULL)",
            name="ck_branch_head_history_observed_sha_presence",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_branch_head_history_branch_observed",
        "branch_head_history",
        ["tracked_branch_id", "observed_at"],
        schema=SCHEMA,
    )

    # 수집 Snapshot은 Frontend binding 없이 tracked_branch로 만들어지므로 기존
    # overlay 전용 NOT NULL 제약을 완화하고 수집 정본 연결을 추가한다.
    op.alter_column(
        "snapshots",
        "binding_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
        schema=SCHEMA,
    )
    op.alter_column(
        "snapshots",
        "frontend_project_id",
        existing_type=sa.String(255),
        nullable=True,
        schema=SCHEMA,
    )
    op.add_column(
        "snapshots",
        sa.Column(
            "tracked_branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.tracked_branches.tracked_branch_id",
                ondelete="RESTRICT",
                name="fk_snapshots_tracked_branch",
            ),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_snapshots_tracked_branch_target",
        "snapshots",
        ["tracked_branch_id", "target_revision"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")

    op.drop_index(
        "ix_snapshots_tracked_branch_target",
        table_name="snapshots",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "fk_snapshots_tracked_branch",
        "snapshots",
        type_="foreignkey",
        schema=SCHEMA,
    )
    op.drop_column("snapshots", "tracked_branch_id", schema=SCHEMA)
    op.alter_column(
        "snapshots",
        "frontend_project_id",
        existing_type=sa.String(255),
        nullable=False,
        schema=SCHEMA,
    )
    op.alter_column(
        "snapshots",
        "binding_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_branch_head_history_branch_observed",
        table_name="branch_head_history",
        schema=SCHEMA,
    )
    op.drop_table("branch_head_history", schema=SCHEMA)
    op.drop_index(
        "ix_tracked_branches_repository_tracked",
        table_name="tracked_branches",
        schema=SCHEMA,
    )
    op.drop_table("tracked_branches", schema=SCHEMA)
    op.drop_index(
        "ix_repository_sync_runs_repo_started",
        table_name="repository_sync_runs",
        schema=SCHEMA,
    )
    op.drop_table("repository_sync_runs", schema=SCHEMA)
