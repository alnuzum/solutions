"""The deal in the CRM: client, contact, deal with product lines, the note with the calculation, the
task for what is left, then the deal read back from the CRM."""

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


async def write(crm: CRM, plan: Plan, customer: Customer, *, lang: str, tags: tuple[str, ...]) -> Written:
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
    await crm.add_note(created.id, plan.note)
    if plan.task:
        await crm.add_task(created.id, TaskIn(plan.task, datetime.now(UTC) + timedelta(days=1)))
    deal = await crm.get_deal(created.id)
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
    return Written(created.id, company_id, contact_id, readback)
