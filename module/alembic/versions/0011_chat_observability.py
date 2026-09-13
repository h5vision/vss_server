"""Add durable Chat conversation and observability trace persistence.

Revision ID: 0011_chat_observability
Revises: 0010_remove_unused_phase7a
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_chat_observability"
down_revision: str | None = "0010_remove_unused_phase7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "snapshot"


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Snapshot Alembic migrations require PostgreSQL.")


def upgrade() -> None:
    _require_postgresql()

    op.create_table(
        "chat_conversations",
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("requester_type", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("requester_id", sa.String(255), nullable=True),
        sa.Column("client_instance_id", sa.String(255), nullable=True),
        sa.Column("origin", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("project_id", sa.String(255), nullable=True),
        sa.Column("content_capture_mode", sa.String(32), nullable=False, server_default="metadata"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requester_type IN ('authenticated_user','client_instance','admin',"
            "'internal','unknown')",
            name="ck_chat_conversations_requester_type",
        ),
        sa.CheckConstraint(
            "origin IN ('vision_frontend','admin_debug','api','internal_test','unknown')",
            name="ck_chat_conversations_origin",
        ),
        sa.CheckConstraint(
            "content_capture_mode IN ('metadata','question_answer','full_debug')",
            name="ck_chat_conversations_capture_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active','archived')",
            name="ck_chat_conversations_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_conversations_last_message",
        "chat_conversations",
        ["last_message_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_conversations_requester",
        "chat_conversations",
        ["requester_type", "requester_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_conversations_project",
        "chat_conversations",
        ["project_id", "last_message_at"],
        schema=SCHEMA,
    )

    conversation_fk = f"{SCHEMA}.chat_conversations.conversation_id"
    op.create_table(
        "chat_messages",
        sa.Column("message_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(conversation_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("content_length", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requester_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_chat_messages_conversation_sequence"
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_chat_messages_sequence"),
        sa.CheckConstraint(
            "role IN ('user','assistant','system')",
            name="ck_chat_messages_role",
        ),
        sa.CheckConstraint("content_length >= 0", name="ck_chat_messages_content_length"),
        sa.CheckConstraint(
            "content_sha256 IS NULL OR length(content_sha256) = 64",
            name="ck_chat_messages_content_sha256",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_messages_conversation_created",
        "chat_messages",
        ["conversation_id", "created_at"],
        schema=SCHEMA,
    )

    message_fk = f"{SCHEMA}.chat_messages.message_id"
    op.create_table(
        "chat_responses",
        sa.Column("response_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(conversation_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(message_fk, ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assistant_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(message_fk, ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("vss_request_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=True),
        sa.Column("index_id", sa.String(255), nullable=True),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("chat_model", sa.String(255), nullable=True),
        sa.Column("embedding_model", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("has_evidence", sa.Boolean(), nullable=True),
        sa.Column("top_score", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reference_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embed_ms", sa.Float(), nullable=True),
        sa.Column("search_ms", sa.Float(), nullable=True),
        sa.Column("bm25_ms", sa.Float(), nullable=True),
        sa.Column("symbol_ms", sa.Float(), nullable=True),
        sa.Column("prompt_ms", sa.Float(), nullable=True),
        sa.Column("pre_llm_ms", sa.Float(), nullable=True),
        sa.Column("ttft_ms", sa.Float(), nullable=True),
        sa.Column("gen_ms", sa.Float(), nullable=True),
        sa.Column("total_ms", sa.Float(), nullable=True),
        sa.Column("decode_tok_s", sa.Float(), nullable=True),
        sa.Column("eval_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("first_token_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("trace_id", name="uq_chat_responses_trace_id"),
        sa.UniqueConstraint("vss_request_id", name="uq_chat_responses_vss_request_id"),
        sa.CheckConstraint(
            "status IN ('created','running','completed','failed','cancelled')",
            name="ck_chat_responses_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','no_evidence','error')",
            name="ck_chat_responses_outcome",
        ),
        sa.CheckConstraint("source_count >= 0", name="ck_chat_responses_source_count"),
        sa.CheckConstraint("reference_count >= 0", name="ck_chat_responses_reference_count"),
        sa.CheckConstraint(
            "eval_count IS NULL OR eval_count >= 0",
            name="ck_chat_responses_eval_count",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_responses_conversation_started",
        "chat_responses",
        ["conversation_id", "started_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_responses_status_started",
        "chat_responses",
        ["status", "started_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_responses_chat_model",
        "chat_responses",
        ["chat_model", "started_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_responses_embedding_model",
        "chat_responses",
        ["embedding_model", "started_at"],
        schema=SCHEMA,
    )

    response_trace_fk = f"{SCHEMA}.chat_responses.trace_id"
    op.create_table(
        "chat_trace_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "trace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(response_trace_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("elapsed_ms", sa.Float(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("trace_id", "sequence", name="uq_chat_trace_events_trace_sequence"),
        sa.CheckConstraint("sequence >= 0", name="ck_chat_trace_events_sequence"),
        sa.CheckConstraint(
            "event_type IN ('request','runtime_snapshot','meta','stage','delta_batch',"
            "'done','error')",
            name="ck_chat_trace_events_type",
        ),
        sa.CheckConstraint(
            "elapsed_ms IS NULL OR elapsed_ms >= 0",
            name="ck_chat_trace_events_elapsed_ms",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_trace_events_trace_created",
        "chat_trace_events",
        ["trace_id", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "chat_model_observations",
        sa.Column(
            "observation_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "trace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(response_trace_fk, ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("runtime_available", sa.Boolean(), nullable=False),
        sa.Column("resident", sa.Boolean(), nullable=True),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "role IN ('embedding','completion')",
            name="ck_chat_model_observations_role",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_model_observations_trace_observed",
        "chat_model_observations",
        ["trace_id", "observed_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_model_observations_model_observed",
        "chat_model_observations",
        ["model_name", "observed_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    _require_postgresql()
    op.drop_table("chat_model_observations", schema=SCHEMA)
    op.drop_table("chat_trace_events", schema=SCHEMA)
    op.drop_table("chat_responses", schema=SCHEMA)
    op.drop_table("chat_messages", schema=SCHEMA)
    op.drop_table("chat_conversations", schema=SCHEMA)
