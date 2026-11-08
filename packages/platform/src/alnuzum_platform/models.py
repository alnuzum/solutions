"""Database models of the platform."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alnuzum_platform.db import Base

CRMS = ("amocrm", "kommo", "bitrix24", "zoho", "odoo")
STATUSES = ("active", "disabled", "reauth_required")


class CrmConnection(Base):
    """A connection to a customer's CRM, owned by one browser session (``owner``) and pointing at one
    CRM account (``account``). Tokens, keys and webhook URLs live only in ``secret`` (encrypted);
    ``endpoint`` and ``meta`` hold nothing secret."""

    __tablename__ = "crm_connections"
    __table_args__ = (
        CheckConstraint(f"crm IN {CRMS}", name="crm"),
        CheckConstraint(f"status IN {STATUSES}", name="status"),
        CheckConstraint("status = 'disabled' OR secret IS NOT NULL", name="secret_present"),
        Index("ix_crm_connections_crm_account", "crm", "account"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    crm: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(200), default="")
    endpoint: Mapped[str] = mapped_column(String(300))
    account: Mapped[str] = mapped_column(String(400), default="", server_default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="active")
    last_error: Mapped[str | None] = mapped_column(String(60))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_id: Mapped[str | None] = mapped_column(String(32))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthState(Base):
    """One OAuth window: started by ``owner``, usable once, for 15 minutes. Only a hash of the state
    is stored. amoCRM sends the keys of the integration it creates here (encrypted) before the
    browser comes back."""

    __tablename__ = "oauth_states"
    __table_args__ = (CheckConstraint(f"crm IN {CRMS}", name="crm"),)

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    crm: Mapped[str] = mapped_column(String(16))
    lang: Mapped[str] = mapped_column(String(5), default="ru")
    client: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrmWrite(Base):
    """Journal of what the platform wrote into a CRM: what, where to see it, how it ended. Never a
    secret and never the customer's field values; the daily quota is counted from it."""

    __tablename__ = "crm_writes"
    __table_args__ = (Index("ix_crm_writes_connection_created", "connection_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crm_connections.id", ondelete="CASCADE"))
    owner: Mapped[str] = mapped_column(String(128))
    crm: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(32))
    record_id: Mapped[str | None] = mapped_column(String(40))
    record_url: Mapped[str | None] = mapped_column(String(500))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
