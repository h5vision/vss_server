"""Harden Snapshot persistence constraints and audit fields.

Revision ID: 0002_harden
Revises: 0001_initial
Create Date: 2026-08-27 17:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_harden"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")


def upgrade() -> None:
    _require_postgresql()

    op.drop_constraint(
        "snapshot_deltas_snapshot_id_fkey",
        "snapshot_deltas",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_snapshot_deltas_snapshot_id",
        "snapshot_deltas",
        "snapshots",
        ["snapshot_id"],
        ["snapshot_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "snapshot_attempts_snapshot_id_fkey",
        "snapshot_attempts",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_snapshot_attempts_snapshot_id",
        "snapshot_attempts",
        "snapshots",
        ["snapshot_id"],
        ["snapshot_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )

    op.create_check_constraint(
        "ck_snapshots_base_revision_length",
        "snapshots",
        "length(base_revision) = 40",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshots_target_revision_length",
        "snapshots",
        "length(target_revision) = 40",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshots_source_type",
        "snapshots",
        "source_type IN ('client_local_git', 'remote_clone', 'prior_revision', "
        "'bootstrap_full')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshots_state",
        "snapshots",
        "state IN ('received', 'validated', 'binding_required', 'materializing', "
        "'materialized', 'submitting', 'accepted', 'indexing', 'already_indexed', "
        "'completed', 'rejected', 'failed', 'aborted')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshots_attempt_count",
        "snapshots",
        "attempt_count >= 0",
        schema=SCHEMA,
    )

    op.create_check_constraint(
        "ck_snapshot_deltas_status",
        "snapshot_deltas",
        "status IN ('added', 'modified', 'deleted', 'renamed')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshot_deltas_encoding",
        "snapshot_deltas",
        "encoding = 'utf-8'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshot_deltas_content_storage",
        "snapshot_deltas",
        "NOT (content IS NOT NULL AND content_locator IS NOT NULL)",
        schema=SCHEMA,
    )

    op.create_unique_constraint(
        "uq_snapshot_attempts_snapshot_number",
        "snapshot_attempts",
        ["snapshot_id", "attempt_number"],
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_snapshot_attempts_snapshot_number",
        table_name="snapshot_attempts",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshot_attempts_number",
        "snapshot_attempts",
        "attempt_number >= 1",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshot_attempts_latency",
        "snapshot_attempts",
        "latency_ms IS NULL OR latency_ms >= 0",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_snapshot_attempts_status_code",
        "snapshot_attempts",
        "upstream_status_code IS NULL OR upstream_status_code BETWEEN 100 AND 599",
        schema=SCHEMA,
    )

    op.add_column(
        "audit_logs",
        sa.Column(
            "outcome",
            sa.String(32),
            server_default=sa.text("'succeeded'"),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column("audit_logs", sa.Column("reason", sa.String(255)), schema=SCHEMA)
    op.add_column("audit_logs", sa.Column("detail", sa.Text()), schema=SCHEMA)
    op.add_column("audit_logs", sa.Column("before_json", sa.JSON()), schema=SCHEMA)
    op.add_column("audit_logs", sa.Column("after_json", sa.JSON()), schema=SCHEMA)
    op.create_check_constraint(
        "ck_audit_logs_outcome",
        "audit_logs",
        "outcome IN ('succeeded', 'failed', 'denied')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    _require_postgresql()

    op.drop_constraint("ck_audit_logs_outcome", "audit_logs", schema=SCHEMA, type_="check")
    for column in ("after_json", "before_json", "detail", "reason", "outcome"):
        op.drop_column("audit_logs", column, schema=SCHEMA)

    for constraint in (
        "ck_snapshot_attempts_status_code",
        "ck_snapshot_attempts_latency",
        "ck_snapshot_attempts_number",
    ):
        op.drop_constraint(constraint, "snapshot_attempts", schema=SCHEMA, type_="check")
    op.drop_constraint(
        "uq_snapshot_attempts_snapshot_number",
        "snapshot_attempts",
        schema=SCHEMA,
        type_="unique",
    )
    op.create_index(
        "ix_snapshot_attempts_snapshot_number",
        "snapshot_attempts",
        ["snapshot_id", "attempt_number"],
        schema=SCHEMA,
    )

    for constraint in (
        "ck_snapshot_deltas_content_storage",
        "ck_snapshot_deltas_encoding",
        "ck_snapshot_deltas_status",
    ):
        op.drop_constraint(constraint, "snapshot_deltas", schema=SCHEMA, type_="check")
    for constraint in (
        "ck_snapshots_attempt_count",
        "ck_snapshots_state",
        "ck_snapshots_source_type",
        "ck_snapshots_target_revision_length",
        "ck_snapshots_base_revision_length",
    ):
        op.drop_constraint(constraint, "snapshots", schema=SCHEMA, type_="check")

    op.drop_constraint(
        "fk_snapshot_attempts_snapshot_id",
        "snapshot_attempts",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.create_foreign_key(
        "snapshot_attempts_snapshot_id_fkey",
        "snapshot_attempts",
        "snapshots",
        ["snapshot_id"],
        ["snapshot_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "fk_snapshot_deltas_snapshot_id",
        "snapshot_deltas",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.create_foreign_key(
        "snapshot_deltas_snapshot_id_fkey",
        "snapshot_deltas",
        "snapshots",
        ["snapshot_id"],
        ["snapshot_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="CASCADE",
    )
