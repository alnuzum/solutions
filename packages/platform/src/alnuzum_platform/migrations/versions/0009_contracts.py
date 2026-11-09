"""contract check: runs of a comparison (24 hours, sealed), standards per connection (sealed), the money
they spent

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contract_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("ip_key", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.Uuid, nullable=False),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("demo", sa.Boolean, nullable=False),
        sa.Column("standard", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sealed", sa.LargeBinary, nullable=True),
        sa.Column("summary", postgresql.JSONB, nullable=True),
        sa.Column("candidates", postgresql.JSONB, nullable=True),
        sa.Column("deal_id", sa.String(64), nullable=True),
        sa.Column("deal_title", sa.String(300), nullable=True),
        sa.Column("deal_url", sa.String(500), nullable=True),
        sa.Column("error", sa.String(40), nullable=True),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("write_ip_key", sa.String(64), nullable=True),
        sa.Column("steps", postgresql.JSONB, nullable=True),
    )
    op.create_index("ix_contract_runs_owner_created", "contract_runs", ["owner", "created_at"])
    op.create_index("ix_contract_runs_ip_created", "contract_runs", ["ip_key", "created_at"])
    op.create_index("ix_contract_runs_conn_created", "contract_runs", ["connection_id", "created_at"])
    op.create_index("ix_contract_runs_crm_written", "contract_runs", ["crm", "written_at"])
    op.create_table(
        "contract_standards",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("connection_id", sa.Uuid, nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("clauses", sa.Integer, nullable=False),
        sa.Column("sealed", sa.LargeBinary, nullable=False),
        sa.UniqueConstraint("owner", "connection_id", name="uq_contract_standards_owner_conn"),
    )
    op.create_table(
        "contract_spend",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("usd", sa.Numeric(10, 5), nullable=False),
        sa.Column("units", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean, nullable=True),
    )
    op.create_index("ix_contract_spend_created", "contract_spend", ["created_at"])


def downgrade() -> None:
    op.drop_table("contract_spend")
    op.drop_table("contract_standards")
    op.drop_table("contract_runs")
