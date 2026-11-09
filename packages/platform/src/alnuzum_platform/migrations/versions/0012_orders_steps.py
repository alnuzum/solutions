"""order to deal: the steps of a write already done (the deal, its note, its task), so a retry after a
failure adds what is missing to the deal it made instead of making a second one

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders_runs", sa.Column("steps", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("orders_runs", "steps")
