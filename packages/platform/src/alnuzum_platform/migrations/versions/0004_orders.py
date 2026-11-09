"""order-to-deal solution: runs kept for a day, price rules, the journal of model calls

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09

The tables of every solution live in the one migration chain of the deployment; their models are in
the solution's package (``alnuzum_orders.models``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orders_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False, index=True),
        sa.Column("ip_key", sa.String(64), nullable=False, index=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("crm_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("demo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("lang", sa.String(5), nullable=False, server_default="ru"),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("deal_id", sa.String(40)),
        sa.Column("deal_url", sa.String(500)),
        sa.Column("readback", postgresql.JSONB()),
        sa.Column("error", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("written_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('reading', 'rejected', 'ready', 'writing', 'written', 'failed')", name="status"
        ),
    )
    op.create_index("ix_orders_runs_owner_created", "orders_runs", ["owner", "created_at"])
    op.create_index("ix_orders_runs_ip_created", "orders_runs", ["ip_key", "created_at"])
    op.create_table(
        "orders_rules",
        sa.Column("owner", sa.String(128), primary_key=True),
        sa.Column("target", sa.String(64), primary_key=True),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "orders_model_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True
        ),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("ip_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usd", sa.Numeric(10, 5), nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean()),
    )


def downgrade() -> None:
    op.drop_table("orders_model_calls")
    op.drop_table("orders_rules")
    op.drop_index("ix_orders_runs_ip_created", "orders_runs")
    op.drop_index("ix_orders_runs_owner_created", "orders_runs")
    op.drop_table("orders_runs")
