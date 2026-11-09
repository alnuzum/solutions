"""A scan of an account: the won deals of 24 months read page by page (dates and sums only), the clients who
missed their cycle found by the rule, the products read only for the client a visitor opens.

Reading stops at a ceiling of deals or of seconds and says how far it got. Zoho's search gives at most 2,000
records for one criterion, so there the period is read in quarters."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from alnuzum_crm.base import CRM, aware
from alnuzum_crm.types import Purchase
from alnuzum_winback import demo as plan
from alnuzum_winback.rule import HISTORY_DAYS, TZ, Order, orders_of, reasons, verdict

#: won deals a request: amoCRM 250, Bitrix24 50 (its own page), Zoho 200, Odoo 100
PAGE = {"amocrm": 250, "kommo": 250, "bitrix24": 50, "zoho": 200, "odoo": 100}
MAX_PAGES = 400
SHOWN = 50  # clients on the list
OTHERS = 12  # names of the clients on their cycle or with a short history


@dataclass(frozen=True)
class Read:
    purchases: list[Purchase]
    cut: str | None  # None, "cap" (deals) or "time" (seconds)


def period(day: date) -> tuple[datetime, datetime]:
    """24 months up to the end of ``day`` in Tashkent."""
    until = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ).astimezone(UTC)
    return until - timedelta(days=HISTORY_DAYS + 1), until


def windows(kind: str, since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """One window, or quarters of the period for Zoho (oldest first, back to back)."""
    if kind != "zoho":
        return [(since, until)]
    out: list[tuple[datetime, datetime]] = []
    lo = since
    while lo < until:
        hi = min(lo + timedelta(days=91), until)
        out.append((lo, hi))
        lo = hi
    return out


async def read_account(
    crm: CRM,
    kind: str,
    day: date,
    *,
    cap: int,
    seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> Read:
    since, until = period(day)
    got: dict[str, Purchase] = {}
    started = clock()
    try:
        async with asyncio.timeout(seconds):
            for lo, hi in windows(kind, since, until):
                cursor: str | None = None
                for _ in range(MAX_PAGES):
                    if clock() - started > seconds:
                        return Read(list(got.values()), "time")
                    page = await crm.purchases(
                        since=lo, until=hi, lines=False, cursor=cursor, limit=PAGE.get(kind, 100)
                    )
                    for p in page.items:
                        got.setdefault(p.deal_id, p)
                    if len(got) >= cap:
                        return Read(list(got.values())[:cap], "cap")
                    cursor = page.next_cursor
                    if cursor is None:
                        break
    except TimeoutError:
        return Read(list(got.values()), "time")
    return Read(list(got.values()), None)


def client_of(p: Purchase) -> str | None:
    """The client a purchase belongs to: its company, otherwise its contact."""
    if p.company_id:
        return f"company:{p.company_id}"
    if p.contact_id:
        return f"contact:{p.contact_id}"
    return None


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def currency_of(purchases: list[Purchase]) -> str | None:
    seen: dict[str, int] = {}
    for p in purchases:
        if p.currency:
            seen[p.currency] = seen.get(p.currency, 0) + 1
    return max(seen, key=lambda c: seen[c]) if seen else None


def only_demo(purchases: list[Purchase]) -> list[Purchase]:
    return [p for p in purchases if p.title.startswith(plan.PREFIX)]


def analyse(purchases: list[Purchase], day: date) -> dict[str, Any]:
    """The clients of a read, without names: the missed ones by what they would have brought (then by days),
    and counts of the rest. Runs in a thread: thousands of deals are sorted and grouped here."""
    by_client: dict[str, list[Purchase]] = {}
    for p in purchases:
        key = client_of(p)
        if key:
            by_client.setdefault(key, []).append(p)
    missed: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    counts = {"missed": 0, "on_cycle": 0, "short": 0}
    for key, bought in by_client.items():
        seen = verdict(orders_of(bought), day)
        counts[seen.kind] += 1
        row = {
            "key": key,
            "kind": seen.kind,
            "orders": seen.orders,
            "cycle": seen.cycle,
            "since": seen.since,
            "last": seen.last.isoformat() if seen.last else None,
            "missed": seen.missed,
            "cycles": str(seen.cycles),
            "average": _money(seen.average),
            "lost": _money(seen.lost),
        }
        (missed if seen.kind == "missed" else others).append(row)
    missed.sort(key=lambda r: (-Decimal(r["lost"]), -(r["since"] or 0), r["key"]))
    others.sort(key=lambda r: (r["kind"] != "on_cycle", r["since"] or 0, r["key"]))
    return {
        "day": day.isoformat(),
        "read": len(purchases),
        "currency": currency_of(purchases),
        "counts": counts,
        "clients": missed[:SHOWN],
        "others": others[:OTHERS],
    }


async def name_clients(crm: CRM, result: dict[str, Any], *, demo: bool) -> dict[str, Any]:
    """Names for the clients a scan shows; the demo keeps only the companies of its plan."""
    rows = [*result["clients"], *result["others"]]
    companies = [r["key"].split(":", 1)[1] for r in rows if r["key"].startswith("company:")]
    contacts = [r["key"].split(":", 1)[1] for r in rows if r["key"].startswith("contact:")]
    names = {f"company:{k}": v for k, v in (await crm.client_names(companies)).items()} if companies else {}
    if contacts:
        names |= {f"contact:{k}": v for k, v in (await crm.client_names(contacts, contacts=True)).items()}

    def keep(r: dict[str, Any]) -> bool:
        name = names.get(r["key"], "")
        return bool(name) and (not demo or (r["key"].startswith("company:") and name in plan.NAMES))

    named = {
        part: [r | {"name": names[r["key"]][:200]} for r in result[part] if keep(r)]
        for part in ("clients", "others")
    }
    for i, r in enumerate(named["clients"], 1):
        r["n"] = i
    return result | named


def detail(purchases: list[Purchase], day: date, *, demo: bool) -> dict[str, Any]:
    """The screen of one client: its orders with their products (newest first), the verdict and the reasons
    with the orders they rest on. Runs in a thread."""
    if demo:
        purchases = only_demo(purchases)
    since, _ = period(day)
    orders: list[Order] = orders_of(p for p in purchases if aware(p.closed_at) >= since)
    seen = verdict(orders, day)
    why = reasons(orders, seen)
    refs: dict[int, list[int]] = {}
    for r, reason in enumerate(why, 1):
        for i in reason.refs:
            refs.setdefault(i, []).append(r)
    shown = [
        {
            "i": i,
            "day": o.day.isoformat(),
            "amount": _money(o.amount),
            "deals": [{"id": d[:64], "title": t[:200]} for d, t in o.deals],
            "lines": [
                {"name": x.name[:200], "quantity": str(x.quantity.normalize()), "total": _money(x.total)}
                for x in o.lines
            ],
            "refs": refs.get(i, []),
        }
        for i, o in enumerate(orders)
    ]
    return {
        "currency": currency_of(purchases),
        "kind": seen.kind,
        "orders": list(reversed(shown)),
        "reasons": [
            {"r": r, "kind": x.kind, "refs": list(x.refs), "data": _plain(x.data)}
            for r, x in enumerate(why, 1)
        ],
    }


def _plain(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
