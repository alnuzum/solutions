"""Where an adapter gets its access token, and how it asks for a fresh one after a 401."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from alnuzum_crm.errors import CrmAuthError


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime

    def __repr__(self) -> str:  # tokens never reach logs through repr()
        return f"TokenSet(expires_at={self.expires_at.isoformat()})"

    def expiring(self, margin: timedelta = timedelta(seconds=60)) -> bool:
        return datetime.now(UTC) + margin >= self.expires_at


class TokenSource(Protocol):
    async def token(self) -> str:
        """A token to use now (refreshed ahead of expiry when the source can)."""

    async def refresh(self, stale: str) -> str:
        """A token other than ``stale``, which the CRM has just refused."""


class StaticToken:
    """A long-lived token (amoCRM/Kommo) or a key that cannot be refreshed."""

    def __init__(self, token: str, *, crm: str) -> None:
        self._token = token
        self._crm = crm

    def __repr__(self) -> str:
        return "StaticToken(***)"

    async def token(self) -> str:
        return self._token

    async def refresh(self, stale: str) -> str:
        raise CrmAuthError("token refused and cannot be refreshed", crm=self._crm, status=401)


Refresher = Callable[[TokenSet], Awaitable[TokenSet]]
Saver = Callable[[TokenSet], Awaitable[None]]


class MemoryOAuthToken:
    """OAuth tokens held in memory: one refresh at a time, and a refresh that another caller
    already made is reused instead of spending the (often single-use) refresh token twice."""

    def __init__(self, tokens: TokenSet, refresher: Refresher, *, on_refresh: Saver | None = None) -> None:
        self._tokens = tokens
        self._refresher = refresher
        self._on_refresh = on_refresh
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"MemoryOAuthToken({self._tokens!r})"

    @property
    def current(self) -> TokenSet:
        return self._tokens

    async def token(self) -> str:
        if self._tokens.expiring():
            return await self.refresh(self._tokens.access_token)
        return self._tokens.access_token

    async def refresh(self, stale: str) -> str:
        async with self._lock:
            if self._tokens.access_token != stale and not self._tokens.expiring():
                return self._tokens.access_token
            self._tokens = await self._refresher(self._tokens)
            if self._on_refresh is not None:
                await self._on_refresh(self._tokens)
            return self._tokens.access_token
