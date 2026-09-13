"""Add provider-neutral PR/MR current state and revision observations.

Revision ID: 0006_change_request_context
Revises: 0005_reconcile_collection
Create Date: 2026-09-02 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_change_request_context"
down_revision: str | None = "0005_reconcile_collection"
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
        "change_requests",
        sa.Column(
            "change_request_id",
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
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("base_ref", sa.String(512), nullable=False),
        sa.Column("head_ref", sa.String(512), nullable=False),
        sa.Column("current_base_sha", sa.String(40), nullable=False),
        sa.Column("current_head_sha", sa.String(40), nullable=False),
        sa.Column("current_merge_sha", sa.String(40), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
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
            "provider",
            "external_number",
            name="uq_change_requests_repository_provider_number",
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab')", name="ck_change_requests_provider"
        ),
        sa.CheckConstraint(
            "kind IN ('pull_request', 'merge_request')", name="ck_change_requests_kind"
        ),
        sa.CheckConstraint(
            "(provider = 'github' AND kind = 'pull_request') OR "
            "(provider = 'gitlab' AND kind = 'merge_request')",
            name="ck_change_requests_provider_kind",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed', 'merged')", name="ck_change_requests_state"
        ),
        sa.CheckConstraint(
            "external_number > 0", name="ck_change_requests_external_number"
        ),
        sa.CheckConstraint(
            "base_ref LIKE 'refs/heads/%' AND head_ref LIKE 'refs/heads/%'",
            name="ck_change_requests_branch_refs",
        ),
        sa.CheckConstraint(
            "length(current_base_sha) = 40", name="ck_change_requests_base_sha_length"
        ),
        sa.CheckConstraint(
            "length(current_head_sha) = 40", name="ck_change_requests_head_sha_length"
        ),
        sa.CheckConstraint(
            "current_merge_sha IS NULL OR length(current_merge_sha) = 40",
            name="ck_change_requests_merge_sha_length",
        ),
        sa.CheckConstraint(
            "(state = 'merged' AND current_merge_sha IS NOT NULL AND merged_at IS NOT NULL) OR "
            "(state <> 'merged' AND current_merge_sha IS NULL AND merged_at IS NULL)",
            name="ck_change_requests_merge_state",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_change_requests_repository_state",
        "change_requests",
        ["repository_id", "state"],
        schema=SCHEMA,
    )

    change_request_fk = f"{SCHEMA}.change_requests.change_request_id"
    op.create_table(
        "change_request_revisions",
        sa.Column(
            "revision_observation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "change_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(change_request_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observation_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("base_ref", sa.String(512), nullable=False),
        sa.Column("head_ref", sa.String(512), nullable=False),
        sa.Column("base_sha", sa.String(40), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("merge_sha", sa.String(40), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "change_request_id",
            "observation_key",
            name="uq_change_request_revisions_observation",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed', 'merged')",
            name="ck_change_request_revisions_state",
        ),
        sa.CheckConstraint(
            "base_ref LIKE 'refs/heads/%' AND head_ref LIKE 'refs/heads/%'",
            name="ck_change_request_revisions_branch_refs",
        ),
        sa.CheckConstraint(
            "length(base_sha) = 40", name="ck_change_request_revisions_base_sha"
        ),
        sa.CheckConstraint(
            "length(head_sha) = 40", name="ck_change_request_revisions_head_sha"
        ),
        sa.CheckConstraint(
            "merge_sha IS NULL OR length(merge_sha) = 40",
            name="ck_change_request_revisions_merge_sha",
        ),
        sa.CheckConstraint(
            "(state = 'merged' AND merge_sha IS NOT NULL) OR "
            "(state <> 'merged' AND merge_sha IS NULL)",
            name="ck_change_request_revisions_merge_state",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_change_request_revisions_request_observed",
        "change_request_revisions",
        ["change_request_id", "observed_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    _require_postgresql()
    op.drop_index(
        "ix_change_request_revisions_request_observed",
        table_name="change_request_revisions",
        schema=SCHEMA,
    )
    op.drop_table("change_request_revisions", schema=SCHEMA)
    op.drop_index(
        "ix_change_requests_repository_state",
        table_name="change_requests",
        schema=SCHEMA,
    )
    op.drop_table("change_requests", schema=SCHEMA)
