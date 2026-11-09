"""incoming mail: runs of a letter (24 hours, sealed; the steps of a write), routing rules per owner and CRM,
the money they spent, the records the demo created in the test accounts

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("ip_key", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.Uuid, nullable=False),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("demo", sa.Boolean, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sealed", sa.LargeBinary, nullable=True),
        sa.Column("summary", postgresql.JSONB, nullable=True),
        sa.Column("steps", postgresql.JSONB, nullable=True),
        sa.Column("deal_url", sa.String(500), nullable=True),
        sa.Column("error", sa.String(40), nullable=True),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("write_ip_key", sa.String(64), nullable=True),
        sa.Column("writing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erase_after", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_mail_runs_owner_created", "mail_runs", ["owner", "created_at"])
    op.create_index("ix_mail_runs_ip_created", "mail_runs", ["ip_key", "created_at"])
    op.create_index("ix_mail_runs_conn_created", "mail_runs", ["connection_id", "created_at"])
    op.create_index("ix_mail_runs_crm_written", "mail_runs", ["crm", "written_at"])
    op.create_table(
        "mail_rules",
        sa.Column("owner", sa.String(128), primary_key=True),
        sa.Column("target", sa.String(64), primary_key=True),
        sa.Column("rules", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "mail_spend",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("usd", sa.Numeric(10, 5), nullable=False),
        sa.Column("units", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean, nullable=True),
        sa.Column("ip_key", sa.String(64), nullable=True),
    )
    op.create_index("ix_mail_spend_created", "mail_spend", ["created_at"])
    op.create_index("ix_mail_spend_ip_created", "mail_spend", ["ip_key", "created_at"])
    op.create_table(
        "mail_demo_records",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("ip_key", sa.String(64), nullable=True),
    )
    op.create_index("ix_mail_demo_records_crm_created", "mail_demo_records", ["crm", "created_at"])


def downgrade() -> None:
    op.drop_table("mail_demo_records")
    op.drop_table("mail_spend")
    op.drop_table("mail_rules")
    op.drop_table("mail_runs")
