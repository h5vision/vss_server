"""Remove unused Phase 7A PR/MR and Repository Tag persistence.

Revision ID: 0010_remove_unused_phase7a
Revises: 0009_repository_sync_fencing
Create Date: 2026-09-09

The production deployment had no rows in these optional tables when this cleanup
was designed. Upgrade refuses to drop them if data appears before deployment.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision: str = "0010_remove_unused_phase7a"
down_revision: str | None = "0009_repository_sync_fencing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"
_REMOVED_TABLES = (
    "change_request_revisions",
    "change_requests",
    "tag_revision_history",
    "repository_tags",
)


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")


def _assert_removed_tables_empty() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in _REMOVED_TABLES:
        count = bind.execute(sa.text(f'SELECT count(*) FROM {SCHEMA}.{table}')).scalar_one()
        if count:
            raise RuntimeError(
                f"Refusing to remove optional Phase 7A table {SCHEMA}.{table}: "
                f"{count} row(s) exist."
            )


def upgrade() -> None:
    _require_postgresql()
    _assert_removed_tables_empty()
    op.drop_table("change_request_revisions", schema=SCHEMA)
    op.drop_table("change_requests", schema=SCHEMA)
    op.drop_table("tag_revision_history", schema=SCHEMA)
    op.drop_table("repository_tags", schema=SCHEMA)


def _restore_change_request_tables() -> None:
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


def _restore_tag_tables() -> None:
    repository_fk = f"{SCHEMA}.repositories.repository_id"
    sync_run_fk = f"{SCHEMA}.repository_sync_runs.sync_run_id"
    op.create_table(
        "repository_tags",
        sa.Column(
            "repository_tag_id",
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
        sa.Column("tag_ref", sa.String(512), nullable=False),
        sa.Column("current_commit_sha", sa.String(40), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
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
            "tag_ref",
            name="uq_repository_tags_repository_ref",
        ),
        sa.CheckConstraint(
            "tag_ref LIKE 'refs/tags/%'", name="ck_repository_tags_ref_prefix"
        ),
        sa.CheckConstraint(
            "current_commit_sha IS NULL OR length(current_commit_sha) = 40",
            name="ck_repository_tags_current_sha",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_repository_tags_repository_current",
        "repository_tags",
        ["repository_id", "current_commit_sha"],
        schema=SCHEMA,
    )

    tag_fk = f"{SCHEMA}.repository_tags.repository_tag_id"
    op.create_table(
        "tag_revision_history",
        sa.Column(
            "tag_history_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "repository_tag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(tag_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "sync_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(sync_run_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("previous_commit_sha", sa.String(40), nullable=True),
        sa.Column("observed_commit_sha", sa.String(40), nullable=True),
        sa.Column("change_type", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "previous_commit_sha IS NULL OR length(previous_commit_sha) = 40",
            name="ck_tag_revision_history_previous_sha",
        ),
        sa.CheckConstraint(
            "observed_commit_sha IS NULL OR length(observed_commit_sha) = 40",
            name="ck_tag_revision_history_observed_sha",
        ),
        sa.CheckConstraint(
            "NOT (previous_commit_sha IS NULL AND observed_commit_sha IS NULL)",
            name="ck_tag_revision_history_has_revision",
        ),
        sa.CheckConstraint(
            "change_type IN ('created', 'moved', 'deleted', 'recreated')",
            name="ck_tag_revision_history_change_type",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_tag_revision_history_tag_observed",
        "tag_revision_history",
        ["repository_tag_id", "observed_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    _require_postgresql()
    _restore_change_request_tables()
    _restore_tag_tables()
