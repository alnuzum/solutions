"""the three solutions: when a run began to be written, so a run left "writing" by a stopped process is
released (``failed``) after a while instead of holding its quota for a day

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("orders_runs", "voice_runs", "contract_runs")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("writing_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "writing_at")
