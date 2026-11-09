"""Live E2E against the test accounts: create everything, read it back, remove it."""

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.base import CRM
from alnuzum_crm.bitrix24 import Bitrix24, parse_webhook
from alnuzum_crm.netguard import GuardedTransport
from alnuzum_crm.oauth import zoho_refresh
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.testing import (
    PHONE_CODE,
    POOL_KEY,
    POOL_NUMBERS,
    TECH_LIMIT,
    Scrubber,
    check_room,
    clients_in_account,
    describe_jsonrpc,
    run_conformance,
    run_numbers,
)
from alnuzum_crm.tokens import MemoryOAuthToken, StaticToken, TokenSet
from alnuzum_crm.types import CompanyIn, DealIn, DealLine, DealStatus, Leftovers, ProductIn, TaskIn
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


# --- the lookups the mail triage added: companies by their requisites, users, a deal's stage and lead ---

#: Zoho's search index lags 2 to 5 minutes behind a write: a company kept for good is looked up instead,
#: created by the first run that misses it and found by every run after
ZOHO_LOOKUP = ("E2E Lookup pool", "390000002", "9000002")


def _phone(tail: str) -> str:
    return f"+998 {PHONE_CODE} {tail[:3]} {tail[3:5]} {tail[5:]}"


def _zoho() -> ZohoCRM:
    env = need("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    accounts = os.environ.get("ZOHO_ACCOUNTS_SERVER", "https://accounts.zoho.com")

    async def refresh(tokens: TokenSet) -> TokenSet:
        return await zoho_refresh(accounts, env["ZOHO_CLIENT_ID"], env["ZOHO_CLIENT_SECRET"], tokens)

    expired = TokenSet("", env["ZOHO_REFRESH_TOKEN"], datetime.now(UTC) - timedelta(seconds=1))
    return ZohoCRM(
        os.environ.get("ZOHO_API_DOMAIN", "https://developer.zohoapis.com"),
        MemoryOAuthToken(expired, refresh),
    )


def _adapter(kind: str) -> CRM:
    if kind in ("amocrm", "kommo"):
        prefix, cls = ("AMOCRM", AmoCRM) if kind == "amocrm" else ("KOMMO", Kommo)
        env = need(f"{prefix}_DOMAIN", f"{prefix}_TOKEN")
        return cls(env[f"{prefix}_DOMAIN"], StaticToken(env[f"{prefix}_TOKEN"], crm=kind))
    if kind == "bitrix24":
        return Bitrix24(need("B24_WEBHOOK")["B24_WEBHOOK"])
    if kind == "zoho":
        return _zoho()
    env = need("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_PASSWORD")
    return Odoo(env["ODOO_URL"], env["ODOO_DB"], env["ODOO_LOGIN"], env["ODOO_PASSWORD"], trusted_url=True)


@pytest.mark.parametrize("kind", ["amocrm", "kommo", "bitrix24", "zoho", "odoo"])
async def test_lookups_of_the_mail_triage(kind: str, run_id: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``users``, ``find_companies`` by INN and phone, ``create_deal`` into a chosen stage with a chosen
    lead. amoCRM and Kommo create no company or contact here (their 100): the pool is looked up."""
    crm = _adapter(kind)
    made = Leftovers()
    async with crm:
        if isinstance(crm, AmoCRM):
            before = await crm.clients()
            inn, tail = POOL_NUMBERS
            company = await crm.find_company(CompanyIn(f"E2E Co {POOL_KEY}", inn=inn))
            assert company, "the pool company is gone: the conformance run above recreates it"
            fresh = False
        elif kind == "zoho":
            name, inn, tail = ZOHO_LOOKUP
            ref = await crm.find_or_create_company(CompanyIn(name, inn=inn, phone=_phone(tail)))
            company, fresh = ref.id, ref.created
        else:
            inn, tail = run_numbers(run_id)
            company = await crm.create_company(CompanyIn(f"E2E Lookup {run_id}", inn=inn, phone=_phone(tail)))
            made = Leftovers(company_ids=(company,))
            fresh = False
        try:
            users = await crm.users()
            assert users and all(u.id and u.name for u in users)
            opens = [s for s in await crm.stages() if s.kind is DealStatus.OPEN]
            stage = opens[1] if len(opens) > 1 else opens[0]
            deal = await crm.create_deal(
                DealIn(f"E2E lookup {run_id}", stage_id=stage.id, responsible_id=users[0].id)
            )
            made = Leftovers(deal_ids=(deal.id,), company_ids=made.company_ids)
            assert (await crm.get_deal(deal.id)).stage_id == stage.id
            if fresh:
                with capsys.disabled():
                    print(f"\n{kind}: the lookup company created, searched from the next run (index lag)")
            else:
                by_inn = {h.id for h in await crm.find_companies(inn=inn)}
                by_phone = {h.id for h in await crm.find_companies(phone=_phone(tail))}
                assert company in by_inn, f"{kind}: not found by INN"
                assert company in by_phone, f"{kind}: not found by phone"
        finally:
            await crm.discard(made)
        if isinstance(crm, AmoCRM):
            assert await crm.clients() == before, (
                "no company or contact may be created in a technical account"
            )


# --- win-back: the purchases of a client and a task on the client itself ---

#: amoCRM and Kommo delete no deal (405) and Zoho's search lags minutes behind a write: there the pool company
#: keeps two won deals for good, created by the first run that misses them and read by every run after (a closed
#: deal is not among the 100 open ones of a technical account). Bitrix24 and Odoo make and remove their own.
PURCHASE_POOL = (("E2E purchase pool 1", 60, Decimal(3)), ("E2E purchase pool 2", 20, Decimal(2)))
POOL_KINDS = ("amocrm", "kommo", "zoho")


async def _linked_back(crm: CRM, company: str, title: str) -> bool:
    """The conformance run unlinks every deal from the amoCRM pool company: a pool deal found by its title
    is linked back instead of a new one made."""
    if not isinstance(crm, AmoCRM):
        return False
    for hit in await crm.find_deals(title, limit=20):
        if hit.title == title and hit.status is DealStatus.WON:
            link = [{"to_entity_id": int(hit.id), "to_entity_type": "leads"}]
            await crm._request("POST", f"/api/v4/companies/{int(company)}/link", json=link)
            return True
    return False


async def _product(crm: CRM, name: str, made: list[str]) -> str:
    found = next((p.id for p in (await crm.list_products(query=name)).items if p.name == name), None)
    if found is None:
        found = await crm.create_product(ProductIn(name, Decimal(100), unit="шт"))
        made.append(found)
    return found


@pytest.mark.parametrize("kind", ["amocrm", "kommo", "bitrix24", "zoho", "odoo"])
async def test_purchases_and_a_task_on_the_client(
    kind: str, run_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """``create_won_deal`` at a past date, ``purchases`` of the company and of the whole account with their
    products, ``add_client_task`` on the company, ``client_tasks``, ``discard_task``. amoCRM and Kommo create
    no company, contact or open deal here: the account's counts stay as they were."""
    crm = _adapter(kind)
    now = datetime.now(UTC)
    pool = kind in POOL_KINDS
    key = POOL_KEY if pool else run_id
    companies: list[str] = []
    products: list[str] = []
    deals: list[str] = []
    async with crm:
        before = (await crm.clients(), await crm.open_deals()) if isinstance(crm, AmoCRM) else None
        try:
            if isinstance(crm, AmoCRM):
                company = await crm.find_company(CompanyIn(f"E2E Co {POOL_KEY}", inn=POOL_NUMBERS[0]))
                assert company, "the pool company is gone: the conformance run above recreates it"
            elif kind == "zoho":
                name, inn, tail = ZOHO_LOOKUP
                company = (await crm.find_or_create_company(CompanyIn(name, inn=inn, phone=_phone(tail)))).id
            else:
                company = await crm.create_company(CompanyIn(f"E2E Winback {run_id}"))
                companies.append(company)
            product = await _product(crm, f"E2E product {key}", [] if pool else products)
            have = {p.title: p for p in (await crm.purchases(company_id=company)).items}
            fresh = False
            for title, days, qty in PURCHASE_POOL:
                if pool and (title in have or await _linked_back(crm, company, title)):
                    continue
                line = DealLine(f"E2E product {key}", qty, Decimal(100), product_id=product)
                made = await crm.create_won_deal(
                    DealIn(title, (line,), company_id=company, tags=("e2e",)), now - timedelta(days=days)
                )
                fresh = True
                if not pool:
                    deals.append(made.id)
                    products.extend(made.product_ids)

            got = (await crm.purchases(company_id=company)).items
            mine = [p for p in got if p.title in {t for t, _, _ in PURCHASE_POOL}]
            assert [p.title for p in mine] == ["E2E purchase pool 2", "E2E purchase pool 1"], got
            for p, (_, days, qty) in zip(mine, reversed(PURCHASE_POOL), strict=True):
                assert p.company_id == company
                assert p.closed_at.date() <= now.date() and p.closed_at > now - timedelta(days=days + 2), p
                assert [(x.product_id, x.quantity) for x in p.lines] == [(product, qty)], p.lines
                assert all(x.unit_price == Decimal(100) for x in p.lines), p.lines
            assert (await crm.purchases(company_id=company, lines=False)).items[0].lines == ()
            named = await crm.client_names([company])
            assert list(named) == [company] and named[company].startswith("E2E"), named
            since = min(p.closed_at for p in mine) - timedelta(days=1)
            window = await crm.purchases(company_id=company, since=since, until=now + timedelta(days=1))
            assert {p.deal_id for p in mine} <= {p.deal_id for p in window.items}

            if kind == "zoho" and fresh:
                with capsys.disabled():
                    print(
                        f"\n{kind}: the pool's won deals created, the account is searched from the next run"
                    )
            else:
                wanted, cursor = {p.deal_id for p in mine}, None
                for _ in range(40):
                    page = await crm.purchases(
                        since=since, until=now + timedelta(days=1), lines=False, cursor=cursor, limit=50
                    )
                    wanted -= {p.deal_id for p in page.items}
                    cursor = page.next_cursor
                    if not wanted or cursor is None:
                        break
                assert not wanted, f"{kind}: won deals missing from the account's purchases: {wanted}"

            task = await crm.add_client_task(
                TaskIn(f"E2E win-back {run_id}", now + timedelta(days=1)), company_id=company
            )
            listed = {t.id: t for t in await crm.client_tasks(company_id=company)}
            assert task in listed and listed[task].text == f"E2E win-back {run_id}", listed
            await crm.discard_task(task)
            assert task not in {t.id for t in await crm.client_tasks(company_id=company)}
        finally:
            await crm.discard(
                Leftovers(deal_ids=tuple(deals), company_ids=tuple(companies), product_ids=tuple(products))
            )
        if before is not None and isinstance(crm, AmoCRM):
            assert (await crm.clients(), await crm.open_deals()) == before, (
                "no company, contact or open deal may be created in a technical account"
            )
