"""web: connection account and meta, OAuth states, journal of CRM writes

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CRMS = "crm IN ('amocrm', 'kommo', 'bitrix24', 'zoho', 'odoo')"


def upgrade() -> None:
    op.add_column("crm_connections", sa.Column("account", sa.String(400), server_default="", nullable=False))
    op.add_column(
        "crm_connections",
        sa.Column("meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column("crm_connections", sa.Column("last_error", sa.String(60), nullable=True))
    op.add_column("crm_connections", sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_crm_connections_crm_account", "crm_connections", ["crm", "account"])

    op.create_table(
        "oauth_states",
        sa.Column("id_hash", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("lang", sa.String(5), nullable=False),
        sa.Column("client", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(CRMS, name=op.f("ck_oauth_states_crm")),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_oauth_states")),
    )
    op.create_index(op.f("ix_oauth_states_expires_at"), "oauth_states", ["expires_at"])

    op.create_table(
        "crm_writes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("record_id", sa.String(40), nullable=True),
        sa.Column("record_url", sa.String(500), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("error", sa.String(60), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["crm_connections.id"],
            name=op.f("fk_crm_writes_connection_id_crm_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crm_writes")),
    )
    op.create_index("ix_crm_writes_connection_created", "crm_writes", ["connection_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_crm_writes_connection_created", table_name="crm_writes")
    op.drop_table("crm_writes")
    op.drop_index(op.f("ix_oauth_states_expires_at"), table_name="oauth_states")
    op.drop_table("oauth_states")
    op.drop_index("ix_crm_connections_crm_account", table_name="crm_connections")
    op.drop_column("crm_connections", "checked_at")
    op.drop_column("crm_connections", "last_error")
    op.drop_column("crm_connections", "meta")
    op.drop_column("crm_connections", "account")
