"""The purchase history of the demo: five «Демо» companies of the voice solution, two of which stopped buying
a few cycles ago, two still buy on their cycle and one has too short a history to judge.

Each purchase is a won deal «Демо · Покупка · <company> · <date>» with products of the demo catalog at the
account's catalog price. The plan is fixed to calendar dates: a run makes only the purchases whose date has
come and that are not there yet, so a run every day tops the history up and the demo does not grow old. A
company the account does not have is skipped, never created (amoCRM and Kommo keep 100 contacts and companies
in all), and there the open deals and the clients are counted around the first purchase: a run stops if either
moved."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.base import CRM
from alnuzum_crm.types import CompanyIn, DealIn, DealLine, Product

log = logging.getLogger("alnuzum_winback.demo")

PREFIX = "Демо · Покупка · "
TITLE = PREFIX + "{company} · {day}"
#: the currency of the demo deals per CRM (Odoo takes the company's own)
CURRENCY = {"amocrm": "UZS", "kommo": "UZS", "bitrix24": "RUB", "zoho": "UZS", "odoo": None}


@dataclass(frozen=True)
class Buyer:
    company: str
    first: date
    cycle: int
    #: the day it stopped buying (its last purchase is before it), None while it still buys
    stopped: date | None
    basket: tuple[tuple[str, int], ...]
    lapsed: bool


SEED = date(2026, 11, 10)
BUYERS = (
    Buyer(
        "Ромашка",
        SEED - timedelta(days=365),
        30,
        SEED - timedelta(days=75),
        (("EL-AV-001", 20), ("EL-UZ-001", 4), ("EL-CB-002", 200), ("EL-SH-002", 2)),
        lapsed=True,
    ),
    Buyer(
        "Navruz Stroy",
        SEED - timedelta(days=360),
        45,
        None,
        (("EL-SV-001", 60), ("EL-SV-003", 40), ("EL-CB-001", 300)),
        lapsed=False,
    ),
    Buyer(
        "Samarqand Tekstil",
        SEED - timedelta(days=365),
        60,
        SEED - timedelta(days=150),
        (("EL-CB-004", 500), ("EL-GF-002", 200), ("EL-KR-001", 30)),
        lapsed=True,
    ),
    Buyer(
        "Bahor Market",
        SEED - timedelta(days=300),
        21,
        None,
        (("EL-RZ-001", 40), ("EL-RZ-003", 30), ("EL-KL-001", 100)),
        lapsed=False,
    ),
    Buyer(
        "Tashkent City Mall",
        SEED - timedelta(days=260),
        90,
        SEED - timedelta(days=100),
        (("EL-SV-002", 80), ("EL-UD-003", 10)),
        lapsed=False,
    ),
)
NAMES = frozenset(b.company for b in BUYERS)


def _share(*parts: object) -> float:
    """A stable number in [0, 1) for the same inputs: the plan is the same on every run and machine."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def plan(buyer: Buyer, today: date) -> list[tuple[date, tuple[tuple[str, int], ...]]]:
    """The purchases of a buyer up to ``today``: intervals of its cycle ±20 %, two or three products of its
    basket each time, quantities ±30 %."""
    out: list[tuple[date, tuple[tuple[str, int], ...]]] = []
    day, n = buyer.first, 0
    end = min(today, buyer.stopped) if buyer.stopped else today
    while day <= end:
        picked = [
            (sku, max(1, round(qty * (0.7 + 0.6 * _share(buyer.company, n, sku)))))
            for i, (sku, qty) in enumerate(buyer.basket)
            if i < 2 or _share(buyer.company, n, "pick", sku) < 0.5
        ]
        out.append((day, tuple(picked)))
        n += 1
        day += timedelta(days=round(buyer.cycle * (0.8 + 0.4 * _share(buyer.company, n, "gap"))))
    return out


def expected(present: set[str]) -> set[str]:
    """Who of the companies an account has must be found missing their cycle."""
    return {b.company for b in BUYERS if b.lapsed and b.company in present}


async def catalog(crm: CRM) -> dict[str, Product]:
    found: dict[str, Product] = {}
    cursor: str | None = None
    for _ in range(50):
        page = await crm.list_products(cursor=cursor, limit=200)
        found |= {p.sku: p for p in page.items if p.sku}
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return found


class CountsMoved(RuntimeError):
    """A technical account counted other clients or open deals after a purchase was written: stopped."""


async def _counts(crm: CRM) -> tuple[int, int] | None:
    return (await crm.clients(), await crm.open_deals()) if isinstance(crm, AmoCRM) else None


async def seed(crm: CRM, kind: str, day: date) -> int:
    """Write the purchases of the plan up to ``day`` that the account does not have yet; returns how many."""
    products: dict[str, Product] | None = None
    before: tuple[int, int] | None = None
    made = 0
    for buyer in BUYERS:
        company = await crm.find_company(CompanyIn(buyer.company))
        if company is None:
            continue
        have = {p.title for p in (await crm.purchases(company_id=company, lines=False, limit=1000)).items}
        for when, basket in plan(buyer, day):
            title = TITLE.format(company=buyer.company, day=when.isoformat())
            if title in have:
                continue
            if products is None:
                products = await catalog(crm)
                before = await _counts(crm)
            lines = tuple(
                DealLine(
                    products[sku].name, Decimal(qty), products[sku].price or Decimal(0), products[sku].id
                )
                for sku, qty in basket
                if sku in products
            )
            deal = DealIn(title, lines, company_id=company, currency=CURRENCY.get(kind), tags=("demo",))
            await crm.create_won_deal(
                deal, datetime.combine(when, datetime.min.time(), UTC) + timedelta(hours=7)
            )
            made += 1
            if made == 1 and before is not None:
                after = await _counts(crm)
                if after != before:
                    raise CountsMoved(f"{kind}: {before} -> {after}")
    return made
