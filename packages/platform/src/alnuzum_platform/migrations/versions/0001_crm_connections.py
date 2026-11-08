"""crm connections

Revision ID: 0001
Revises:
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crm_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("crm", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("endpoint", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=True),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "crm IN ('amocrm', 'kommo', 'bitrix24', 'zoho', 'odoo')", name=op.f("ck_crm_connections_crm")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'reauth_required')", name=op.f("ck_crm_connections_status")
        ),
        sa.CheckConstraint(
            "status = 'disabled' OR secret IS NOT NULL", name=op.f("ck_crm_connections_secret_present")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crm_connections")),
    )
    op.create_index(op.f("ix_crm_connections_owner"), "crm_connections", ["owner"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_crm_connections_owner"), table_name="crm_connections")
    op.drop_table("crm_connections")
