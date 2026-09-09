"""Add bounded Repository commit graph catalog and run lease.

Revision ID: 0007_commit_catalog
Revises: 0006_change_request_context
Create Date: 2026-09-02 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_commit_catalog"
down_revision: str | None = "0006_change_request_context"
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
        "repository_commits",
        sa.Column(
            "repository_commit_id",
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
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("tree_sha", sa.String(40), nullable=False),
        sa.Column("author_name", sa.String(255), nullable=True),
        sa.Column("authored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("object_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "repository_id",
            "commit_sha",
            name="uq_repository_commits_repository_sha",
        ),
        sa.CheckConstraint(
            "length(commit_sha) = 40", name="ck_repository_commits_sha_length"
        ),
        sa.CheckConstraint(
            "length(tree_sha) = 40", name="ck_repository_commits_tree_length"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_repository_commits_repository_committed",
        "repository_commits",
        ["repository_id", "committed_at"],
        schema=SCHEMA,
    )

    commit_fk = f"{SCHEMA}.repository_commits.repository_commit_id"
    op.create_table(
        "repository_commit_parents",
        sa.Column(
            "repository_commit_parent_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "repository_commit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(commit_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "parent_commit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(commit_fk, ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("parent_sha", sa.String(40), nullable=False),
        sa.Column("parent_order", sa.Integer(), nullable=False),
        sa.Column("parent_missing_reason", sa.String(32), nullable=True),
        sa.UniqueConstraint(
            "repository_commit_id",
            "parent_order",
            name="uq_repository_commit_parents_order",
        ),
        sa.UniqueConstraint(
            "repository_commit_id",
            "parent_sha",
            name="uq_repository_commit_parents_sha",
        ),
        sa.CheckConstraint(
            "length(parent_sha) = 40", name="ck_repository_commit_parents_sha"
        ),
        sa.CheckConstraint(
            "parent_order >= 0", name="ck_repository_commit_parents_order"
        ),
        sa.CheckConstraint(
            "parent_missing_reason IS NULL OR parent_missing_reason IN "
            "('scan_truncated', 'shallow_history', 'object_unavailable')",
            name="ck_repository_commit_parents_missing_reason",
        ),
        sa.CheckConstraint(
            "(parent_commit_id IS NOT NULL AND parent_missing_reason IS NULL) OR "
            "(parent_commit_id IS NULL AND parent_missing_reason IS NOT NULL)",
            name="ck_repository_commit_parents_resolution",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_repository_commit_parents_parent_sha",
        "repository_commit_parents",
        ["parent_sha"],
        schema=SCHEMA,
    )

    op.create_table(
        "commit_catalog_runs",
        sa.Column(
            "run_id",
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
        sa.Column("state", sa.String(16), server_default="running", nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("roots_json", sa.JSON(), nullable=False),
        sa.Column(
            "unavailable_roots_json",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column("max_commits", sa.Integer(), nullable=False),
        sa.Column("discovered_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("persisted_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("shallow", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("history_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')",
            name="ck_commit_catalog_runs_state",
        ),
        sa.CheckConstraint(
            "max_commits > 0", name="ck_commit_catalog_runs_max_commits"
        ),
        sa.CheckConstraint(
            "discovered_count >= 0 AND persisted_count >= 0",
            name="ck_commit_catalog_runs_counts",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND finished_at IS NULL) OR "
            "(state <> 'running' AND finished_at IS NOT NULL)",
            name="ck_commit_catalog_runs_finished_state",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_commit_catalog_runs_active_repository",
        "commit_catalog_runs",
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_commit_catalog_runs_repository_started",
        "commit_catalog_runs",
        ["repository_id", "started_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    _require_postgresql()
    op.drop_index(
        "ix_commit_catalog_runs_repository_started",
        table_name="commit_catalog_runs",
        schema=SCHEMA,
    )
    op.drop_index(
        "uq_commit_catalog_runs_active_repository",
        table_name="commit_catalog_runs",
        schema=SCHEMA,
    )
    op.drop_table("commit_catalog_runs", schema=SCHEMA)
    op.drop_index(
        "ix_repository_commit_parents_parent_sha",
        table_name="repository_commit_parents",
        schema=SCHEMA,
    )
    op.drop_table("repository_commit_parents", schema=SCHEMA)
    op.drop_index(
        "ix_repository_commits_repository_committed",
        table_name="repository_commits",
        schema=SCHEMA,
    )
    op.drop_table("repository_commits", schema=SCHEMA)
