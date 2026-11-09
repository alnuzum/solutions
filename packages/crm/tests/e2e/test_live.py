"""Live E2E against the test accounts: create everything, read it back, remove it."""

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.bitrix24 import Bitrix24, parse_webhook
from alnuzum_crm.netguard import GuardedTransport
from alnuzum_crm.oauth import zoho_refresh
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.testing import (
    TECH_LIMIT,
    Scrubber,
    check_room,
    clients_in_account,
    describe_jsonrpc,
    run_conformance,
)
from alnuzum_crm.tokens import MemoryOAuthToken, StaticToken, TokenSet
from alnuzum_crm.zoho import ZohoCRM

from .conftest import Recording, need

pytestmark = pytest.mark.e2e

FULL_RUN = [
    "company created",
    "company deduplicated by name",
    "company deduplicated by INN",
    "contact deduplicated by e-mail and phone",
    "found contact linked to the company",
    "deal with lines created",
    "file attached",
    "note added",
    "task added",
    "deal read back with lines",
    "deal history read",
    "stages read",
    "deal found by title, company and contact",
    "deal moved, amount set, products added",
    "catalog read",
    "missing deal is CrmNotFound",
    "test records removed",
]
#: the amoCRM family on its pool: the company was found, not created
POOL_RUN = ["company found in the pool", *FULL_RUN[1:]]


@pytest.mark.parametrize(("cls", "prefix"), [(AmoCRM, "AMOCRM"), (Kommo, "KOMMO")])
async def test_amocrm_family(
    cls: type[AmoCRM],
    prefix: str,
    run_id: str,
    recording: Callable[..., Recording],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On the pool (a technical account keeps 100 contacts and companies); E2E_CREATE=1 creates its
    own company and contacts as the other CRMs do, three records that stay in the account."""
    env = need(f"{prefix}_DOMAIN", f"{prefix}_TOKEN")
    token = StaticToken(env[f"{prefix}_TOKEN"], crm=cls.kind.value)
    # counted outside the recording: the contract tests replay the scenario alone
    async with cls(env[f"{prefix}_DOMAIN"], token) as probe:
        count = await clients_in_account(probe)
    with capsys.disabled():
        print(f"\n{cls.kind.value}: contacts and companies {count}/{TECH_LIMIT}")
    check_room(count)
    pool = os.environ.get("E2E_CREATE") != "1"
    rec = recording(f"{cls.kind.value}-pool" if pool else cls.kind.value)
    crm = cls(env[f"{prefix}_DOMAIN"], token, transport=rec.recorder)
    async with crm:
        steps = await run_conformance(crm, run_id, pool=pool)
    assert steps in ((FULL_RUN, POOL_RUN) if pool else (FULL_RUN,))
    sub = crm.domain.split(".")[0]
    placeholder = "example.amocrm.ru" if cls is AmoCRM else "example.kommo.com"
    rec.save(
        run_id,
        Scrubber(
            {env[f"{prefix}_TOKEN"]: "TOKEN", crm.domain: placeholder, sub: "example"},
            [(r"/upload/[A-Za-z0-9._~%-]+", "/upload/UPLOAD")],
        ),
    )


async def test_bitrix24(run_id: str, recording: Callable[..., Recording]) -> None:
    env = need("B24_WEBHOOK")
    rec = recording("bitrix24")
    crm = Bitrix24(env["B24_WEBHOOK"], transport=rec.recorder)
    async with crm:
        steps = await run_conformance(crm, run_id)
    assert steps == FULL_RUN
    host, base = parse_webhook(env["B24_WEBHOOK"])
    token = base.rsplit("/", 1)[1]
    rec.save(
        run_id, Scrubber({token: "WEBHOOKTOKEN", host: "example.bitrix24.ru", host.split(".")[0]: "example"})
    )


async def test_zoho(run_id: str, recording: Callable[..., Recording]) -> None:
    env = need("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    accounts = os.environ.get("ZOHO_ACCOUNTS_SERVER", "https://accounts.zoho.com")

    async def refresh(tokens: TokenSet) -> TokenSet:
        return await zoho_refresh(accounts, env["ZOHO_CLIENT_ID"], env["ZOHO_CLIENT_SECRET"], tokens)

    expired = TokenSet("", env["ZOHO_REFRESH_TOKEN"], datetime.now(UTC) - timedelta(seconds=1))
    tokens = MemoryOAuthToken(expired, refresh)
    rec = recording("zoho")
    crm = ZohoCRM(
        os.environ.get("ZOHO_API_DOMAIN", "https://developer.zohoapis.com"), tokens, transport=rec.recorder
    )
    async with crm:
        steps = await run_conformance(crm, run_id)
    assert steps == FULL_RUN
    rec.save(run_id, Scrubber({tokens.current.access_token: "TOKEN", env["ZOHO_REFRESH_TOKEN"]: "REFRESH"}))


async def test_odoo(run_id: str, recording: Callable[..., Recording]) -> None:
    env = need("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_PASSWORD")
    rec = recording("odoo", describe_jsonrpc)
    # the test Odoo runs next to the tests (Docker): an operator-configured address
    crm = Odoo(
        env["ODOO_URL"],
        env["ODOO_DB"],
        env["ODOO_LOGIN"],
        env["ODOO_PASSWORD"],
        trusted_url=True,
        transport=rec.recorder,
    )
    async with crm:
        steps = await run_conformance(crm, run_id)
    assert steps == FULL_RUN
    rec.save(
        run_id,
        Scrubber({env["ODOO_URL"].rstrip("/"): "https://odoo.example.com", env["ODOO_PASSWORD"]: "KEY"}),
    )


async def test_odoo_through_the_network_guard() -> None:
    """The production path: an https Odoo on the internet, reached only through the guard."""
    crm = Odoo(os.environ.get("ODOO_PUBLIC_URL", "https://www.odoo.com"), "none", "none", "none")
    async with crm:
        assert (await crm.server_version()).strip()


async def test_guard_reaches_a_host_listed_ipv6_first() -> None:
    """A host whose DNS gives IPv6 first is reached from a network without IPv6 (CI runners, Docker)."""
    async with httpx.AsyncClient(transport=GuardedTransport(), timeout=20) as client:
        resp = await client.get("https://nextcloud.com/", follow_redirects=False)
    assert resp.status_code < 500
