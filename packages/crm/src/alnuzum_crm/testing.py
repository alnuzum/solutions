"""Record live CRM traffic, replay it in tests, and the conformance scenario every adapter passes.

A recording keeps only what a test needs to replay: method, URL, a short description of a
JSON-RPC call, status, content type and body. Request bodies and headers (tokens) are never kept,
and every known secret and host is replaced by a placeholder before anything is written.
"""

import asyncio
import base64
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.base import CRM
from alnuzum_crm.errors import CrmNotFound
from alnuzum_crm.types import (
    CompanyIn,
    ContactIn,
    DealHit,
    DealIn,
    DealLine,
    DealStatus,
    DealUpdate,
    FileIn,
    Leftovers,
    Product,
    ProductIn,
    Ref,
    TaskIn,
)

Describe = Callable[[httpx.Request], str]


def describe_jsonrpc(request: httpx.Request) -> str:
    """ "object.execute_kw crm.lead create" for an Odoo JSON-RPC call, "" for anything else."""
    try:
        body = json.loads(request.content or b"null")
        params = body["params"]
        args = params.get("args") or []
        op = f"{params['service']}.{params['method']}"
        if params["method"] == "execute_kw":
            op += f" {args[3]} {args[4]}"
        return str(op)
    except (ValueError, KeyError, TypeError, IndexError):
        return ""


@dataclass
class Scrubber:
    """Replaces secrets and real hosts with placeholders; longer values first."""

    replacements: dict[str, str] = field(default_factory=dict)
    patterns: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, text: str) -> str:
        for value in sorted(self.replacements, key=len, reverse=True):
            if value:
                text = text.replace(value, self.replacements[value])
        for pattern, repl in self.patterns:
            text = re.sub(pattern, repl, text)
        return text


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, *, describe: Describe | None = None) -> None:
        self.inner = inner
        self.describe = describe or (lambda _r: "")
        self.calls: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        op = self.describe(request)
        resp = await self.inner.handle_async_request(request)
        body = await resp.aread()
        ctype = resp.headers.get("content-type", "")
        is_text = "json" in ctype or ctype.startswith("text/") or not body
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "op": op,
                "status": resp.status_code,
                "content_type": ctype,
                "body": body.decode() if is_text else base64.b64encode(body).decode(),
                "binary": not is_text,
            }
        )
        return httpx.Response(
            resp.status_code, headers={"content-type": ctype}, content=body, request=request
        )

    async def aclose(self) -> None:
        await self.inner.aclose()

    def save(self, path: Path, meta: dict[str, Any], scrub: Scrubber) -> None:
        calls = [
            {**c, "url": scrub(c["url"]), "body": c["body"] if c["binary"] else scrub(c["body"])}
            for c in self.calls
        ]
        doc = {"meta": meta, "calls": calls}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


class ReplayMismatch(AssertionError):
    pass


class Replayer(httpx.AsyncBaseTransport):
    """Serves a recording in order; a request that differs from the recorded one fails the test."""

    def __init__(self, path: Path, *, describe: Describe | None = None) -> None:
        doc = json.loads(path.read_text())
        self.meta: dict[str, Any] = doc["meta"]
        self.calls: list[dict[str, Any]] = doc["calls"]
        self.describe = describe or (lambda _r: "")
        self.position = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.position >= len(self.calls):
            raise ReplayMismatch(f"unexpected request #{self.position + 1}: {request.method} {request.url}")
        call = self.calls[self.position]
        got = (request.method, str(request.url), self.describe(request))
        want = (call["method"], call["url"], call["op"])
        if got != want:
            raise ReplayMismatch(f"request #{self.position + 1}: got {got}, recorded {want}")
        self.position += 1
        body = base64.b64decode(call["body"]) if call["binary"] else call["body"].encode()
        return httpx.Response(
            call["status"], headers={"content-type": call["content_type"]}, content=body, request=request
        )

    @property
    def exhausted(self) -> bool:
        return self.position == len(self.calls)


class ConformanceError(AssertionError):
    pass


def _check(ok: bool, what: str) -> None:
    if not ok:
        raise ConformanceError(what)


Sleep = Callable[[float], Awaitable[None]]

#: a record id that does not exist in the test accounts, per CRM
MISSING_IDS = {"amocrm": "999999999", "kommo": "999999999", "bitrix24": "999999999", "odoo": "999999999"}
MISSING_IDS["zoho"] = "111111111111111111"


@dataclass
class Collected:
    deal_ids: list[str] = field(default_factory=list)
    contact_ids: list[str] = field(default_factory=list)
    company_ids: list[str] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)

    def leftovers(self) -> Leftovers:
        return Leftovers(
            tuple(self.deal_ids),
            tuple(self.contact_ids),
            tuple(self.company_ids),
            tuple(self.product_ids),
            tuple(self.task_ids),
        )


async def _poll[T](
    probe: Callable[[], Awaitable[T | None]], sleep: Sleep, *, tries: int = 30, pause: float = 3.0
) -> T | None:
    """Search indexes of some CRMs (Zoho, the catalog of Kommo) lag behind writes by seconds to minutes."""
    for attempt in range(tries):
        found = await probe()
        if found is not None:
            return found
        if attempt + 1 < tries:
            await sleep(pause)
    return None


#: Tries of 3 s for a new product in the catalog search: Kommo's index took 8 to 41 s on a quiet account,
#: 50 to 117 s for ten minutes after a bulk write and more than 110 s in three runs of CI on 09.11
CATALOG_TRIES = 100


# no operator in Uzbekistan has the code 00: a test phone can never be somebody's number
PHONE_CODE = "00"


def run_numbers(run_id: str) -> tuple[str, str]:
    """INN (9 digits) and phone tail (7 digits) unique to a run: amoCRM never deletes contacts,
    so a fixed test phone would match the contacts of earlier runs."""
    n = int(run_id, 16)
    return f"3{n % 10**8:08d}", f"{n % 10**7:07d}"


#: A technical amoCRM or Kommo account keeps 100 contacts and companies in all, and its API deletes
#: none (405): the amoCRM family runs on a pool, one company and two contacts with fixed details that
#: every run finds instead of creating its own.
POOL_KEY = "pool"
POOL_NUMBERS = ("390000001", "9000001")
TECH_LIMIT = 100
#: the run stops here, before the CRM answers "Error code 205" to every new contact or company
TECH_GUARD = 90


async def clients_in_account(crm: AmoCRM) -> int:
    """Contacts and companies of an amoCRM or Kommo account, counted page by page."""
    return await crm.clients()


def check_room(count: int) -> None:
    """A plain failure near the limit instead of a "205" in the middle of the scenario."""
    _check(
        count < TECH_GUARD,
        f"account near the technical limit: {count}/{TECH_LIMIT}, "
        "clean up in the browser (see README.md, Tests)",
    )


def _amo(crm: CRM) -> AmoCRM:
    if not isinstance(crm, AmoCRM):
        raise ConformanceError(f"the pool is for the amoCRM family, not {crm.kind.value}")
    return crm


async def _unlink(crm: AmoCRM, owner: str, owner_id: str, key: str) -> None:
    """Unlink every company or deal (``key``) from a pooled contact or company."""
    params = {"with": "leads"} if key == "leads" else None
    found = await crm._request("GET", f"/api/v4/{owner}/{int(owner_id)}", params=params)
    ids = [int(x["id"]) for x in (found or {}).get("_embedded", {}).get(key, [])]
    if ids:
        body = [{"to_entity_id": i, "to_entity_type": key} for i in ids]
        await crm._request("POST", f"/api/v4/{owner}/{int(owner_id)}/unlink", json=body)


async def _in_catalog(crm: CRM, name: str) -> str | None:
    return next((p.id for p in (await crm.list_products(query=name)).items if p.name == name), None)


async def _found(crm: CRM, text: str, deal_id: str) -> DealHit | None:
    return next((h for h in await crm.find_deals(text) if h.id == deal_id), None)


async def run_conformance(
    crm: CRM,
    run_id: str,
    *,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
    keep: bool = False,
    pool: bool = False,
) -> list[str]:
    """Create everything, read it back, remove it. Returns the names of the passed steps.

    With ``pool`` (the amoCRM family) the company, the contacts and the catalog product are the pool's:
    found, created only when they are gone, never discarded; deals, their new products, files, notes and
    tasks are the run's own."""
    done: list[str] = []
    got = Collected()
    amo = _amo(crm) if pool else None
    key = POOL_KEY if pool else run_id
    inn, tail = POOL_NUMBERS if pool else run_numbers(run_id)
    phone = f"+998 {PHONE_CODE} {tail[:3]} {tail[3:5]} {tail[5:]}"
    name = f"E2E Co {key}"
    email = f"e2e-{key}@example.com"
    try:
        co = await crm.find_or_create_company(CompanyIn(name=name, inn=inn, phone=phone))
        if pool and not co.created:
            done.append("company found in the pool")
        else:
            _check(co.created, "a new company is created")
            done.append("company created")
        if not pool:
            got.company_ids.append(co.id)

        found = await _poll(lambda: crm.find_company(CompanyIn(name=name.upper())), sleep)
        _check(found == co.id, f"company found by name in another case ({found!r} != {co.id!r})")
        again = await crm.find_or_create_company(CompanyIn(name=name.upper()))
        _check(again == Ref(co.id, False), "find_or_create reuses the company")
        done.append("company deduplicated by name")
        if "inn" in crm.capabilities:
            renamed = CompanyIn(name=f"E2E other {run_id}", inn=inn)
            found = await _poll(lambda: crm.find_company(renamed), sleep)
            _check(found == co.id, "company found by INN under another name")
            other_inn = f"4{inn[1:]}"
            _check(
                await crm.find_company(CompanyIn(name=name, inn=other_inn)) is None,
                "a company with the same name and another INN is another legal entity",
            )
            done.append("company deduplicated by INN")

        ct = await crm.find_or_create_contact(
            ContactIn(name=f"E2E Person {key}", email=email, phone=phone), company_id=co.id
        )
        _check(ct.created or pool, "a new contact is created")
        if not pool:
            got.contact_ids.append(ct.id)
        found = await _poll(lambda: crm.find_contact(ContactIn(name="x", email=email.upper())), sleep)
        _check(found == ct.id, "contact found by e-mail in another case")
        other_format = f"998{PHONE_CODE}{tail}"
        found = await _poll(lambda: crm.find_contact(ContactIn(name="x", phone=other_format)), sleep)
        _check(found == ct.id, "contact found by phone in another format")
        again = await crm.find_or_create_contact(ContactIn(name="Someone", email=email))
        _check(again == Ref(ct.id, False), "find_or_create reuses the contact")
        done.append("contact deduplicated by e-mail and phone")

        lone_email = f"e2e-lone-{key}@example.com"
        lone_in = ContactIn(name=f"E2E Lone {key}", email=lone_email)
        lone = await crm.find_contact(lone_in) if amo else None
        if amo and lone:
            # the pool's lone contact keeps the company of the last run: free it to check the link again
            await _unlink(amo, "contacts", lone, "companies")
        else:
            lone = await crm.create_contact(lone_in)
            if not pool:
                got.contact_ids.append(lone)
            await _poll(lambda: crm.find_contact(ContactIn(name="x", email=lone_email)), sleep)
        linked = await crm.find_or_create_contact(ContactIn(name="x", email=lone_email), company_id=co.id)
        _check(linked == Ref(lone, False), "the contact without a company is found")
        _check(not await crm.link_contact(lone, co.id), "the found contact is now linked to the company")
        done.append("found contact linked to the company")
        if amo:
            # find_deals reads 16 deals of a company or a contact: the pool's deals of earlier runs go
            for owner, owner_id in (("companies", co.id), ("contacts", ct.id)):
                await _unlink(amo, owner, owner_id, "leads")

        about = f"Категория: E2E\nСинонимы: e2e {key}"
        product = f"E2E product {key}"
        # the pool's product sits in the catalog search long since: a new one waits out Kommo's index
        product_id = await _in_catalog(crm, product) if pool else None
        if product_id is None:
            product_id = await crm.create_product(
                ProductIn(
                    product,
                    Decimal(100),
                    f"E2E-{key}",
                    unit="шт",
                    category="E2E",
                    description=about,
                    stock=Decimal(7),
                )
            )
            if not pool:
                got.product_ids.append(product_id)
        lines = (
            DealLine(product, Decimal("2.5"), Decimal(100), product_id=product_id),
            DealLine(f"E2E service {run_id}", Decimal(3), Decimal("10.25")),
        )
        deal_in = DealIn(f"E2E deal {run_id}", lines, company_id=co.id, contact_id=ct.id, tags=("e2e",))
        created = await crm.create_deal(deal_in)
        got.deal_ids.append(created.id)
        got.product_ids.extend(created.product_ids)
        done.append("deal with lines created")

        await crm.attach_file(created.id, FileIn(f"e2e-{run_id}.txt", b"hello from e2e\n", "text/plain"))
        done.append("file attached")
        await crm.add_note(created.id, f"E2E note {run_id}")
        done.append("note added")
        due = (now or datetime.now(UTC)) + timedelta(days=1)
        got.task_ids.append(await crm.add_task(created.id, TaskIn(f"E2E task {run_id}", due)))
        done.append("task added")

        deal = await crm.get_deal(created.id)
        _check(deal.title == deal_in.title, "deal title reads back")
        _check(deal.status is DealStatus.OPEN, "new deal is open")
        _check(
            sorted((d.quantity, d.unit_price) for d in deal.lines)
            == sorted((d.quantity, d.unit_price) for d in lines),
            f"deal lines read back: {deal.lines!r}",
        )
        # amoCRM keeps whole numbers and recounts the budget from linked products, dropping kopecks
        exact = "fractional_amount" in crm.capabilities
        diff = abs((deal.amount or Decimal(0)) - deal_in.amount)
        _check(diff == 0 if exact else diff < 1, f"deal amount {deal.amount} vs {deal_in.amount}")
        done.append("deal read back with lines")

        by_company = [d.id for d in await crm.deal_history(company_id=co.id)]
        by_contact = [d.id for d in await crm.deal_history(contact_id=ct.id)]
        _check(created.id in by_company and created.id in by_contact, "deal is in the client's history")
        done.append("deal history read")

        # the voice of a manager: find the deal by its title, company and contact, move it, add products
        stages = await crm.stages()
        _check(
            any(x.kind is DealStatus.OPEN for x in stages) and any(x.kind is DealStatus.WON for x in stages),
            f"stages read: {stages!r}",
        )
        done.append("stages read")
        for text in (deal_in.title, name, f"E2E Person {key}"):
            found_deal = await _poll(lambda text=text: _found(crm, text, created.id), sleep)  # type: ignore[misc]
            _check(found_deal is not None, f"deal found by {text!r}")
        done.append("deal found by title, company and contact")
        current = next((x for x in stages if x.id == deal.stage_id), None)
        _check(current is not None, f"the deal's stage {deal.stage_id!r} is among the stages")
        target = next(
            x
            for x in stages
            if x.kind is DealStatus.OPEN
            and x.id != deal.stage_id
            and current
            and x.pipeline == current.pipeline
        )
        await crm.add_lines(created.id, (DealLine(product, Decimal(4), Decimal(100), product_id=product_id),))
        close = (
            (now or datetime.now(UTC)).date() + timedelta(days=30)
            if "close_date" in crm.capabilities
            else None
        )
        await crm.update_deal(
            created.id, DealUpdate(stage_id=target.id, amount=Decimal(4321), close_date=close)
        )
        after = await crm.get_deal(created.id)
        _check(after.stage_id == target.id, f"stage {after.stage_id!r} after the move to {target.id!r}")
        _check(after.amount == Decimal(4321), f"amount {after.amount} after the update")
        added = sum((x.quantity for x in after.lines if x.product_id == product_id), Decimal(0))
        _check(added == Decimal("6.5"), f"the product's quantity {added} after adding 4 to 2.5")
        done.append("deal moved, amount set, products added")

        async def catalog_hit() -> Product | None:
            page = await crm.list_products(query=product)
            return next((p for p in page.items if p.id == product_id), None)

        hit = await _poll(catalog_hit, sleep, tries=CATALOG_TRIES)
        _check(hit is not None, "product found in the catalog")
        _check(hit is not None and hit.price in (None, Decimal(100)), f"catalog price {hit and hit.price}")
        _check(hit is not None and hit.description == about, f"description {hit and hit.description!r}")
        _check(hit is not None and bool(hit.unit), f"catalog unit {hit and hit.unit!r}")
        if "stock" in crm.capabilities:
            _check(hit is not None and hit.stock == Decimal(7), f"catalog stock {hit and hit.stock}")
        done.append("catalog read")

        try:
            await crm.get_deal(MISSING_IDS[crm.kind.value])
        except CrmNotFound:
            done.append("missing deal is CrmNotFound")
        else:
            raise ConformanceError("a missing deal raises CrmNotFound")
    finally:
        if not keep:
            await crm.discard(got.leftovers())
    if not keep:
        done.append("test records removed")
    return done
