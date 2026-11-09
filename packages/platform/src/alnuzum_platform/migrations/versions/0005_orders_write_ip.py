"""a demo write is counted by the address that wrote it, not the one that uploaded the order

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders_runs", sa.Column("write_ip_key", sa.String(64), nullable=True))
    op.create_index("ix_orders_runs_write_ip", "orders_runs", ["write_ip_key"])
    # demo deals written before this column keep counting for an address: the one that uploaded them
    op.execute(
        "UPDATE orders_runs SET write_ip_key = ip_key"
        " WHERE demo AND write_ip_key IS NULL AND (status = 'writing' OR written_at IS NOT NULL)"
    )


def downgrade() -> None:
    op.drop_index("ix_orders_runs_write_ip", "orders_runs")
    op.drop_column("orders_runs", "write_ip_key")
