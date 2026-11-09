"""Load the purchase history of the win-back solution into the demo account of a CRM.

    uv run python scripts/load_winback_demo.py amocrm kommo bitrix24 zoho odoo
    uv run python scripts/load_winback_demo.py --check amocrm kommo bitrix24 zoho odoo

The buyers are the «Демо · …» companies of the voice solution (``load_voice_demo.py``); a company the
account does not have is skipped, never created (amoCRM and Kommo keep 100 contacts and companies in all).
Each purchase is a won deal «Демо · Покупка · <company> · <date>» with products of the demo catalog at the
account's catalog price, closed on its date. Two companies stopped buying a few cycles ago, two still buy on
their cycle, one has too short a history to judge. The plan is fixed to calendar dates: a run makes only the
purchases whose date has come and that are not there yet, so running it again later tops the history up and
the demo does not grow old.

amoCRM and Kommo: the open deals and the contacts and companies are counted before the first purchase and
after it; the run stops if either count moved (a technical account keeps 100 open deals). ``--check`` reads
the account as the solution will, all won deals of 24 months page by page, and prints who missed their cycle;
it fails when that is not the plan's answer (Zoho's search finds a deal minutes after it is written).
"""

import argparse
import asyncio
import hashlib
import itertools
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from load_demo_catalog import CURRENCY, adapter

from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.base import CRM
from alnuzum_crm.types import CompanyIn, DealIn, DealLine, Product, Purchase

TITLE = "Демо · Покупка · {company} · {day}"
#: purchases closer than this are one order
SAME_ORDER = 7
#: a company has missed its cycle when the days since its last order exceed both
TOLERANCE, MARGIN, MIN_ORDERS = 1.5, 14, 3


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


def verdict(days: list[date], today: date) -> tuple[str, int | None, int | None]:
    """«missed» / «on cycle» / «short history», the cycle in days and the days since the last order."""
    orders: list[date] = []
    for day in sorted(days):
        if not orders or (day - orders[-1]).days >= SAME_ORDER:
            orders.append(day)
    if len(orders) < MIN_ORDERS:
        return "short history", None, (today - orders[-1]).days if orders else None
    cycle = int(statistics.median((b - a).days for a, b in itertools.pairwise(orders)))
    since = (today - orders[-1]).days
    missed = since > TOLERANCE * cycle and since > cycle + MARGIN
    return ("missed" if missed else "on cycle"), cycle, since


async def catalog(crm: CRM) -> dict[str, Product]:
    found: dict[str, Product] = {}
    cursor: str | None = None
    while True:
        page = await crm.list_products(cursor=cursor, limit=200)
        found |= {p.sku: p for p in page.items if p.sku}
        if page.next_cursor is None:
            return found
        cursor = page.next_cursor


async def counts(crm: CRM) -> tuple[int, int] | None:
    return (await crm.clients(), await crm.open_deals()) if isinstance(crm, AmoCRM) else None


async def load(name: str, today: date) -> None:
    async with adapter(name) as crm:
        products = await catalog(crm)
        before = await counts(crm)
        print(f"{name}: contacts+companies and open deals before: {before}")
        made = 0
        for buyer in BUYERS:
            company = await crm.find_company(CompanyIn(buyer.company))
            if company is None:
                print(f"{name}: no company {buyer.company!r}, skipped")
                continue
            have = {p.title for p in (await crm.purchases(company_id=company, lines=False, limit=1000)).items}
            for day, basket in plan(buyer, today):
                title = TITLE.format(company=buyer.company, day=day.isoformat())
                if title in have:
                    continue
                lines = tuple(
                    DealLine(
                        products[sku].name, Decimal(qty), products[sku].price or Decimal(0), products[sku].id
                    )
                    for sku, qty in basket
                )
                deal = DealIn(
                    title,
                    lines,
                    company_id=company,
                    currency=None if name == "odoo" else CURRENCY[name],
                    tags=("demo",),
                )
                await crm.create_won_deal(
                    deal, datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=7)
                )
                made += 1
                if made == 1 and before is not None:
                    after = await counts(crm)
                    if after != before:
                        sys.exit(
                            f"{name}: counts moved after the first purchase: {before} -> {after}, stopped"
                        )
        print(f"{name}: {made} purchases added, counts after: {await counts(crm)}")


async def check(name: str, today: date) -> bool:
    async with adapter(name) as crm:
        names: dict[str, str] = {}
        for buyer in BUYERS:
            company = await crm.find_company(CompanyIn(buyer.company))
            if company is not None:
                names[company] = buyer.company
        since = datetime.combine(today - timedelta(days=730), datetime.min.time(), UTC)
        bought: dict[str, list[Purchase]] = {}
        cursor: str | None = None
        for _ in range(200):
            page = await crm.purchases(since=since, lines=False, cursor=cursor, limit=200)
            for p in page.items:
                if p.company_id in names and p.title.startswith("Демо · Покупка · "):
                    bought.setdefault(p.company_id, []).append(p)
            cursor = page.next_cursor
            if cursor is None:
                break
        missed: set[str] = set()
        for company_id, company in names.items():
            got = bought.get(company_id, [])
            kind, cycle, days = verdict([p.closed_at.date() for p in got], today)
            print(
                f"{name}: {company}: {len(got)} purchases, {kind}, cycle {cycle}, {days} days since the last"
            )
            if kind == "missed":
                missed.add(company)
        expected = {b.company for b in BUYERS if b.lapsed and b.company in names.values()}
        ok = missed == expected
        result = "ok" if ok else "WRONG"
        print(f"{name}: missed their cycle {sorted(missed)}, expected {sorted(expected)}: {result}")
        return ok


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("crm", nargs="+", choices=sorted(CURRENCY))
    parser.add_argument(
        "--check", action="store_true", help="read the account and print who missed the cycle"
    )
    args = parser.parse_args()
    today = datetime.now(UTC).date()
    if args.check:
        results = await asyncio.gather(*(check(name, today) for name in args.crm))
        if not all(results):
            sys.exit(1)
    else:
        await asyncio.gather(*(load(name, today) for name in args.crm))


if __name__ == "__main__":
    asyncio.run(main())
