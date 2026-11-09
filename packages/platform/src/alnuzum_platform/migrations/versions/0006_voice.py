"""voice to CRM: runs of a note, the money they spent, one-time links of a Telegram chat, bound chats,
updates already taken

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("ip_key", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=True),
        sa.Column("connection_id", sa.Uuid, nullable=False),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("demo", sa.Boolean, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("readback", postgresql.JSONB, nullable=True),
        sa.Column("deal_url", sa.String(500), nullable=True),
        sa.Column("error", sa.String(40), nullable=True),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_voice_runs_owner_created", "voice_runs", ["owner", "created_at"])
    op.create_index("ix_voice_runs_ip_created", "voice_runs", ["ip_key", "created_at"])
    op.create_index("ix_voice_runs_crm_written", "voice_runs", ["crm", "written_at"])
    op.create_table(
        "voice_spend",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("usd", sa.Numeric(10, 5), nullable=False),
        sa.Column("units", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean, nullable=True),
    )
    op.create_index("ix_voice_spend_created", "voice_spend", ["created_at"])
    op.create_table(
        "voice_links",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("connection_id", sa.Uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "voice_chats",
        sa.Column("chat_id", sa.BigInteger, primary_key=True),
        sa.Column("demo_crm", sa.String(16), nullable=True),
        sa.Column("owner", sa.String(128), nullable=True),
        sa.Column("connection_id", sa.Uuid, nullable=True),
        sa.Column("mode", sa.String(8), nullable=False, server_default="demo"),
        sa.Column("lang", sa.String(2), nullable=False, server_default="ru"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_voice_chats_connection", "voice_chats", ["connection_id"])
    op.create_table(
        "voice_updates",
        sa.Column("update_id", sa.BigInteger, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    for name in ("voice_updates", "voice_chats", "voice_links", "voice_spend", "voice_runs"):
        op.drop_table(name)
