"""voice to CRM: the address a paid call came from, so the minutes of speech are bounded per address too

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("voice_spend", sa.Column("ip_key", sa.String(64), nullable=True))
    op.create_index("ix_voice_spend_owner_created", "voice_spend", ["owner", "created_at"])
    op.create_index("ix_voice_spend_ip_created", "voice_spend", ["ip_key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_voice_spend_ip_created", "voice_spend")
    op.drop_index("ix_voice_spend_owner_created", "voice_spend")
    op.drop_column("voice_spend", "ip_key")
