"""win-back: scans of an account (24 hours, sealed) and the journal of the tasks set on clients

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "winback_scans",
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
        sa.Column("error", sa.String(40), nullable=True),
    )
    op.create_index("ix_winback_scans_owner_created", "winback_scans", ["owner", "created_at"])
    op.create_index("ix_winback_scans_ip_created", "winback_scans", ["ip_key", "created_at"])
    op.create_index("ix_winback_scans_conn_created", "winback_scans", ["connection_id", "created_at"])
    op.create_table(
        "winback_tasks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("ip_key", sa.String(64), nullable=False),
        sa.Column("scan_id", sa.Uuid, nullable=False),
        sa.Column("connection_id", sa.Uuid, nullable=False),
        sa.Column("crm", sa.String(16), nullable=False),
        sa.Column("demo", sa.Boolean, nullable=False),
        sa.Column("client", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("due", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(40), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_winback_tasks_owner_created", "winback_tasks", ["owner", "created_at"])
    op.create_index("ix_winback_tasks_ip_created", "winback_tasks", ["ip_key", "created_at"])
    op.create_index("ix_winback_tasks_crm_created", "winback_tasks", ["crm", "created_at"])
    op.create_index("ix_winback_tasks_client", "winback_tasks", ["connection_id", "client", "status"])
    op.create_index("ix_winback_tasks_task", "winback_tasks", ["crm", "task_id"])


def downgrade() -> None:
    op.drop_table("winback_tasks")
    op.drop_table("winback_scans")
