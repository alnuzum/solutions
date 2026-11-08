"""Database models of the platform."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from alnuzum_platform.db import Base

CRMS = ("amocrm", "kommo", "bitrix24", "zoho", "odoo")
STATUSES = ("active", "disabled", "reauth_required")


class CrmConnection(Base):
    """A connection to a customer's CRM. Tokens, keys and webhook URLs live only in ``secret``
    (encrypted); ``endpoint`` holds the CRM address without any secret."""

    __tablename__ = "crm_connections"
    __table_args__ = (
        CheckConstraint(f"crm IN {CRMS}", name="crm"),
        CheckConstraint(f"status IN {STATUSES}", name="status"),
        CheckConstraint("status = 'disabled' OR secret IS NOT NULL", name="secret_present"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    crm: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(200), default="")
    endpoint: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="active")
    secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_id: Mapped[str | None] = mapped_column(String(32))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
