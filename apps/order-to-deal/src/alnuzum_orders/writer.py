"""The deal in the CRM: client, contact, deal with product lines, the note with the calculation, the
task for what is left, then the deal read back from the CRM."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alnuzum_crm.base import CRM
from alnuzum_crm.errors import CrmValidationError
from alnuzum_crm.types import CompanyIn, ContactIn, CrmKind, DealIn, TaskIn
from alnuzum_orders.engine import Plan
from alnuzum_orders.model import Customer

NO_REQUISITES = {"ru": "Заявка без реквизитов", "en": "Order without requisites"}


@dataclass(frozen=True)
class Written:
    deal_id: str
    company_id: str | None
    contact_id: str | None
    readback: dict[str, Any]


# a step done: its name, and the deal it made (for "deal")
Mark = Callable[[str, str | None], Awaitable[None]]


async def _nothing(step: str, deal_id: str | None) -> None:
    return None


async def write(
    crm: CRM,
    plan: Plan,
    customer: Customer,
    *,
    lang: str,
    tags: tuple[str, ...],
    done: frozenset[str] = frozenset(),
    deal_id: str | None = None,
    mark: Mark = _nothing,
) -> Written:
    """A retry after a failure passes the steps already ``done`` and the deal it made: it adds what is
    missing to that deal and never makes a second one."""
    if "deal" in done and deal_id:
        return await _rest(crm, plan, deal_id, None, None, done=done, mark=mark)
    if any(x.quantity <= 0 or x.unit_price <= 0 for x in plan.lines) or (
        plan.total is not None and plan.total < 0
    ):
        # refused before anything is written: a deal line is a positive quantity at a positive price
        raise CrmValidationError("a deal line without a positive quantity and price", crm=crm.kind.value)
    company_id = contact_id = None
    if customer.company:
        company_id = (
            await crm.find_or_create_company(
                CompanyIn(customer.company, inn=customer.inn, phone=customer.phone, email=customer.email)
            )
        ).id
    if customer.person or customer.email or customer.phone:
        contact = ContactIn(
            customer.person or customer.email or customer.phone or "", customer.email, customer.phone
        )
        contact_id = (await crm.find_or_create_contact(contact, company_id=company_id)).id
    if crm.kind is CrmKind.ODOO and plan.lines and not (company_id or contact_id):
        # an Odoo quotation needs a customer
        company_id = (
            await crm.find_or_create_company(CompanyIn(NO_REQUISITES.get(lang, NO_REQUISITES["en"])))
        ).id
    created = await crm.create_deal(
        DealIn(
            plan.title, plan.lines, company_id=company_id, contact_id=contact_id, tags=tags, total=plan.total
        )
    )
    await mark("deal", created.id)
    return await _rest(crm, plan, created.id, company_id, contact_id, done=done, mark=mark)


async def _rest(
    crm: CRM,
    plan: Plan,
    deal_id: str,
    company_id: str | None,
    contact_id: str | None,
    *,
    done: frozenset[str],
    mark: Mark,
) -> Written:
    if "note" not in done:
        await crm.add_note(deal_id, plan.note)
        await mark("note", None)
    if plan.task and "task" not in done:
        await crm.add_task(deal_id, TaskIn(plan.task, datetime.now(UTC) + timedelta(days=1)))
        await mark("task", None)
    deal = await crm.get_deal(deal_id)
    readback = {
        "id": deal.id,
        "title": deal.title,
        "amount": None if deal.amount is None else str(deal.amount),
        "currency": deal.currency,
        "lines": [
            {
                "name": ln.name,
                "sku": ln.sku,
                "quantity": str(ln.quantity),
                "price": str(ln.unit_price),
                "total": str((ln.quantity * ln.unit_price).quantize(Decimal("0.01"))),
            }
            for ln in deal.lines
        ],
        "note": bool(plan.note),
        "task": bool(plan.task),
    }
    return Written(deal_id, company_id, contact_id, readback)
