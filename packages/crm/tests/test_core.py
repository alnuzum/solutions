"""Rate limiting, retries, token sources, OAuth helpers, record/replay."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from alnuzum_crm import oauth
from alnuzum_crm.errors import CrmAuthError, CrmError, CrmRateLimited, CrmUnavailable
from alnuzum_crm.http import HttpCore, RateLimiter, RetryPolicy, shared_limiter
from alnuzum_crm.testing import Recorder, Replayer, ReplayMismatch, Scrubber, describe_jsonrpc
from alnuzum_crm.tokens import MemoryOAuthToken, StaticToken, TokenSet
from alnuzum_crm.types import CrmKind

FAST = RetryPolicy(max_retries=2, backoff=0.001)


def later(hours: int = 1) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def test_rate_limiter_spreads_calls() -> None:
    lim = RateLimiter(2, period=0.1)
    start = time.monotonic()
    for _ in range(5):
        await lim.acquire()
    assert time.monotonic() - start >= 0.2
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)


def test_shared_limiter_is_one_per_account() -> None:
    assert shared_limiter("x:1", 5) is shared_limiter("x:1", 5)
    assert shared_limiter("x:1", 6).rate == 6


def test_retry_delay() -> None:
    policy = RetryPolicy(backoff=0.5, max_delay=10)
    assert policy.delay(1, "3") == 3.0
    assert policy.delay(1, "999") == 10.0
    assert policy.delay(3, None) == 2.0
    assert policy.delay(3, "Wed, 21 Oct 2026 07:28:00 GMT") == 2.0


async def test_http_core_retries_then_gives_up_without_the_url() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom https://secret.example/rest/1/token", request=request)

    core = HttpCore("test", limiter=RateLimiter(1000), retry=FAST, transport=httpx.MockTransport(handler))
    with pytest.raises(CrmUnavailable) as err:
        await core.send("GET", "https://secret.example/rest/1/token")
    assert calls == 3
    assert "token" not in str(err.value)
    await core.aclose()


async def test_http_core_retries_429_and_custom_predicate() -> None:
    answers = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, text="limit"),
        httpx.Response(200, text="ok"),
    ]
    core = HttpCore(
        "test", limiter=RateLimiter(1000), retry=FAST, transport=httpx.MockTransport(lambda r: answers.pop(0))
    )
    resp = await core.send("GET", "https://x.example/", retry_if=lambda r: r.text == "limit")
    assert resp.text == "ok"
    await core.aclose()


async def test_static_token() -> None:
    src = StaticToken("long-lived", crm="amocrm")
    assert await src.token() == "long-lived"
    assert "long-lived" not in repr(src)
    with pytest.raises(CrmAuthError):
        await src.refresh("long-lived")


async def test_memory_oauth_token_refreshes_once_for_concurrent_callers() -> None:
    calls = 0
    saved: list[TokenSet] = []

    async def refresher(t: TokenSet) -> TokenSet:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return TokenSet(f"a{calls + 1}", f"r{calls + 1}", later())

    async def save(t: TokenSet) -> None:
        saved.append(t)

    src = MemoryOAuthToken(TokenSet("a1", "r1", later()), refresher, on_refresh=save)
    assert await src.token() == "a1"
    results = await asyncio.gather(*(src.refresh("a1") for _ in range(4)))
    assert set(results) == {"a2"}
    assert calls == 1
    assert [t.refresh_token for t in saved] == ["r2"]
    assert "a2" not in repr(src)
    assert "a2" not in repr(src.current)


async def test_memory_oauth_token_refreshes_ahead_of_expiry() -> None:
    async def refresher(t: TokenSet) -> TokenSet:
        return TokenSet("fresh", t.refresh_token, later())

    src = MemoryOAuthToken(TokenSet("old", "r", datetime.now(UTC) + timedelta(seconds=5)), refresher)
    assert await src.token() == "fresh"


@pytest.mark.parametrize(
    ("raw", "kind", "want"),
    [
        ("acme", CrmKind.AMOCRM, "acme.amocrm.ru"),
        ("https://Acme.amocrm.com/leads/", CrmKind.AMOCRM, "acme.amocrm.com"),
        ("acme", CrmKind.KOMMO, "acme.kommo.com"),
    ],
)
def test_normalize_amo_domain(raw: str, kind: CrmKind, want: str) -> None:
    assert oauth.normalize_amo_domain(raw, kind) == want


@pytest.mark.parametrize("raw", ["acme.amocrm.ru.evil.com", "evil.com", "a_b.amocrm.ru", "", "x.y.amocrm.ru"])
def test_normalize_amo_domain_refuses(raw: str) -> None:
    with pytest.raises(ValueError, match="account domain"):
        oauth.normalize_amo_domain(raw)


def test_zoho_hosts() -> None:
    assert oauth.zoho_host("https://accounts.zoho.eu", oauth.ZOHO_ACCOUNTS_HOSTS) == "accounts.zoho.eu"
    assert oauth.zoho_host("www.zohoapis.in", oauth.ZOHO_API_HOSTS) == "www.zohoapis.in"
    for bad in (
        "http://accounts.zoho.com",
        "https://accounts.zoho.com:8443",
        "https://accounts.zoho.com.evil.io",
    ):
        with pytest.raises(ValueError, match="Zoho"):
            oauth.zoho_host(bad, oauth.ZOHO_ACCOUNTS_HOSTS)


def test_zoho_developer_edition_host_is_exact() -> None:
    # a token issued for the Developer Edition answers only on its own host
    assert oauth.zoho_host("https://developer.zohoapis.com", oauth.ZOHO_API_HOSTS) == "developer.zohoapis.com"
    for bad in (
        "https://developer.zohoapis.com.evil.io",
        "https://evil.developer.zohoapis.com",
        "https://xdeveloper.zohoapis.com",
        "https://developer.zohoapis.eu",
        "https://sandbox.zohoapis.com",
        "http://developer.zohoapis.com",
        "https://developer.zohoapis.com:8443",
        "https://developer.zoho.com",
    ):
        with pytest.raises(ValueError, match="Zoho"):
            oauth.zoho_host(bad, oauth.ZOHO_API_HOSTS)
    # the API host never doubles as an accounts server
    with pytest.raises(ValueError, match="Zoho"):
        oauth.zoho_host("https://developer.zohoapis.com", oauth.ZOHO_ACCOUNTS_HOSTS)


def test_authorize_urls() -> None:
    assert oauth.amo_authorize_url(CrmKind.KOMMO, "cid", "st").startswith(
        "https://www.kommo.com/oauth?client_id=cid"
    )
    assert "www.amocrm.ru" in oauth.amo_authorize_url(CrmKind.AMOCRM, "cid", "st")
    url = oauth.zoho_authorize_url("cid", "https://x.example/cb", "st", "ZohoCRM.modules.ALL")
    assert url.startswith("https://accounts.zoho.com/oauth/v2/auth?")
    assert "access_type=offline" in url


def mock(*answers: httpx.Response | Exception) -> httpx.MockTransport:
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler)


async def test_amo_code_and_refresh() -> None:
    ok = httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 86400})
    t = await oauth.amo_exchange_code(
        CrmKind.AMOCRM, "acme", "id", "secret", "https://x/cb", "code", transport=mock(ok)
    )
    assert (t.access_token, t.refresh_token) == ("A", "R")
    ok2 = httpx.Response(200, json={"access_token": "A2", "refresh_token": "R2"})
    t2 = await oauth.amo_refresh(
        CrmKind.KOMMO, "acme", "id", "secret", "https://x/cb", t, transport=mock(ok2)
    )
    assert t2.refresh_token == "R2"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(400, json={"hint": "Authorization code has expired"}), CrmAuthError),
        (httpx.Response(401, text="nope"), CrmAuthError),
        (httpx.Response(500, json={}), CrmError),
        (httpx.Response(200, json={"error": "invalid_code"}), CrmAuthError),
        (httpx.ConnectError("x"), CrmUnavailable),
    ],
)
async def test_token_endpoint_errors(answer: httpx.Response | Exception, error: type[Exception]) -> None:
    with pytest.raises(error):
        await oauth.amo_exchange_code(
            CrmKind.AMOCRM, "acme", "i", "s", "https://x/cb", "c", transport=mock(answer)
        )


async def test_zoho_code_and_refresh() -> None:
    ok = httpx.Response(
        200,
        json={
            "access_token": "A",
            "refresh_token": "R",
            "api_domain": "https://www.zohoapis.eu",
            "expires_in": 3600,
        },
    )
    tokens, api = await oauth.zoho_exchange_code(
        "accounts.zoho.eu", "i", "s", "https://x/cb", "c", transport=mock(ok)
    )
    assert api == "www.zohoapis.eu"
    fresh = await oauth.zoho_refresh(
        "accounts.zoho.eu", "i", "s", tokens, transport=mock(httpx.Response(200, json={"access_token": "B"}))
    )
    assert (fresh.access_token, fresh.refresh_token) == ("B", "R")
    evil = httpx.Response(
        200, json={"access_token": "A", "refresh_token": "R", "api_domain": "https://evil.example"}
    )
    with pytest.raises(ValueError, match="Zoho"):
        await oauth.zoho_exchange_code(
            "accounts.zoho.com", "i", "s", "https://x/cb", "c", transport=mock(evil)
        )


def test_rate_limited_error_keeps_retry_after() -> None:
    err = CrmRateLimited("slow down", crm="zoho", retry_after=5.0, status=429)
    assert (err.retry_after, err.status, str(err)) == (5.0, 429, "zoho: slow down")


def test_describe_jsonrpc() -> None:
    req = httpx.Request(
        "POST",
        "https://o/jsonrpc",
        json={
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": ["db", 2, "k", "crm.lead", "create"],
            }
        },
    )
    assert describe_jsonrpc(req) == "object.execute_kw crm.lead create"
    assert describe_jsonrpc(httpx.Request("POST", "https://o/", content=b"not json")) == ""


async def test_record_scrub_and_replay(tmp_path: Path) -> None:
    inner = mock(
        httpx.Response(200, json={"token": "SECRET-1"}),
        httpx.Response(200, content=b"\x00\x01", headers={"content-type": "application/octet-stream"}),
    )
    rec = Recorder(inner)
    async with httpx.AsyncClient(transport=rec) as client:
        await client.get("https://real.host/a?t=SECRET-1")
        await client.get("https://real.host/b")
    path = tmp_path / "c.json"
    rec.save(path, {"run_id": "1"}, Scrubber({"SECRET-1": "X", "real.host": "example.com"}, [(r"/b$", "/B")]))
    text = path.read_text()
    assert "SECRET-1" not in text
    assert "real.host" not in text
    replay = Replayer(path)
    async with httpx.AsyncClient(transport=replay) as client:
        assert (await client.get("https://example.com/a?t=X")).json() == {"token": "X"}
        with pytest.raises(ReplayMismatch, match="recorded"):
            await client.get("https://example.com/other")
        assert (await client.get("https://example.com/B")).content == b"\x00\x01"
        assert replay.exhausted
        with pytest.raises(ReplayMismatch, match="unexpected"):
            await client.get("https://example.com/again")
    assert json.loads(text)["meta"] == {"run_id": "1"}
