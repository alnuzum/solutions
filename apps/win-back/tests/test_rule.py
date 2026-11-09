"""The rule of the cycle on its edges: what is one order, when a client has missed, how much, why; the due date
of a task; how far a read of the account goes."""

import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from alnuzum_crm.types import DealLine, Page, Purchase
from alnuzum_winback import demo as plan
from alnuzum_winback.rule import TZ, Order, due, orders_of, reasons, verdict
from alnuzum_winback.scan import analyse, read_account, windows

DAY = date(2026, 11, 10)


def buy(
    day: date, amount: int = 100, *, company: str = "1", lines: tuple[DealLine, ...] = (), n: int = 0
) -> Purchase:
    moment = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=7)
    return Purchase(f"{day.isoformat()}-{n}", "x", moment, Decimal(amount), "UZS", company, None, lines)


def orders(*days_ago: int, amount: int = 100) -> list[Order]:
    return [Order(DAY - timedelta(days=d), Decimal(amount), ()) for d in sorted(days_ago, reverse=True)]


def test_deals_within_seven_days_of_the_first_are_one_order() -> None:
    first = date(2026, 1, 1)
    got = orders_of(
        [buy(first + timedelta(days=6), 50, n=2), buy(first), buy(first + timedelta(days=7), n=3)]
    )
    assert [(o.day, o.amount, len(o.deals)) for o in got] == [
        (first, Decimal(150), 2),
        (first + timedelta(days=7), Decimal(100), 1),
    ]


def test_an_order_sums_its_products_by_product() -> None:
    a = DealLine("Кабель", Decimal(2), Decimal(10), product_id="7")
    b = DealLine("кабель ", Decimal(1), Decimal(10))
    got = orders_of([buy(DAY, lines=(a, b)), buy(DAY, lines=(a,), n=1)])
    assert [(x.name, x.quantity, x.total) for x in got[0].lines] == [
        ("Кабель", Decimal(4), Decimal(40)),
        ("кабель ", Decimal(1), Decimal(10)),
    ]


def test_two_orders_are_too_short_a_history() -> None:
    seen = verdict(orders(400, 10), DAY)
    assert (seen.kind, seen.orders, seen.cycle, seen.since) == ("short", 2, None, 10)
    assert verdict([], DAY).kind == "short"


@pytest.mark.parametrize(
    ("since", "kind"),
    [(45, "on_cycle"), (46, "missed")],  # cycle 30: 1.5 cycles = 45 days; cycle + 14 = 44
)
def test_missed_needs_more_than_one_and_a_half_cycles(since: int, kind: str) -> None:
    assert verdict(orders(since + 60, since + 30, since), DAY).kind == kind


@pytest.mark.parametrize(
    ("since", "kind"),
    [(34, "on_cycle"), (35, "missed")],  # cycle 20: 1.5 cycles = 30 days; cycle + 14 = 34
)
def test_missed_needs_more_than_the_cycle_and_two_weeks(since: int, kind: str) -> None:
    assert verdict(orders(since + 40, since + 20, since), DAY).kind == kind


def test_the_cycle_is_the_median_and_the_loss_whole_cycles_of_the_average_order() -> None:
    seen = verdict(orders(181, 150, 119, 89) + orders(86, amount=400), DAY)
    # intervals 31, 31, 30, 3 → median 30.5 → 30 days
    assert seen.cycle == 30 and seen.since == 86 and seen.kind == "missed"
    assert seen.missed == 2 and seen.average == Decimal("160.00") and seen.lost == Decimal("320.00")
    assert str(seen.cycles) == "2.9" and seen.next_day == DAY - timedelta(days=56)


def test_reasons_name_their_orders() -> None:
    habit = DealLine("Кабель", Decimal(1), Decimal(100), product_id="7")
    once = DealLine("Лампа", Decimal(1), Decimal(100), product_id="8")
    other = DealLine("Розетка", Decimal(1), Decimal(10), product_id="9")
    history = [
        Order(
            DAY - timedelta(days=200),
            Decimal(1000),
            (),
            tuple(orders_of([buy(DAY, lines=(habit, other))])[0].lines),
        ),
        Order(
            DAY - timedelta(days=170),
            Decimal(1000),
            (),
            tuple(orders_of([buy(DAY, lines=(habit, once))])[0].lines),
        ),
        Order(
            DAY - timedelta(days=140),
            Decimal(1000),
            (),
            tuple(orders_of([buy(DAY, lines=(other,))])[0].lines),
        ),
        Order(
            DAY - timedelta(days=110), Decimal(690), (), tuple(orders_of([buy(DAY, lines=(other,))])[0].lines)
        ),
    ]
    seen = verdict(history, DAY)
    why = reasons(history, seen)
    assert [r.kind for r in why] == ["cycle", "gone", "small"]
    assert why[0].refs == (0, 1, 2, 3)
    # the cable was in 2 of the 3 orders before the last: a habit; the lamp in one only is not
    assert why[1].refs == (0, 1, 3) and why[1].data == {"name": "Кабель", "had": 2, "of": 3}
    assert why[2].refs[0] == 3 and why[2].data["less"] == 31


def test_a_last_order_at_seventy_percent_is_not_small() -> None:
    history = [*orders(200, 170, 140), Order(DAY - timedelta(days=110), Decimal(70), ())]
    assert [r.kind for r in reasons(history, verdict(history, DAY))] == ["cycle"]


def test_reasons_of_a_client_on_its_cycle_or_with_a_short_history() -> None:
    on = orders(60, 30, 10)
    assert [(r.kind, r.refs) for r in reasons(on, verdict(on, DAY))] == [("on_cycle", (2,))]
    short = orders(60, 30)
    assert [(r.kind, r.refs) for r in reasons(short, verdict(short, DAY))] == [("short", (0, 1))]


@pytest.mark.parametrize(
    ("utc", "local_due"),
    [
        (datetime(2026, 11, 10, 9, 0, tzinfo=UTC), datetime(2026, 11, 11, 18, 0)),  # Tuesday → Wednesday
        (datetime(2026, 11, 13, 9, 0, tzinfo=UTC), datetime(2026, 11, 16, 18, 0)),  # Friday → Monday
        (datetime(2026, 11, 14, 9, 0, tzinfo=UTC), datetime(2026, 11, 16, 18, 0)),  # Saturday → Monday
        (
            datetime(2026, 11, 12, 20, 0, tzinfo=UTC),
            datetime(2026, 11, 16, 18, 0),
        ),  # already Friday in Tashkent
    ],
)
def test_a_task_is_due_the_next_working_day_at_six_in_tashkent(utc: datetime, local_due: datetime) -> None:
    got = due(utc)
    assert got.tzinfo == TZ and got.replace(tzinfo=None) == local_due


def test_the_demo_plan_gives_its_answer() -> None:
    bought = [
        buy(day, company=b.company, n=i)
        for b in plan.BUYERS
        for i, (day, _basket) in enumerate(plan.plan(b, DAY))
    ]
    got = analyse(bought, DAY)
    assert {c["key"] for c in got["clients"]} == {"company:Ромашка", "company:Samarqand Tekstil"}
    assert [Decimal(c["lost"]) for c in got["clients"]] == sorted(
        (Decimal(c["lost"]) for c in got["clients"]), reverse=True
    )
    assert got["counts"] == {"missed": 2, "on_cycle": 2, "short": 1}
    assert plan.expected({b.company for b in plan.BUYERS}) == {"Ромашка", "Samarqand Tekstil"}
    assert plan.expected({"Ромашка", "Navruz Stroy"}) == {"Ромашка"}


def test_zoho_reads_the_period_in_quarters() -> None:
    since, until = datetime(2024, 11, 10, tzinfo=UTC), datetime(2026, 11, 11, tzinfo=UTC)
    got = windows("zoho", since, until)
    assert got[0][0] == since and got[-1][1] == until and len(got) == 9
    assert all(a[1] == b[0] for a, b in itertools.pairwise(got))
    assert windows("amocrm", since, until) == [(since, until)]


class Pages:
    def __init__(self, total: int, *, tick: float = 0.0) -> None:
        self.total, self.tick, self.t, self.asked = total, tick, 0.0, []  # type: ignore[var-annotated]

    def clock(self) -> float:
        return self.t

    async def purchases(self, **kw: Any) -> Page[Purchase]:
        self.asked.append(kw)
        self.t += self.tick
        start, size = int(kw["cursor"] or 0), kw["limit"]
        items = tuple(buy(DAY, n=i) for i in range(start, min(start + size, self.total)))
        return Page(items, str(start + size) if start + size < self.total else None)


async def test_a_read_stops_at_its_ceiling_of_deals_or_of_seconds() -> None:
    whole = Pages(120)
    got = await read_account(whole, "odoo", DAY, cap=2000, seconds=60, clock=whole.clock)  # type: ignore[arg-type]
    assert got.cut is None and len(got.purchases) == 120 and all(not kw["lines"] for kw in whole.asked)
    capped = Pages(5000)
    got = await read_account(capped, "amocrm", DAY, cap=2000, seconds=60, clock=capped.clock)  # type: ignore[arg-type]
    assert got.cut == "cap" and len(got.purchases) == 2000 and capped.asked[0]["limit"] == 250
    slow = Pages(5000, tick=25)
    got = await read_account(slow, "bitrix24", DAY, cap=2000, seconds=60, clock=slow.clock)  # type: ignore[arg-type]
    assert got.cut == "time" and len(got.purchases) == 150


def test_orders_whose_products_were_not_read_do_not_count_for_a_habit() -> None:
    habit = DealLine("Кабель", Decimal(1), Decimal(100), product_id="7")
    other = DealLine("Розетка", Decimal(1), Decimal(100), product_id="9")
    lined = orders_of([buy(DAY, lines=(habit,)), buy(DAY, lines=(habit,), n=1)])[0].lines
    history = [
        Order(DAY - timedelta(days=200), Decimal(100), ()),  # older than the newest read with products
        Order(DAY - timedelta(days=170), Decimal(100), ()),
        Order(DAY - timedelta(days=140), Decimal(100), (), lined),
        Order(DAY - timedelta(days=110), Decimal(100), (), lined),
        Order(DAY - timedelta(days=80), Decimal(100), (), orders_of([buy(DAY, lines=(other,))])[0].lines),
    ]
    why = reasons(history, verdict(history, DAY))
    assert [(r.kind, r.refs, r.data.get("of")) for r in why][1] == ("gone", (2, 3, 4), 2)
    unread_last = [*history[:4], Order(DAY - timedelta(days=80), Decimal(100), ())]
    assert [r.kind for r in reasons(unread_last, verdict(unread_last, DAY))] == ["cycle"]


class Seeded:
    """An account for the seed: two plan companies, one purchase of the plan already there."""

    kind = "bitrix24"

    def __init__(self) -> None:
        self.made: list[tuple[str, date, int]] = []

    async def find_company(self, company: Any) -> str | None:
        return {"Ромашка": "1", "Navruz Stroy": "2"}.get(company.name)

    async def purchases(self, **kw: Any) -> Page[Purchase]:
        first = plan.TITLE.format(company="Ромашка", day=plan.BUYERS[0].first.isoformat())
        return Page(
            (Purchase("x", first, datetime.now(UTC), None, None, "1"),) if kw["company_id"] == "1" else ()
        )

    async def list_products(self, **kw: Any) -> Page[Any]:
        from alnuzum_crm.types import Product

        skus = {sku for b in plan.BUYERS for sku, _ in b.basket}
        return Page(tuple(Product(f"id-{s}", f"Товар {s}", s, Decimal(10), "UZS") for s in sorted(skus)))

    async def create_won_deal(self, deal: Any, closed_at: datetime) -> Any:
        self.made.append((deal.title, closed_at.date(), len(deal.lines)))
        return None


async def test_the_seed_writes_only_what_the_account_lacks() -> None:
    crm = Seeded()
    made = await plan.seed(crm, "bitrix24", DAY)  # type: ignore[arg-type]
    want = len(plan.plan(plan.BUYERS[0], DAY)) - 1 + len(plan.plan(plan.BUYERS[1], DAY))
    assert made == want == len(crm.made)
    assert all(t.startswith(plan.PREFIX) and n >= 2 for t, _d, n in crm.made)
    assert all(d <= DAY for _t, d, _n in crm.made)
    assert not any(t.endswith(plan.BUYERS[0].first.isoformat()) for t, _d, _n in crm.made)
