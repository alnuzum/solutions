"""voice to CRM: the steps of a write already done, so a retry after a failure goes on from the first
step not done and never writes a done one again

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("voice_runs", sa.Column("steps", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("voice_runs", "steps")
