"""CRM connections: stored encrypted, refreshed without races, disabled for good.

Secrets of a connection (one JSON object, encrypted as a whole):

| CRM | keys |
|---|---|
| amocrm, kommo | ``access_token``; with OAuth also ``refresh_token``, ``expires_at`` (ISO 8601) |
| bitrix24 | ``webhook_url`` |
| zoho | ``access_token``, ``refresh_token``, ``expires_at``, ``accounts_server`` |
| odoo | ``database``, ``login``, ``api_key`` |

Every address that came from a user is checked before it is stored: the amoCRM/Kommo domain and
the Zoho hosts against the CRM's own domains, the Bitrix24 webhook against the cloud portals, and
the Odoo address for https and public IPs (and again on every connect, see ``netguard``).
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alnuzum_crm.bitrix24 import parse_webhook
from alnuzum_crm.errors import CrmAuthError
from alnuzum_crm.netguard import check_public_url
from alnuzum_crm.oauth import ZOHO_ACCOUNTS_HOSTS, ZOHO_API_HOSTS, normalize_amo_domain, zoho_host
from alnuzum_crm.tokens import TokenSet
from alnuzum_crm.types import CrmKind
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.models import CrmConnection

SECRET_KEYS: dict[CrmKind, tuple[frozenset[str], frozenset[str]]] = {
    # (required, optional)
    CrmKind.AMOCRM: (frozenset({"access_token"}), frozenset({"refresh_token", "expires_at"})),
    CrmKind.KOMMO: (frozenset({"access_token"}), frozenset({"refresh_token", "expires_at"})),
    CrmKind.BITRIX24: (frozenset({"webhook_url"}), frozenset()),
    CrmKind.ZOHO: (
        frozenset({"access_token", "refresh_token", "expires_at", "accounts_server"}),
        frozenset(),
    ),
    CrmKind.ODOO: (frozenset({"database", "login", "api_key"}), frozenset()),
}

Refresher = Callable[[TokenSet], Awaitable[TokenSet]]


class ConnectionNotFound(LookupError):
    pass


class ConnectionDisabled(RuntimeError):
    """Disabled, or waiting for the user to authorise again."""


@dataclass(frozen=True)
class ConnectionInfo:
    """What may leave the store: no secrets."""

    id: uuid.UUID
    owner: str
    crm: CrmKind
    label: str
    endpoint: str
    status: str
    access_expires_at: datetime | None
    created_at: datetime | None

    @classmethod
    def of(cls, row: CrmConnection) -> "ConnectionInfo":
        return cls(
            row.id,
            row.owner,
            CrmKind(row.crm),
            row.label,
            row.endpoint,
            row.status,
            row.access_expires_at,
            row.created_at,
        )


def _aad(conn_id: uuid.UUID, crm: str) -> bytes:
    return f"crm-connection:{conn_id}:{crm}".encode()


def tokens_of(secrets: dict[str, str]) -> TokenSet:
    expires = secrets.get("expires_at")
    return TokenSet(
        secrets["access_token"],
        secrets.get("refresh_token", ""),
        datetime.fromisoformat(expires) if expires else datetime.max.replace(tzinfo=UTC),
    )


async def validate(crm: CrmKind, endpoint: str, secrets: dict[str, str]) -> str:
    """The endpoint to store (normalised), or ValueError for anything that does not belong."""
    required, optional = SECRET_KEYS[crm]
    keys = set(secrets)
    if not required <= keys or not keys <= required | optional:
        raise ValueError(f"{crm.value} secrets need {sorted(required)} (optional {sorted(optional)})")
    if any(not isinstance(v, str) or not v for v in secrets.values()):
        raise ValueError("secret values are non-empty strings")
    if "expires_at" in secrets:
        try:
            datetime.fromisoformat(secrets["expires_at"])
        except ValueError:
            raise ValueError("expires_at is an ISO 8601 time") from None
    if crm in (CrmKind.AMOCRM, CrmKind.KOMMO):
        return normalize_amo_domain(endpoint, crm)
    if crm is CrmKind.BITRIX24:
        host, _ = parse_webhook(secrets["webhook_url"])
        return host
    if crm is CrmKind.ZOHO:
        zoho_host(secrets["accounts_server"], ZOHO_ACCOUNTS_HOSTS)
        return zoho_host(endpoint, ZOHO_API_HOSTS)
    return await check_public_url(endpoint)


class ConnectionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], keyring: Keyring) -> None:
        self._sessions = sessions
        self._keyring = keyring
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    def _seal(self, row: CrmConnection, secrets: dict[str, str]) -> None:
        row.secret = self._keyring.encrypt(json.dumps(secrets).encode(), _aad(row.id, row.crm))
        row.key_id = self._keyring.primary
        row.access_expires_at = (
            datetime.fromisoformat(secrets["expires_at"]) if "expires_at" in secrets else None
        )

    def _open(self, row: CrmConnection) -> dict[str, str]:
        if row.status != "active" or row.secret is None:
            raise ConnectionDisabled(f"connection {row.id} is {row.status}")
        data: dict[str, str] = json.loads(self._keyring.decrypt(row.secret, _aad(row.id, row.crm)))
        return data

    async def create(
        self, owner: str, crm: CrmKind, endpoint: str, secrets: dict[str, str], *, label: str = ""
    ) -> ConnectionInfo:
        clean = await validate(crm, endpoint, secrets)
        row = CrmConnection(
            id=uuid.uuid4(), owner=owner, crm=crm.value, label=label, endpoint=clean, status="active"
        )
        row.token_version = 0
        self._seal(row, secrets)
        async with self._sessions.begin() as session:
            session.add(row)
        return ConnectionInfo.of(row)

    async def _row(
        self, session: AsyncSession, conn_id: uuid.UUID, owner: str | None, *, lock: bool = False
    ) -> CrmConnection:
        query = select(CrmConnection).where(CrmConnection.id == conn_id)
        if owner is not None:
            query = query.where(CrmConnection.owner == owner)
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None:
            raise ConnectionNotFound(str(conn_id))
        return row

    async def get(self, conn_id: uuid.UUID, owner: str) -> ConnectionInfo:
        async with self._sessions() as session:
            return ConnectionInfo.of(await self._row(session, conn_id, owner))

    async def list(self, owner: str) -> list[ConnectionInfo]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(CrmConnection).where(CrmConnection.owner == owner).order_by(CrmConnection.created_at)
            )
            return [ConnectionInfo.of(r) for r in rows.scalars()]

    async def secrets(self, conn_id: uuid.UUID) -> dict[str, str]:
        async with self._sessions() as session:
            return self._open(await self._row(session, conn_id, None))

    async def disable(self, conn_id: uuid.UUID, owner: str) -> None:
        """For good: the secret is erased, a new connection is needed to come back."""
        async with self._sessions.begin() as session:
            row = await self._row(session, conn_id, owner, lock=True)
            row.status = "disabled"
            row.secret = None
            row.key_id = None
            row.access_expires_at = None
            row.token_version += 1
            row.disabled_at = datetime.now(UTC)

    async def refresh_tokens(self, conn_id: uuid.UUID, stale_access: str, refresher: Refresher) -> TokenSet:
        """New tokens after ``stale_access`` was refused or expired; at most one refresh at a time.

        Refresh tokens of amoCRM are single-use: two workers refreshing at once would leave the
        connection with a dead token. One process waits on a lock, other processes on the row
        lock; whoever comes second finds tokens newer than the ones it saw and reuses them."""
        lock = self._locks.setdefault(conn_id, asyncio.Lock())
        async with lock:
            async with self._sessions.begin() as session:
                row = await self._row(session, conn_id, None, lock=True)
                secrets = self._open(row)
                current = tokens_of(secrets)
                if current.access_token != stale_access and not current.expiring():
                    return current
                try:
                    fresh = await refresher(current)
                except CrmAuthError as exc:
                    refused = exc
                else:
                    secrets.update(
                        access_token=fresh.access_token,
                        refresh_token=fresh.refresh_token,
                        expires_at=fresh.expires_at.isoformat(),
                    )
                    self._seal(row, secrets)
                    row.token_version += 1
                    return fresh
            # the CRM refused the refresh token: the user has to authorise again
            async with self._sessions.begin() as session:
                row = await self._row(session, conn_id, None, lock=True)
                row.status = "reauth_required"
            raise refused

    async def rotate_keys(self) -> int:
        """Re-encrypt every secret that is not under the primary key; returns how many."""
        done = 0
        async with self._sessions.begin() as session:
            rows = await session.execute(
                select(CrmConnection)
                .where(CrmConnection.secret.is_not(None), CrmConnection.key_id != self._keyring.primary)
                .with_for_update()
            )
            for row in rows.scalars():
                data: dict[str, Any] = json.loads(
                    self._keyring.decrypt(row.secret or b"", _aad(row.id, row.crm))
                )
                self._seal(row, data)
                done += 1
        return done

    def token_source(self, conn_id: uuid.UUID, refresher: Refresher | None) -> "StoredToken":
        return StoredToken(self, conn_id, refresher)


class StoredToken:
    """``TokenSource`` of an adapter backed by the store."""

    def __init__(self, store: ConnectionStore, conn_id: uuid.UUID, refresher: Refresher | None) -> None:
        self._store = store
        self._id = conn_id
        self._refresher = refresher

    def __repr__(self) -> str:
        return f"StoredToken({self._id})"

    async def token(self) -> str:
        tokens = tokens_of(await self._store.secrets(self._id))
        if tokens.expiring() and self._refresher is not None:
            return (
                await self._store.refresh_tokens(self._id, tokens.access_token, self._refresher)
            ).access_token
        return tokens.access_token

    async def refresh(self, stale: str) -> str:
        if self._refresher is None:
            raise CrmAuthError("token refused and cannot be refreshed", crm="platform", status=401)
        return (await self._store.refresh_tokens(self._id, stale, self._refresher)).access_token
