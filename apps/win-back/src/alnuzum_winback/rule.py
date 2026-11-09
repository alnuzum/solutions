"""The rule of the cycle: who missed their order, by how much and why. Every number is counted from the dates
and sums of won deals, nothing is written by a model; each reason names the orders it rests on.

A client's purchases of 24 months become orders: a deal closed within 7 days of the first deal of an order is
part of it. With 3 orders or more the cycle is the median of the intervals between them; the client has missed
it when the days since the last order exceed both 1.5 cycles and the cycle plus 14 days. The orders missed are
the whole cycles that fit into those days; what they would have brought is the average order times them."""

import itertools
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from alnuzum_crm.types import Purchase

TZ = ZoneInfo("Asia/Tashkent")
HISTORY_DAYS = 730
SAME_ORDER = 7
TOLERANCE, MARGIN, MIN_ORDERS = Decimal("1.5"), 14, 3
#: a product is a habit when it was in at least half of the orders before the last one, and in two at least
HABIT_SHARE, HABIT_MIN = Decimal("0.5"), 2
#: the last order is small when below this share of the median of the ones before it
SMALL_SHARE = Decimal("0.7")
MAX_GONE = 3


@dataclass(frozen=True)
class Line:
    key: str
    name: str
    quantity: Decimal
    total: Decimal


@dataclass(frozen=True)
class Order:
    day: date
    amount: Decimal
    deals: tuple[tuple[str, str], ...]  # (deal id, title)
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True)
class Verdict:
    kind: str  # "missed", "on_cycle" or "short"
    orders: int
    cycle: int | None
    since: int | None  # days since the last order
    last: date | None
    missed: int  # whole cycles since the last order, when missed
    average: Decimal
    lost: Decimal  # the average order times the cycles missed

    @property
    def cycles(self) -> Decimal:
        """The days since the last order in cycles, one decimal (2.8)."""
        if not self.cycle or self.since is None:
            return Decimal(0)
        return (Decimal(self.since) / Decimal(self.cycle)).quantize(Decimal("0.1"), ROUND_HALF_UP)

    @property
    def next_day(self) -> date | None:
        """When the next order is due by the cycle."""
        return self.last + timedelta(days=self.cycle) if self.last and self.cycle else None


@dataclass(frozen=True)
class Reason:
    """``refs`` are the indexes of the orders (oldest first) the reason rests on."""

    kind: str  # "cycle", "gone", "small", "short", "on_cycle"
    refs: tuple[int, ...]
    data: dict[str, Any] = field(default_factory=dict)


def local_day(moment: datetime) -> date:
    return moment.astimezone(TZ).date() if moment.tzinfo else moment.date()


def today(now: datetime) -> date:
    return now.astimezone(TZ).date()


def _key(line: Any) -> str:
    return f"p:{line.product_id}" if line.product_id else "n:" + " ".join(str(line.name).casefold().split())


def orders_of(purchases: Iterable[Purchase]) -> list[Order]:
    """Purchases of one client into orders, oldest first: a deal within ``SAME_ORDER`` days of the first deal
    of an order joins it; the products of an order are summed by product (or by name where there is no id)."""
    ordered = sorted(purchases, key=lambda p: (local_day(p.closed_at), p.deal_id))
    groups: list[list[Purchase]] = []
    for p in ordered:
        if groups and (local_day(p.closed_at) - local_day(groups[-1][0].closed_at)).days < SAME_ORDER:
            groups[-1].append(p)
        else:
            groups.append([p])
    out: list[Order] = []
    for group in groups:
        lines: dict[str, Line] = {}
        for p in group:
            for x in p.lines:
                k = _key(x)
                had = lines.get(k)
                qty = (had.quantity if had else Decimal(0)) + x.quantity
                total = (had.total if had else Decimal(0)) + x.total
                lines[k] = Line(k, had.name if had else str(x.name), qty, total)
        out.append(
            Order(
                local_day(group[0].closed_at),
                sum((p.amount or Decimal(0) for p in group), Decimal(0)),
                tuple((p.deal_id, p.title) for p in group),
                tuple(lines.values()),
            )
        )
    return out


def verdict(orders: Sequence[Order], day: date) -> Verdict:
    """«missed», «on_cycle» or «short» (fewer than ``MIN_ORDERS`` orders) on ``day``."""
    if not orders:
        return Verdict("short", 0, None, None, None, 0, Decimal(0), Decimal(0))
    last = orders[-1].day
    since = (day - last).days
    average = (sum((o.amount for o in orders), Decimal(0)) / len(orders)).quantize(Decimal("0.01"))
    if len(orders) < MIN_ORDERS:
        return Verdict("short", len(orders), None, since, last, 0, average, Decimal(0))
    cycle = int(statistics.median((b.day - a.day).days for a, b in itertools.pairwise(orders)))
    cycle = max(cycle, 1)
    if since > TOLERANCE * cycle and since > cycle + MARGIN:
        missed = since // cycle
        return Verdict("missed", len(orders), cycle, since, last, missed, average, average * missed)
    return Verdict("on_cycle", len(orders), cycle, since, last, 0, average, Decimal(0))


def reasons(orders: Sequence[Order], seen: Verdict) -> list[Reason]:
    """Why the client is on the list, each with the orders it rests on: the cycle; the products bought out of
    habit that the last order left out (only when the products were read); a last order well below the usual.
    """
    every = tuple(range(len(orders)))
    if seen.kind == "short":
        return [Reason("short", every, {"orders": len(orders)})]
    if seen.kind == "on_cycle":
        return [Reason("on_cycle", (len(orders) - 1,), {"cycle": seen.cycle, "next": seen.next_day})]
    out = [
        Reason(
            "cycle",
            every,
            {
                "cycle": seen.cycle,
                "orders": seen.orders,
                "last": seen.last,
                "since": seen.since,
                "cycles": seen.cycles,
            },
        )
    ]
    before, last = orders[:-1], orders[-1]
    lined = [i for i, o in enumerate(before) if o.lines]  # orders whose products were read
    if last.lines and lined:
        counted: dict[str, list[int]] = {}
        names: dict[str, str] = {}
        for i in lined:
            for x in before[i].lines:
                counted.setdefault(x.key, []).append(i)
                names.setdefault(x.key, x.name)
        in_last = {x.key for x in last.lines}
        need = max(HABIT_MIN, math.ceil(HABIT_SHARE * len(lined)))
        habits = sorted(
            ((k, ix) for k, ix in counted.items() if k not in in_last and len(ix) >= need),
            key=lambda kv: (-len(kv[1]), names[kv[0]]),
        )
        for k, ix in habits[:MAX_GONE]:
            out.append(
                Reason(
                    "gone",
                    (*ix, len(orders) - 1),
                    {"name": names[k], "had": len(ix), "of": len(lined)},
                )
            )
    usual = Decimal(statistics.median(o.amount for o in before)) if before else Decimal(0)
    if usual > 0 and last.amount < SMALL_SHARE * usual:
        share = int(((usual - last.amount) / usual * 100).quantize(Decimal(1), ROUND_HALF_UP))
        out.append(
            Reason(
                "small",
                (len(orders) - 1, *range(len(before))),
                {"amount": last.amount, "usual": usual, "less": share},
            )
        )
    return out


def due(now: datetime) -> datetime:
    """The next working day (Monday to Friday) at 18:00 in Tashkent."""
    day = now.astimezone(TZ).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(18), TZ)
