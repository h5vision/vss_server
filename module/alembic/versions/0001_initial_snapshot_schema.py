"""Initial snapshot schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-27 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def upgrade() -> None:
    # 1. Create schema on PostgreSQL
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"
    target_schema = SCHEMA if is_pg else None

    if is_pg:
        op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    # 2. Table: repositories
    op.create_table(
        "repositories",
        sa.Column(
            "repository_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("canonical_name", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("remote_url", sa.String(2048), nullable=False),
        sa.Column("default_branch_ref", sa.String(512), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("canonical_name", name="uq_repositories_canonical_name"),
        schema=target_schema,
    )

    # 3. Table: branch_bindings
    target_repo_fk = (
        f"{SCHEMA}.repositories.repository_id" if is_pg else "repositories.repository_id"
    )
    op.create_table(
        "branch_bindings",
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("frontend_project_id", sa.String(255), nullable=False),
        sa.Column(
            "repository_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(target_repo_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("branch_ref", sa.String(512), nullable=False),
        sa.Column("vss_project_id", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=target_schema,
    )
    # Partial unique index: at most one active binding per frontend_project_id
    op.create_index(
        "uq_branch_bindings_active_frontend_project",
        "branch_bindings",
        ["frontend_project_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = 1"),
        schema=target_schema,
    )
    op.create_index(
        "ix_branch_bindings_repo_branch",
        "branch_bindings",
        ["repository_id", "branch_ref"],
        schema=target_schema,
    )

    # 4. Table: snapshots
    target_binding_fk = (
        f"{SCHEMA}.branch_bindings.binding_id" if is_pg else "branch_bindings.binding_id"
    )
    op.create_table(
        "snapshots",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "binding_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(target_binding_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("frontend_project_id", sa.String(255), nullable=False),
        sa.Column(
            "repository_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(target_repo_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("branch_ref", sa.String(512), nullable=False),
        sa.Column("vss_project_id", sa.String(255), nullable=False),
        sa.Column("base_revision", sa.String(40), nullable=False),
        sa.Column("target_revision", sa.String(40), nullable=False),
        sa.Column(
            "source_type", sa.String(64), server_default="client_local_git", nullable=False
        ),
        sa.Column("state", sa.String(64), server_default="received", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("materialized_locator", sa.String(1024), nullable=True),
        sa.Column("vss_state", sa.String(64), nullable=True),
        sa.Column("vss_reason", sa.String(255), nullable=True),
        sa.Column("vss_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "vss_project_id",
            "target_revision",
            name="uq_snapshots_vss_project_target_revision",
        ),
        schema=target_schema,
    )
    op.create_index(
        "ix_snapshots_repo_branch_target",
        "snapshots",
        ["repository_id", "branch_ref", "target_revision"],
        schema=target_schema,
    )
    op.create_index(
        "ix_snapshots_state",
        "snapshots",
        ["state"],
        schema=target_schema,
    )
    op.create_index(
        "ix_snapshots_created_at",
        "snapshots",
        ["created_at"],
        schema=target_schema,
    )

    # 5. Table: snapshot_deltas
    target_snapshot_fk = (
        f"{SCHEMA}.snapshots.snapshot_id" if is_pg else "snapshots.snapshot_id"
    )
    op.create_table(
        "snapshot_deltas",
        sa.Column("delta_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(target_snapshot_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("old_path", sa.String(4096), nullable=True),
        sa.Column("encoding", sa.String(32), server_default="utf-8", nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_locator", sa.String(1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=target_schema,
    )
    op.create_index(
        "ix_snapshot_deltas_snapshot_id",
        "snapshot_deltas",
        ["snapshot_id"],
        schema=target_schema,
    )

    # 6. Table: snapshot_attempts
    op.create_table(
        "snapshot_attempts",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(target_snapshot_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upstream_status_code", sa.Integer(), nullable=True),
        sa.Column("vss_state", sa.String(64), nullable=True),
        sa.Column("vss_reason", sa.String(255), nullable=True),
        sa.Column("vss_detail", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("vss_result_json", sa.JSON(), nullable=True),
        schema=target_schema,
    )
    op.create_index(
        "ix_snapshot_attempts_snapshot_number",
        "snapshot_attempts",
        ["snapshot_id", "attempt_number"],
        schema=target_schema,
    )

    # 7. Table: audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=target_schema,
    )
    op.create_index(
        "ix_audit_logs_target",
        "audit_logs",
        ["target_type", "target_id"],
        schema=target_schema,
    )
    op.create_index(
        "ix_audit_logs_created_at",
        "audit_logs",
        ["created_at"],
        schema=target_schema,
    )


def downgrade() -> None:
    conn = op.get_bind()
    schema = SCHEMA if conn.dialect.name == "postgresql" else None

    op.drop_table("audit_logs", schema=schema)
    op.drop_table("snapshot_attempts", schema=schema)
    op.drop_table("snapshot_deltas", schema=schema)
    op.drop_table("snapshots", schema=schema)
    op.drop_table("branch_bindings", schema=schema)
    op.drop_table("repositories", schema=schema)

    # The schema and Alembic version table are deployment-owned. Dropping the
    # schema here would remove alembic_version before Alembic records the
    # downgrade and could also violate the vss_server db_init role boundary.
