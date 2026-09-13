"""Add Repository Tag current state and append-only revision history.

Revision ID: 0008_repository_tags
Revises: 0007_commit_catalog
Create Date: 2026-09-03 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_repository_tags"
down_revision: str | None = "0007_commit_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")


def upgrade() -> None:
    _require_postgresql()
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
    op.drop_index(
        "ix_tag_revision_history_tag_observed",
        table_name="tag_revision_history",
        schema=SCHEMA,
    )
    op.drop_table("tag_revision_history", schema=SCHEMA)
    op.drop_index(
        "ix_repository_tags_repository_current",
        table_name="repository_tags",
        schema=SCHEMA,
    )
    op.drop_table("repository_tags", schema=SCHEMA)
