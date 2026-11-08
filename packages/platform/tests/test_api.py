"""Every route but /health needs the admin token; answers never carry secrets."""

import base64
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings

KEYS = "k1:" + base64.b64encode(b"\x01" * 32).decode()
TOKEN = "admin-token-for-tests-0123456789abcdef"
WEBHOOK = "https://acme.bitrix24.ru/rest/1/abcdef123456/"


@pytest.fixture
async def client(engine: object, migrated: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(database_url=migrated, secret_keys=KEYS, admin_token=TOKEN))
    async with _client(app) as c:
        yield c


def _client(app: FastAPI) -> "_Ctx":
    return _Ctx(app)


class _Ctx:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def __aenter__(self) -> httpx.AsyncClient:
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)


AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROUTES = [
    ("GET", "/connections?owner=acme"),
    ("POST", "/connections"),
    ("POST", "/connections/00000000-0000-0000-0000-000000000000/disable?owner=acme"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Bearer "}]
)
async def test_routes_without_the_token_are_401(
    client: httpx.AsyncClient, method: str, path: str, headers: dict[str, str]
) -> None:
    resp = await client.request(method, path, headers=headers, json={})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_short_admin_token_closes_the_api(migrated: str) -> None:
    short = "x" * 31
    app = create_app(Settings(database_url=migrated, secret_keys=KEYS, admin_token=short))
    async with _client(app) as client:
        resp = await client.get(
            "/connections", params={"owner": "acme"}, headers={"Authorization": f"Bearer {short}"}
        )
    assert resp.status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_empty_admin_token_closes_the_api(migrated: str, method: str, path: str) -> None:
    app = create_app(Settings(database_url=migrated, secret_keys=KEYS, admin_token=""))
    async with _client(app) as c:
        for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer"}):
            assert (await c.request(method, path, headers=headers, json={})).status_code == 401


async def test_create_list_disable(client: httpx.AsyncClient) -> None:
    body = {"owner": "acme", "crm": "bitrix24", "label": "main", "secrets": {"webhook_url": WEBHOOK}}
    created = await client.post("/connections", json=body, headers=AUTH)
    assert created.status_code == 201
    assert "abcdef123456" not in created.text
    assert "secret" not in created.json()
    conn_id = created.json()["id"]
    listed = await client.get("/connections?owner=acme", headers=AUTH)
    assert [c["id"] for c in listed.json()] == [conn_id]
    assert "abcdef123456" not in listed.text
    assert (await client.get("/connections?owner=other", headers=AUTH)).json() == []
    assert (await client.post(f"/connections/{conn_id}/disable?owner=other", headers=AUTH)).status_code == 404
    assert (await client.post(f"/connections/{conn_id}/disable?owner=acme", headers=AUTH)).status_code == 204
    assert (await client.get("/connections?owner=acme", headers=AUTH)).json()[0]["status"] == "disabled"


@pytest.mark.parametrize(
    "body",
    [
        {
            "owner": "acme",
            "crm": "bitrix24",
            "secrets": {"webhook_url": "https://evil.example.com/rest/1/abcdef123456/"},
        },
        {
            "owner": "acme",
            "crm": "odoo",
            "endpoint": "https://127.0.0.1",
            "secrets": {"database": "d", "login": "l", "api_key": "k"},
        },
        {"owner": "acme", "crm": "amocrm", "endpoint": "evil.example.com", "secrets": {"access_token": "t"}},
    ],
)
async def test_foreign_addresses_are_422(client: httpx.AsyncClient, body: dict[str, object]) -> None:
    assert (await client.post("/connections", json=body, headers=AUTH)).status_code == 422


async def test_without_secret_keys_the_api_is_503(migrated: str) -> None:
    app = create_app(Settings(database_url=migrated, admin_token=TOKEN))
    async with _client(app) as c:
        assert (await c.get("/connections?owner=acme", headers=AUTH)).status_code == 503
