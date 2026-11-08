import asyncio
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_crm.errors import CrmAuthError
from alnuzum_crm.tokens import TokenSet
from alnuzum_crm.types import CrmKind
from alnuzum_platform.crypto import Keyring, SecretError
from alnuzum_platform.db import make_sessionmaker
from alnuzum_platform.models import CrmConnection
from alnuzum_platform.store import ConnectionDisabled, ConnectionNotFound, ConnectionStore

SOON = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
LATER = (datetime.now(UTC) + timedelta(hours=20)).isoformat()


def amo_secrets(access: str = "access-1", refresh: str = "refresh-1", expires: str = SOON) -> dict[str, str]:
    return {"access_token": access, "refresh_token": refresh, "expires_at": expires}


async def test_secret_is_stored_encrypted(store: ConnectionStore, engine: AsyncEngine) -> None:
    info = await store.create("acme", CrmKind.AMOCRM, "acme", amo_secrets("plain-access-token"))
    assert info.endpoint == "acme.amocrm.ru"
    async with engine.connect() as conn:
        raw = (await conn.execute(text("SELECT secret, key_id FROM crm_connections"))).one()
    assert b"plain-access-token" not in raw.secret
    assert raw.key_id == "k2"
    assert (await store.secrets(info.id))["access_token"] == "plain-access-token"
    assert "plain-access-token" not in repr(info)


async def test_owner_sees_only_own_connections(store: ConnectionStore) -> None:
    mine = await store.create("acme", CrmKind.KOMMO, "acme.kommo.com", {"access_token": "t"})
    await store.create("other", CrmKind.KOMMO, "other.kommo.com", {"access_token": "t"})
    assert [c.id for c in await store.list("acme")] == [mine.id]
    assert (await store.get(mine.id, "acme")).crm is CrmKind.KOMMO
    with pytest.raises(ConnectionNotFound):
        await store.get(mine.id, "other")
    with pytest.raises(ConnectionNotFound):
        await store.disable(mine.id, "other")


async def test_secret_blob_swapped_between_rows_does_not_decrypt(
    store: ConnectionStore, engine: AsyncEngine
) -> None:
    a = await store.create("acme", CrmKind.AMOCRM, "a", amo_secrets("token-a"))
    b = await store.create("acme", CrmKind.AMOCRM, "b", amo_secrets("token-b"))
    async with engine.begin() as conn:
        blob = (
            await conn.execute(text("SELECT secret FROM crm_connections WHERE id = :i"), {"i": a.id})
        ).scalar_one()
        await conn.execute(update(CrmConnection).where(CrmConnection.id == b.id).values(secret=blob))
    with pytest.raises(SecretError):
        await store.secrets(b.id)


async def test_disable_erases_the_secret(store: ConnectionStore, engine: AsyncEngine) -> None:
    info = await store.create(
        "acme", CrmKind.BITRIX24, "", {"webhook_url": "https://acme.bitrix24.ru/rest/1/abcdef123456/"}
    )
    assert info.endpoint == "acme.bitrix24.ru"
    await store.disable(info.id, "acme")
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT status, secret, disabled_at FROM crm_connections"))).one()
    assert row.status == "disabled"
    assert row.secret is None
    assert row.disabled_at is not None
    with pytest.raises(ConnectionDisabled):
        await store.secrets(info.id)


@pytest.mark.parametrize(
    ("crm", "endpoint", "secrets", "error"),
    [
        (CrmKind.AMOCRM, "evil.example.com", {"access_token": "t"}, "account domain"),
        (CrmKind.KOMMO, "acme.amocrm.ru", {"access_token": "t"}, "account domain"),
        (CrmKind.BITRIX24, "", {"webhook_url": "https://evil.example.com/rest/1/abcdef123456/"}, "Bitrix24"),
        (
            CrmKind.ZOHO,
            "https://evil.example.com",
            {**amo_secrets(), "accounts_server": "https://accounts.zoho.com"},
            "Zoho",
        ),
        (
            CrmKind.ZOHO,
            "https://www.zohoapis.com",
            {**amo_secrets(), "accounts_server": "https://evil.example.com"},
            "Zoho",
        ),
        (CrmKind.ODOO, "http://odoo.example.com", {"database": "d", "login": "l", "api_key": "k"}, "https"),
        (CrmKind.AMOCRM, "acme", {"access_token": "t", "webhook_url": "x"}, "secrets need"),
        (CrmKind.AMOCRM, "acme", {"access_token": ""}, "non-empty"),
        (CrmKind.AMOCRM, "acme", {"access_token": "t", "expires_at": "tomorrow"}, "ISO"),
    ],
)
async def test_create_refuses_foreign_addresses(
    store: ConnectionStore, crm: CrmKind, endpoint: str, secrets: dict[str, str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        await store.create("acme", crm, endpoint, secrets)


async def test_odoo_on_a_private_address_is_refused(
    store: ConnectionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dns(monkeypatch, "10.0.0.8")
    with pytest.raises(ValueError, match="non-public"):
        await store.create(
            "acme",
            CrmKind.ODOO,
            "https://odoo.internal.example",
            {"database": "d", "login": "l", "api_key": "k"},
        )


async def test_odoo_on_a_public_address_is_stored(
    store: ConnectionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dns(monkeypatch, "93.184.215.14")
    info = await store.create(
        "acme", CrmKind.ODOO, "https://Odoo.Example.com/", {"database": "d", "login": "l", "api_key": "k"}
    )
    assert info.endpoint == "https://odoo.example.com"


def fake_dns(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """Odoo hosts of the test resolve to ``ip``; everything else (the database) as usual."""
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo

    async def fake(host: Any, port: Any, *a: Any, **kw: Any) -> list[Any]:
        if str(host).startswith("odoo."):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return await real(host, port, *a, **kw)

    monkeypatch.setattr(loop, "getaddrinfo", fake)


class CountingRefresher:
    def __init__(self, delay: float = 0.05) -> None:
        self.calls = 0
        self.delay = delay

    async def __call__(self, tokens: TokenSet) -> TokenSet:
        self.calls += 1
        await asyncio.sleep(self.delay)
        n = int(tokens.refresh_token.rsplit("-", 1)[1]) + 1
        return TokenSet(f"access-{n}", f"refresh-{n}", datetime.now(UTC) + timedelta(hours=24))


async def test_concurrent_refresh_in_one_process_spends_the_refresh_token_once(
    store: ConnectionStore,
) -> None:
    info = await store.create("acme", CrmKind.AMOCRM, "acme", amo_secrets())
    refresher = CountingRefresher()
    results = await asyncio.gather(*(store.refresh_tokens(info.id, "access-1", refresher) for _ in range(5)))
    assert refresher.calls == 1
    assert {r.access_token for r in results} == {"access-2"}
    assert (await store.secrets(info.id))["refresh_token"] == "refresh-2"


async def test_concurrent_refresh_across_processes_uses_the_row_lock(
    store: ConnectionStore, migrated: str, keyring: Keyring
) -> None:
    info = await store.create("acme", CrmKind.AMOCRM, "acme", amo_secrets())
    other_engine = create_async_engine(migrated)
    other = ConnectionStore(make_sessionmaker(other_engine), keyring)  # its own in-process locks
    refresher = CountingRefresher(delay=0.2)
    try:
        a, b = await asyncio.gather(
            store.refresh_tokens(info.id, "access-1", refresher),
            other.refresh_tokens(info.id, "access-1", refresher),
        )
    finally:
        await other_engine.dispose()
    assert refresher.calls == 1
    assert a.access_token == b.access_token == "access-2"


async def test_refused_refresh_marks_the_connection_for_reauthorisation(store: ConnectionStore) -> None:
    info = await store.create("acme", CrmKind.AMOCRM, "acme", amo_secrets())

    async def refused(tokens: TokenSet) -> TokenSet:
        raise CrmAuthError("refresh token revoked", crm="amocrm", status=401)

    with pytest.raises(CrmAuthError):
        await store.refresh_tokens(info.id, "access-1", refused)
    assert (await store.get(info.id, "acme")).status == "reauth_required"
    with pytest.raises(ConnectionDisabled):
        await store.secrets(info.id)


async def test_stored_token_refreshes_ahead_of_expiry_and_after_401(store: ConnectionStore) -> None:
    info = await store.create("acme", CrmKind.AMOCRM, "acme", amo_secrets())  # expires in 10 s
    refresher = CountingRefresher(delay=0)
    source = store.token_source(info.id, refresher)
    assert await source.token() == "access-2"
    assert await source.token() == "access-2"
    assert await source.refresh("access-2") == "access-3"
    assert refresher.calls == 2
    assert str(info.id) in repr(source)


async def test_stored_token_without_refresher(store: ConnectionStore) -> None:
    info = await store.create("acme", CrmKind.AMOCRM, "acme", {"access_token": "long-lived"})
    source = store.token_source(info.id, None)
    assert await source.token() == "long-lived"
    with pytest.raises(CrmAuthError):
        await source.refresh("long-lived")


async def test_rotate_keys(store: ConnectionStore, engine: AsyncEngine) -> None:
    old = ConnectionStore(make_sessionmaker(engine), Keyring([("k1", b"\x01" * 32)]))
    info = await old.create("acme", CrmKind.AMOCRM, "acme", amo_secrets("rotated", expires=LATER))
    assert await store.rotate_keys() == 1
    assert await store.rotate_keys() == 0
    only_new = ConnectionStore(make_sessionmaker(engine), Keyring([("k2", b"\x02" * 32)]))
    assert (await only_new.secrets(info.id))["access_token"] == "rotated"


async def test_unknown_connection(store: ConnectionStore) -> None:
    with pytest.raises(ConnectionNotFound):
        await store.secrets(uuid.uuid4())


async def test_adapter_on_a_stored_connection_refreshes_after_401(store: ConnectionStore) -> None:
    """Store and adapter together: a refused token is refreshed once, saved, and the call repeated."""
    import httpx

    from alnuzum_crm.amocrm import AmoCRM
    from alnuzum_crm.types import CompanyIn

    info = await store.create("acme", CrmKind.AMOCRM, "acme", amo_secrets("access-1", "refresh-1", LATER))
    refresher = CountingRefresher(delay=0)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer access-1":
            return httpx.Response(401, json={"title": "Unauthorized"})
        return httpx.Response(204)

    crm = AmoCRM(
        "acme", store.token_source(info.id, refresher), rate=1000, transport=httpx.MockTransport(handler)
    )
    async with crm:
        assert await crm.find_company(CompanyIn("Acme")) is None
    assert seen[:2] == ["Bearer access-1", "Bearer access-2"]
    assert refresher.calls == 1
    assert (await store.secrets(info.id))["refresh_token"] == "refresh-2"
