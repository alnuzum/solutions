"""The browser side: sessions, CSRF, ownership, quota, OAuth windows, CRM hooks, journal.

The CRMs are replaced by an ``httpx.MockTransport`` that answers like them (token endpoints, account
lookups, Bitrix24 REST, Odoo JSON-RPC); "Check the connection" talks to a fake adapter.
"""

import base64
import hashlib
import hmac
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from alnuzum_crm.errors import CrmAuthError, CrmValidationError
from alnuzum_crm.types import AccountInfo, CreatedDeal, CrmKind, DealIn
from alnuzum_platform.api import create_app, sweep
from alnuzum_platform.config import Settings
from alnuzum_platform.web.i18n import pick_lang
from alnuzum_platform.web.security import RateLimiter, Sessions

from .test_store import fake_dns

KEYS = "k1:" + base64.b64encode(b"\x01" * 32).decode()
SESSION_SECRET = "session-secret-for-tests-0123456789abcdef"
ORIGIN = "https://apps.test"
WEBHOOK = "https://acme.bitrix24.ru/rest/1/whsecrettoken123456/"
KOMMO_SECRET = "kommo-client-secret-for-tests"
SECRETS_SEEN = (
    "whsecrettoken123456",
    "AT-amo",
    "RT-amo",
    "amo-client-secret",
    "AT-kommo",
    "RT-kommo",
    KOMMO_SECRET,
    "AT-zoho",
    "RT-zoho",
    "zoho-client-secret",
    "odoo-api-key-123",
)


class FakeCRM:
    """Stands in for an adapter in "Check the connection"."""

    calls: list[DealIn]
    fail: Exception | None = None

    def __init__(self) -> None:
        self.calls = []

    async def __aenter__(self) -> "FakeCRM":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def create_deal(self, deal: DealIn) -> CreatedDeal:
        if self.fail is not None:
            raise self.fail
        self.calls.append(deal)
        return CreatedDeal(str(1000 + len(self.calls)))

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        return f"{account.web}/deal/{deal_id}"

    async def account(self) -> AccountInfo:
        if self.fail is not None:
            raise self.fail
        return AccountInfo("x", "x", "https://x")


class Net:
    """What the CRMs answer, and every request the platform sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.odoo_uid: Any = 2

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if path == "/oauth2/access_token" and host.endswith(".amocrm.ru"):
            return httpx.Response(
                200, json={"access_token": "AT-amo", "refresh_token": "RT-amo", "expires_in": 86400}
            )
        if path == "/oauth2/access_token" and host.endswith(".kommo.com"):
            return httpx.Response(
                200, json={"access_token": "AT-kommo", "refresh_token": "RT-kommo", "expires_in": 86400}
            )
        if path == "/api/v4/account":
            return httpx.Response(
                200, json={"id": 4242 if host.endswith("kommo.com") else 777, "name": "Acme"}
            )
        if host.startswith("accounts.zoho") and path == "/oauth/v2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "AT-zoho",
                    "refresh_token": "RT-zoho",
                    "api_domain": "https://www.zohoapis.eu",
                    "expires_in": 3600,
                },
            )
        if host.startswith("accounts.zoho") and path == "/oauth/v2/token/revoke":
            return httpx.Response(200, json={"status": "success"})
        if host == "www.zohoapis.eu" and path == "/crm/v8/org":
            return httpx.Response(
                200, json={"org": [{"id": "9001", "domain_name": "org9001", "company_name": "EU"}]}
            )
        if host.endswith(".bitrix24.ru") and path.endswith("/crm.deal.list.json"):
            return httpx.Response(200, json={"result": [], "total": 0})
        if path == "/jsonrpc":
            body = json.loads(request.content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": self.odoo_uid})
        return httpx.Response(404, json={"error": "unexpected"})

    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests}


def settings(migrated: str, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": migrated,
        "secret_keys": KEYS,
        "session_secret": SESSION_SECRET,
        "public_url": ORIGIN,
        "app_env": "stand",
        "app_version": "abc123",
        "kommo_client_id": "kommo-client-id",
        "kommo_client_secret": KOMMO_SECRET,
        "zoho_client_id": "zoho-client-id",
        "zoho_client_secret": "zoho-client-secret",
        "quota_writes_per_day": 3,
        "rate_per_minute": 10_000,
        "rate_post_per_minute": 10_000,
        "sweep_minutes": 0,
    }
    return Settings(**(base | kw))


class Browser:
    """One browser: its own cookie jar, so two of them are two sessions."""

    def __init__(self, app: FastAPI) -> None:
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)

    async def csrf(self, path: str = "/ru/connect/bitrix24") -> str:
        page = await self.http.get(path)
        found = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert found, f"no csrf on {path}: {page.status_code}"
        return found.group(1)

    async def post(
        self, path: str, data: dict[str, str] | None = None, *, csrf: str | None = None
    ) -> httpx.Response:
        token = csrf if csrf is not None else await self.csrf("/ru/connect/bitrix24")
        return await self.http.post(path, data={"csrf": token} | (data or {}))

    async def connect_bitrix24(self, webhook: str = WEBHOOK) -> str:
        resp = await self.post("/ru/connect/bitrix24", {"webhook_url": webhook})
        assert resp.status_code == 303, resp.text[:300]
        return resp.headers["location"].rsplit("/", 1)[1]


@pytest.fixture
def net() -> Net:
    return Net()


@pytest.fixture
async def make(engine: AsyncEngine, migrated: str, net: Net) -> AsyncIterator[Callable[..., FastAPI]]:
    apps: list[Any] = []

    def build(**kw: Any) -> FastAPI:
        app = create_app(settings(migrated, **kw), transport=httpx.MockTransport(net.handler))
        apps.append(app)
        return app

    started: list[Any] = []

    async def start(app: FastAPI) -> None:
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        started.append(ctx)

    build.start = start  # type: ignore[attr-defined]
    yield build
    for ctx in started:
        await ctx.__aexit__(None, None, None)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM crm_writes"))
        await conn.execute(text("DELETE FROM oauth_states"))


@pytest.fixture
async def app(make: Callable[..., FastAPI]) -> FastAPI:
    built = make()
    await make.start(built)  # type: ignore[attr-defined]
    return built


@pytest.fixture
def fake(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> FakeCRM:
    crm = FakeCRM()
    monkeypatch.setattr(app.state.connectors, "adapter", lambda info, secrets, store: crm)
    return crm


async def rows(engine: AsyncEngine, sql: str) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql))).all())


# --- sessions and headers ---


async def test_home_issues_a_secure_session_cookie_and_strict_headers(app: FastAPI) -> None:
    resp = await Browser(app).http.get("/ru")
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("__Host-an_sid=")
    for flag in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert flag.lower() in cookie.lower()
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "script" not in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert "<script" not in resp.text


async def test_owner_is_a_hash_of_the_session_not_the_session(app: FastAPI, engine: AsyncEngine) -> None:
    b = Browser(app)
    await b.connect_bitrix24()
    sid = Sessions(SESSION_SECRET, secure=True).verify(b.http.cookies["__Host-an_sid"])
    assert sid
    (owner,) = (await rows(engine, "SELECT owner FROM crm_connections"))[0]
    assert owner == "s:" + hashlib.sha256(sid.encode()).hexdigest()[:48]
    assert sid not in owner


def test_cookie_signature_is_checked() -> None:
    s = Sessions(SESSION_SECRET, secure=True)
    sid = s.new_sid()
    good = s.sign(sid)
    assert s.verify(good) == sid
    assert s.verify(good[:-2] + ("AA" if not good.endswith("AA") else "BB")) is None
    assert s.verify(Sessions("another-secret-0123456789abcdefghijkl", secure=True).sign(sid)) is None
    assert s.verify(f"{sid}.") is None
    assert s.verify("x.y.z") is None
    assert s.verify(None) is None
    assert s.verify(f"{'й' * 43}.sig") is None


# --- who may do what ---

ROUTES_WITHOUT_COOKIE = [
    ("GET", "/ru/connections/{id}", 401),
    ("POST", "/ru/connections/{id}/check", 403),
    ("POST", "/ru/connections/{id}/disable", 403),
    ("POST", "/ru/connect/bitrix24", 403),
    ("POST", "/ru/connect/odoo", 403),
    ("GET", "/oauth/zoho/callback?state=s&code=c", 400),
    ("GET", "/oauth/amocrm/callback?state=s&code=c", 400),
    ("GET", "/oauth/kommo/callback?state=s&code=c", 400),
]


@pytest.mark.parametrize(("method", "path", "code"), ROUTES_WITHOUT_COOKIE)
async def test_routes_without_a_session_cookie(app: FastAPI, method: str, path: str, code: int) -> None:
    conn_id = await Browser(app).connect_bitrix24()
    stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    resp = await stranger.request(method, path.format(id=conn_id), data={"csrf": "x"})
    assert resp.status_code == code
    forged = {"__Host-an_sid": Sessions("not-our-secret-0123456789abcdefghijkl", secure=True).sign("a" * 43)}
    resp = await stranger.request(method, path.format(id=conn_id), data={"csrf": "x"}, cookies=forged)
    assert resp.status_code == code


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/ru/connections/{id}"),
        ("POST", "/ru/connections/{id}/check"),
        ("POST", "/ru/connections/{id}/disable"),
        ("GET", "/en/connections/{id}"),
    ],
)
async def test_another_session_gets_404_on_a_foreign_connection(
    app: FastAPI, fake: FakeCRM, engine: AsyncEngine, method: str, path: str
) -> None:
    owner, stranger = Browser(app), Browser(app)
    conn_id = await owner.connect_bitrix24()
    token = await stranger.csrf()
    resp = await stranger.http.request(method, path.format(id=conn_id), data={"csrf": token})
    assert resp.status_code == 404
    assert fake.calls == []
    assert (await rows(engine, "SELECT status FROM crm_connections"))[0][0] == "active"
    home = await stranger.http.get("/ru")
    assert conn_id not in home.text
    assert conn_id in (await owner.http.get("/ru")).text


async def test_unknown_or_malformed_connection_is_404(app: FastAPI) -> None:
    b = Browser(app)
    await b.csrf()
    for conn_id in (str(uuid.uuid4()), "not-a-uuid", "..%2f..%2fetc"):
        assert (await b.http.get(f"/ru/connections/{conn_id}")).status_code == 404
    assert (await b.http.get("/xx")).status_code == 404
    assert (await b.http.get("/ru/connect/hubspot")).status_code == 404
    assert (await b.http.get("/ru/connect/bitrix24/start")).status_code == 404


async def test_csrf_token_and_origin_are_required(app: FastAPI, fake: FakeCRM) -> None:
    b, other = Browser(app), Browser(app)
    conn_id = await b.connect_bitrix24()
    path = f"/ru/connections/{conn_id}/check"
    assert (await b.http.post(path, data={})).status_code == 403
    assert (await b.post(path, csrf="forged")).status_code == 403
    assert (await b.post(path, csrf=await other.csrf())).status_code == 403
    token = await b.csrf()
    evil = await b.http.post(path, data={"csrf": token}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert fake.calls == []
    ok = await b.http.post(path, data={"csrf": token}, headers={"Origin": ORIGIN})
    assert ok.status_code == 303
    assert len(fake.calls) == 1


# --- connect, check, disconnect ---


async def test_bitrix24_connect_check_disconnect(
    app: FastAPI, fake: FakeCRM, engine: AsyncEngine, net: Net
) -> None:
    b = Browser(app)
    conn_id = await b.connect_bitrix24()
    assert any(r.url.path.endswith("/crm.deal.list.json") for r in net.requests)
    page = await b.http.get(f"/ru/connections/{conn_id}")
    assert 'data-status="active"' in page.text
    assert "acme.bitrix24.ru" in page.text
    assert "whsecrettoken" not in page.text

    resp = await b.post(f"/ru/connections/{conn_id}/check", csrf=await b.csrf())
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ru/connections/{conn_id}"
    assert fake.calls[0].tags == ("e2e",)
    assert "e2e" in fake.calls[0].title
    page = await b.http.get(f"/ru/connections/{conn_id}")
    assert 'data-deal="1001"' in page.text
    assert 'href="https://acme.bitrix24.ru/deal/1001"' in page.text
    (journal,) = await rows(engine, "SELECT action, record_id, record_url, ok, error FROM crm_writes")
    assert journal == ("test_deal", "1001", "https://acme.bitrix24.ru/deal/1001", True, None)

    resp = await b.post(f"/ru/connections/{conn_id}/disable", csrf=await b.csrf())
    assert resp.status_code == 303
    (status, secret) = (await rows(engine, "SELECT status, secret FROM crm_connections"))[0]
    assert status == "disabled"
    assert secret is None
    page = await b.http.get(f"/ru/connections/{conn_id}")
    assert 'data-status="disabled"' in page.text
    assert 'data-action="check"' not in page.text
    assert (await b.post(f"/ru/connections/{conn_id}/check", csrf=await b.csrf())).status_code == 303
    assert len(fake.calls) == 1


async def test_daily_quota_per_connection(app: FastAPI, fake: FakeCRM, engine: AsyncEngine) -> None:
    b = Browser(app)
    conn_id = await b.connect_bitrix24()
    token = await b.csrf()
    for _ in range(3):
        assert (
            (await b.post(f"/ru/connections/{conn_id}/check", csrf=token))
            .headers["location"]
            .endswith(conn_id)
        )
    over = await b.post(f"/ru/connections/{conn_id}/check", csrf=token)
    assert over.headers["location"].endswith("?n=quota")
    assert len(fake.calls) == 3
    assert len(await rows(engine, "SELECT 1 FROM crm_writes")) == 3
    page = await b.http.get(over.headers["location"])
    assert 'data-notice="quota"' in page.text
    assert "3" in page.text
    # yesterday's writes do not count
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE crm_writes SET created_at = now() - interval '25 hours'"))
    assert (
        (await b.post(f"/ru/connections/{conn_id}/check", csrf=token)).headers["location"].endswith(conn_id)
    )
    assert len(fake.calls) == 4


async def test_refused_keys_turn_the_connection_to_reconnect(
    app: FastAPI, fake: FakeCRM, engine: AsyncEngine
) -> None:
    b = Browser(app)
    conn_id = await b.connect_bitrix24()
    fake.fail = CrmAuthError("Token has been revoked: AT-amo", crm="bitrix24", status=401)
    resp = await b.post(f"/ru/connections/{conn_id}/check", csrf=await b.csrf())
    assert resp.headers["location"].endswith("?n=auth")
    assert (await rows(engine, "SELECT status, last_error FROM crm_connections"))[0] == (
        "reauth_required",
        "auth",
    )
    assert (await rows(engine, "SELECT ok, error FROM crm_writes"))[0] == (False, "auth")
    page = await b.http.get(f"/ru/connections/{conn_id}")
    assert 'data-status="reauth_required"' in page.text
    assert "AT-amo" not in page.text


async def test_crm_message_never_reaches_the_journal(
    app: FastAPI, fake: FakeCRM, engine: AsyncEngine
) -> None:
    b = Browser(app)
    conn_id = await b.connect_bitrix24()
    fake.fail = CrmValidationError(
        "field TITLE: 'Иванов Иван +998901234567'", crm="bitrix24", code="BAD_TITLE"
    )
    resp = await b.post(f"/ru/connections/{conn_id}/check", csrf=await b.csrf())
    assert resp.headers["location"].endswith("?n=rejected&code=BAD_TITLE")
    assert (await rows(engine, "SELECT error FROM crm_writes"))[0] == ("BAD_TITLE",)
    page = await b.http.get(resp.headers["location"])
    assert "BAD_TITLE" in page.text
    assert "Иванов" not in page.text


async def test_the_same_account_connected_again_replaces_the_old_one(
    app: FastAPI, engine: AsyncEngine
) -> None:
    b = Browser(app)
    first = await b.connect_bitrix24()
    second = await b.connect_bitrix24()
    assert first != second
    found = dict(await rows(engine, "SELECT id::text, status FROM crm_connections"))
    assert found == {first: "disabled", second: "active"}
    page = await b.http.get(f"/ru/connections/{first}")
    assert 'data-notice="replaced"' in page.text


async def test_bitrix24_bad_webhooks_are_refused_without_echo(app: FastAPI, net: Net) -> None:
    b = Browser(app)
    for hook in (
        "https://evil.example/rest/1/whsecrettoken123456/",
        "http://acme.bitrix24.ru/rest/1/whsecrettoken123456/",
        "https://acme.bitrix24.ru/whsecrettoken123456",
    ):
        resp = await b.post("/ru/connect/bitrix24", {"webhook_url": hook})
        assert resp.status_code == 400
        assert "whsecrettoken" not in resp.text
        assert "облачного Битрикс24" in resp.text
    assert net.requests == []
    assert (await b.post("/ru/connect/bitrix24", {"webhook_url": ""})).status_code == 400


async def test_odoo_connect_and_refusals(
    app: FastAPI, net: Net, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dns(monkeypatch, "93.184.215.14")
    b = Browser(app)
    form = {
        "url": "https://odoo.example.com",
        "database": "prod",
        "login": "me@example.com",
        "api_key": "odoo-api-key-123",
    }
    resp = await b.post("/ru/connect/odoo", form)
    assert resp.status_code == 303
    (crm, endpoint, account, _label) = (
        await rows(engine, "SELECT crm, endpoint, account, label FROM crm_connections")
    )[0]
    assert (crm, endpoint, account) == ("odoo", "https://odoo.example.com", "https://odoo.example.com|prod|2")
    net.odoo_uid = False
    refused = await b.post("/en/connect/odoo", form)
    assert refused.status_code == 400
    assert "Odoo refused" in refused.text
    assert "odoo-api-key-123" not in refused.text
    assert 'value="prod"' in refused.text
    missing = await b.post("/ru/connect/odoo", form | {"login": ""})
    assert missing.status_code == 400
    assert "Заполните все поля" in missing.text


# --- OAuth windows ---


def _state(location: str) -> str:
    return parse_qs(urlsplit(location).query)["state"][0]


async def test_amocrm_external_integration_window_and_keys(
    app: FastAPI, net: Net, engine: AsyncEngine
) -> None:
    b = Browser(app)
    await b.csrf()
    start = await b.http.get("/ru/connect/amocrm/start")
    assert start.status_code == 303
    location = start.headers["location"]
    assert location.startswith("https://www.amocrm.ru/oauth/?state=")
    assert location.endswith("&scopes[]=crm&scopes[]=notifications")
    query = parse_qs(urlsplit(location).query)
    assert query["redirect_uri"] == [f"{ORIGIN}/oauth/amocrm/callback"]
    assert query["secrets_uri"] == [f"{ORIGIN}/hooks/amocrm/secrets"]
    assert query["mode"] == ["popup"]
    assert query["origin"] == [ORIGIN]
    assert query["name"] == ["Al-Nuzum"]
    state = _state(location)

    hook = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)  # amoCRM: no cookie
    assert (
        await hook.post(
            "/hooks/amocrm/secrets", json={"state": "unknown", "client_id": "x", "client_secret": "y"}
        )
    ).text == "ok"
    sent = {"state": state, "client_id": "amo-client-id", "client_secret": "amo-client-secret"}
    assert (await hook.post("/hooks/amocrm/secrets", data=sent)).status_code == 200
    # a second delivery does not replace the first
    await hook.post("/hooks/amocrm/secrets", json=sent | {"client_id": "other", "client_secret": "other"})

    params = {
        "code": "c0de",
        "state": state,
        "referer": "acme.amocrm.ru",
        "client_id": "amo-client-id",
        "platform": "1",
    }
    done = await b.http.get("/oauth/amocrm/callback", params=params)
    assert done.status_code == 303
    assert done.headers["location"].startswith("/ru/connections/")
    exchange = next(r for r in net.requests if r.url.path == "/oauth2/access_token")
    assert exchange.url.host == "acme.amocrm.ru"
    body = json.loads(exchange.content)
    assert (body["client_id"], body["client_secret"], body["code"]) == (
        "amo-client-id",
        "amo-client-secret",
        "c0de",
    )
    assert body["redirect_uri"] == f"{ORIGIN}/oauth/amocrm/callback"
    (crm, endpoint, account) = (await rows(engine, "SELECT crm, endpoint, account FROM crm_connections"))[0]
    assert (crm, endpoint, account) == ("amocrm", "acme.amocrm.ru", "777")
    st = app.state.store
    conn_id = uuid.UUID(done.headers["location"].rsplit("/", 1)[1])
    assert (await st.secrets(conn_id))["client_secret"] == "amo-client-secret"
    # the window is used up
    assert (await b.http.get("/oauth/amocrm/callback", params=params)).status_code == 400


async def test_amocrm_redirect_must_match_the_delivered_keys_and_domain(app: FastAPI, net: Net) -> None:
    b = Browser(app)
    hook = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    cases = [
        ({"referer": "acme.amocrm.ru", "client_id": "someone-else"}, True),
        ({"referer": "evil.example", "client_id": "amo-client-id"}, True),
        ({"referer": "acme.amocrm.ru.evil.example", "client_id": "amo-client-id"}, True),
        ({"referer": "acme.amocrm.ru", "client_id": "amo-client-id"}, False),  # keys never delivered
    ]
    for extra, deliver in cases:
        state = _state((await b.http.get("/ru/connect/amocrm/start")).headers["location"])
        if deliver:
            await hook.post(
                "/hooks/amocrm/secrets",
                json={"state": state, "client_id": "amo-client-id", "client_secret": "amo-client-secret"},
            )
        resp = await b.http.get("/oauth/amocrm/callback", params={"code": "c", "state": state} | extra)
        assert resp.status_code == 400, extra
    assert net.requests == []


async def test_oauth_state_belongs_to_one_session_and_is_used_once(
    app: FastAPI, engine: AsyncEngine, net: Net
) -> None:
    owner, thief = Browser(app), Browser(app)
    await thief.csrf()
    start = await owner.http.get("/en/connect/zoho/start")
    location = start.headers["location"]
    assert location.startswith("https://accounts.zoho.com/oauth/v2/auth?")
    query = parse_qs(urlsplit(location).query)
    assert query["redirect_uri"] == [f"{ORIGIN}/oauth/zoho/callback"]
    assert query["access_type"] == ["offline"]
    state = _state(location)
    params = {"code": "c", "state": state, "accounts-server": "https://accounts.zoho.eu", "location": "eu"}
    assert (await thief.http.get("/oauth/zoho/callback", params=params)).status_code == 400
    assert net.requests == []
    done = await owner.http.get("/oauth/zoho/callback", params=params)
    assert done.status_code == 303
    assert done.headers["location"].startswith("/en/connections/")
    assert {r.url.host for r in net.requests} == {"accounts.zoho.eu", "www.zohoapis.eu"}
    assert (await owner.http.get("/oauth/zoho/callback", params=params)).status_code == 400
    (endpoint, account, meta) = (await rows(engine, "SELECT endpoint, account, meta FROM crm_connections"))[0]
    assert (endpoint, account) == ("www.zohoapis.eu", "9001")
    assert meta["web"] == "https://crm.zoho.eu/crm/org9001"
    # expired windows are refused
    late = _state((await owner.http.get("/en/connect/zoho/start")).headers["location"])
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE oauth_states SET expires_at = now() - interval '1 minute' WHERE used_at IS NULL")
        )
    assert (await owner.http.get("/oauth/zoho/callback", params=params | {"state": late})).status_code == 400


async def test_zoho_accounts_server_outside_zoho_is_refused(app: FastAPI, net: Net) -> None:
    b = Browser(app)
    state = _state((await b.http.get("/ru/connect/zoho/start")).headers["location"])
    resp = await b.http.get(
        "/oauth/zoho/callback",
        params={"code": "c", "state": state, "accounts-server": "https://evil.example"},
    )
    assert resp.status_code == 400
    assert net.requests == []


async def test_denied_consent_and_missing_code(app: FastAPI, net: Net) -> None:
    b = Browser(app)
    state = _state((await b.http.get("/ru/connect/kommo/start")).headers["location"])
    denied = await b.http.get("/oauth/kommo/callback", params={"state": state, "error": "access_denied"})
    assert denied.status_code == 400
    assert "Доступ не выдан" in denied.text
    state = _state((await b.http.get("/ru/connect/kommo/start")).headers["location"])
    assert (await b.http.get("/oauth/kommo/callback", params={"state": state})).status_code == 400
    assert net.requests == []


async def test_zoho_disconnect_revokes_the_refresh_token(app: FastAPI, net: Net) -> None:
    b = Browser(app)
    state = _state((await b.http.get("/ru/connect/zoho/start")).headers["location"])
    done = await b.http.get(
        "/oauth/zoho/callback", params={"code": "c", "state": state, "accounts-server": "accounts.zoho.eu"}
    )
    conn_id = done.headers["location"].rsplit("/", 1)[1]
    await b.post(f"/ru/connections/{conn_id}/disable", csrf=await b.csrf())
    revoke = [r for r in net.requests if r.url.path == "/oauth/v2/token/revoke"]
    assert len(revoke) == 1
    assert revoke[0].url.host == "accounts.zoho.eu"
    assert revoke[0].url.params["token"] == "RT-zoho"


async def test_oauth_not_configured(make: Callable[..., FastAPI]) -> None:
    app = make(kommo_client_id="", zoho_client_secret="")
    await make.start(app)  # type: ignore[attr-defined]
    b = Browser(app)
    for crm in ("kommo", "zoho"):
        assert (await b.http.get(f"/ru/connect/{crm}/start")).status_code == 503
        assert f'href="/ru/connect/{crm}/start"' not in (await b.http.get(f"/ru/connect/{crm}")).text


# --- hooks from the CRM side ---


async def _kommo_connection(app: FastAPI) -> tuple[Browser, str]:
    b = Browser(app)
    state = _state((await b.http.get("/ru/connect/kommo/start")).headers["location"])
    location = (
        await b.http.get(
            "/oauth/kommo/callback", params={"code": "c", "state": state, "referer": "acme.kommo.com"}
        )
    ).headers["location"]
    return b, location.rsplit("/", 1)[1]


def _signature(client_uuid: str, account: str, secret: str = KOMMO_SECRET) -> str:
    return hmac.new(secret.encode(), f"{client_uuid}|{account}".encode(), hashlib.sha256).hexdigest()


async def test_kommo_uninstall_hook_needs_our_signature(app: FastAPI, engine: AsyncEngine) -> None:
    b, conn_id = await _kommo_connection(app)
    hook = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    bad = [
        {"client_uuid": "kommo-client-id", "account_id": "4242", "signature": "0" * 64},
        {
            "client_uuid": "kommo-client-id",
            "account_id": "4242",
            "signature": _signature("kommo-client-id", "4242", "guess"),
        },
        {
            "client_uuid": "other-client",
            "account_id": "4242",
            "signature": _signature("other-client", "4242"),
        },
        {"client_uuid": "kommo-client-id", "account_id": "4242", "signature": "й"},
        {"client_uuid": "kommo-client-id", "account_id": "4242"},
    ]
    for params in bad:
        assert (await hook.get("/hooks/kommo/uninstall", params=params)).status_code == 200
    assert (await rows(engine, "SELECT status FROM crm_connections"))[0][0] == "active"
    good = {
        "client_uuid": "kommo-client-id",
        "account_id": "4242",
        "signature": _signature("kommo-client-id", "4242"),
    }
    assert (await hook.post("/hooks/kommo/uninstall", data=good)).text == "ok"
    assert (await rows(engine, "SELECT status, last_error, secret FROM crm_connections"))[0] == (
        "disabled",
        "crm_uninstalled",
        None,
    )
    page = await b.http.get(f"/ru/connections/{conn_id}")
    assert 'data-notice="crm_uninstalled"' in page.text


async def test_sweep_finds_access_removed_in_the_crm(
    app: FastAPI, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = Browser(app)
    lost = await b.connect_bitrix24()
    kept = await b.connect_bitrix24("https://other.bitrix24.ru/rest/1/whsecrettoken123456/")
    dead, alive = FakeCRM(), FakeCRM()
    dead.fail = CrmAuthError("revoked", crm="bitrix24", status=401)
    monkeypatch.setattr(
        app.state.connectors, "adapter", lambda info, secrets, store: dead if str(info.id) == lost else alive
    )
    assert await sweep(app.state.store, app.state.connectors) == (2, 1)
    found = {
        r[0]: r[1:]
        for r in await rows(
            engine, "SELECT id::text, status, last_error, checked_at IS NOT NULL FROM crm_connections"
        )
    }
    assert found[lost] == ("reauth_required", "auth", True)
    assert found[kept] == ("active", None, True)


# --- limits, language, health, secrets ---


async def test_rate_limit_per_address(make: Callable[..., FastAPI]) -> None:
    app = make(rate_per_minute=5, rate_post_per_minute=2)
    await make.start(app)  # type: ignore[attr-defined]
    b = Browser(app)
    token = await b.csrf()
    codes = [
        (await b.http.post("/ru/connect/bitrix24", data={"csrf": token, "webhook_url": ""})).status_code
        for _ in range(3)
    ]
    assert codes == [400, 400, 429]
    assert (await b.http.get("/health")).status_code == 200
    statuses = [(await b.http.get("/ru")).status_code for _ in range(3)]
    assert statuses[-1] == 429


def test_rate_limiter_windows() -> None:
    lim = RateLimiter(2, 1)
    assert [lim.allow("a", "GET", 60.0) for _ in range(3)] == [True, True, False]
    assert lim.allow("b", "GET", 61.0)
    assert lim.allow("a", "POST", 120.0)
    assert not lim.allow("a", "POST", 121.0)


@pytest.mark.parametrize(
    ("header", "lang"), [("ru-RU,ru;q=0.9", "ru"), ("uz", "ru"), ("en-GB", "en"), ("de", "en"), (None, "ru")]
)
def test_language_from_the_browser(header: str | None, lang: str) -> None:
    assert pick_lang(header) == lang


async def test_root_language_switch_and_health(app: FastAPI) -> None:
    b = Browser(app)
    root = await b.http.get("/", headers={"Accept-Language": "en-US"})
    assert root.status_code == 303
    assert root.headers["location"] == "/en"
    en = await b.http.get("/en/connect/odoo")
    assert "Connect your CRM" in en.text
    assert 'href="/ru/connect/odoo"' in en.text
    assert "stand" in en.text
    health = await b.http.get("/health")
    assert health.json() == {"status": "ok", "version": "abc123"}
    assert (await b.http.get("/static/app.css")).status_code == 200


async def test_web_is_closed_without_a_strong_session_secret(make: Callable[..., FastAPI]) -> None:
    app = make(session_secret="short")
    await make.start(app)  # type: ignore[attr-defined]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    assert (await client.get("/ru")).status_code == 404
    assert (await client.get("/health")).status_code == 200


async def test_no_secret_in_any_table_or_log(
    app: FastAPI,
    fake: FakeCRM,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dns(monkeypatch, "93.184.215.14")
    caplog.set_level("DEBUG")
    b = Browser(app)
    await b.connect_bitrix24()
    _, _kommo = await _kommo_connection(app)
    state = _state((await b.http.get("/ru/connect/zoho/start")).headers["location"])
    await b.http.get(
        "/oauth/zoho/callback", params={"code": "c", "state": state, "accounts-server": "accounts.zoho.eu"}
    )
    await b.post(
        "/ru/connect/odoo",
        {"url": "https://odoo.example.com", "database": "d", "login": "l", "api_key": "odoo-api-key-123"},
    )
    for conn_id in [r[0] for r in await rows(engine, "SELECT id::text FROM crm_connections")]:
        await b.post(f"/ru/connections/{conn_id}/check", csrf=await b.csrf())
    dump = json.dumps(
        [
            [str(v) for v in row]
            for table in ("crm_writes", "oauth_states")
            for row in await rows(engine, f"SELECT * FROM {table}")  # noqa: S608
        ]
        + [
            [str(v) for v in row]
            for row in await rows(
                engine,
                "SELECT id, owner, crm, label, endpoint, account, meta, status, last_error FROM crm_connections",
            )
        ]
    )
    blobs = b"".join(
        r[0] for r in await rows(engine, "SELECT secret FROM crm_connections WHERE secret IS NOT NULL")
    )
    for value in SECRETS_SEEN:
        assert value not in dump, value
        assert value.encode() not in blobs, value
        assert value not in caplog.text, value


async def test_stored_connections_get_the_right_adapter_and_refresh_with_their_own_keys(
    app: FastAPI, engine: AsyncEngine, net: Net
) -> None:
    """amoCRM refreshes with the keys of the integration created in that account; Kommo and Zoho with
    ours; an expired access token is refreshed before the call and the new one is stored."""
    b = Browser(app)
    hook = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    state = _state((await b.http.get("/ru/connect/amocrm/start")).headers["location"])
    await hook.post(
        "/hooks/amocrm/secrets",
        json={"state": state, "client_id": "amo-client-id", "client_secret": "amo-client-secret"},
    )
    await b.http.get(
        "/oauth/amocrm/callback",
        params={"code": "c", "state": state, "referer": "acme.amocrm.ru", "client_id": "amo-client-id"},
    )
    await _kommo_connection(app)
    state = _state((await b.http.get("/ru/connect/zoho/start")).headers["location"])
    await b.http.get(
        "/oauth/zoho/callback", params={"code": "c", "state": state, "accounts-server": "accounts.zoho.eu"}
    )
    await b.connect_bitrix24()
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE crm_connections SET access_expires_at = now()"))
    st, connectors = app.state.store, app.state.connectors
    for info in await st.list_active():
        secrets = await st.secrets(info.id)
        if "expires_at" in secrets:  # make the stored access token expire
            secrets["expires_at"] = "2020-01-01T00:00:00+00:00"
            async with st._sessions.begin() as session:
                from alnuzum_platform.models import CrmConnection

                row = await session.get(CrmConnection, info.id)
                st._seal(row, secrets)
        net.requests.clear()
        async with connectors.adapter(info, await st.secrets(info.id), st) as crm:
            assert (
                type(crm).__name__
                == {"amocrm": "AmoCRM", "kommo": "Kommo", "zoho": "ZohoCRM", "bitrix24": "Bitrix24"}[
                    info.crm.value
                ]
            )
            await crm.account()
        refresh = [r for r in net.requests if r.url.path in ("/oauth2/access_token", "/oauth/v2/token")]
        if info.crm.value == "bitrix24":
            assert refresh == []
            continue
        assert len(refresh) == 1, info.crm
        body = refresh[0].content.decode()
        expected_client = {"amocrm": "amo-client-id", "kommo": "kommo-client-id", "zoho": "zoho-client-id"}[
            info.crm.value
        ]
        assert expected_client in body
        assert "refresh_token" in body
        stored = await st.secrets(info.id)
        assert stored["expires_at"] > "2026"
    odoo_info = await st.create(
        "s:x", CrmKind.ODOO, "https://93.184.215.14", {"database": "d", "login": "l", "api_key": "k"}
    )
    assert type(connectors.adapter(odoo_info, await st.secrets(odoo_info.id), st)).__name__ == "Odoo"


async def test_revoke_is_only_for_zoho_and_never_raises(app: FastAPI, net: Net) -> None:
    connectors = app.state.connectors
    assert await connectors.revoke(CrmKind.AMOCRM, {"refresh_token": "x"}) is False
    assert (
        await connectors.revoke(CrmKind.ZOHO, {"refresh_token": "x", "accounts_server": "evil.example"})
        is False
    )
    assert await connectors.revoke(
        CrmKind.ZOHO, {"refresh_token": "RT-zoho", "accounts_server": "accounts.zoho.com"}
    )
    assert [r.url.host for r in net.requests] == ["accounts.zoho.com"]
