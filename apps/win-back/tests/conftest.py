"""Tests of the win-back pages: a real Postgres (DATABASE_URL or the local compose database); the CRMs are
stand-ins at the adapter's boundary holding purchases, names and tasks, so the code that talks to them runs for
real. The demo CRM holds the history of the demo plan, a pool company of the live tests and a stranger."""

import asyncio
import base64
import os
import re
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_crm.errors import CrmRateLimited
from alnuzum_crm.types import ClientTask, CrmKind, DealLine, Page, Purchase, TaskIn
from alnuzum_orders.web import Orders, OrderSettings
from alnuzum_orders.web import make_solution as orders_solution
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings
from alnuzum_platform.migrate import upgrade
from alnuzum_winback import demo as plan
from alnuzum_winback.service import Winback, WinbackSettings
from alnuzum_winback.web import make_solution

DB_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum")
KEYS = "k1:" + base64.b64encode(b"\x03" * 32).decode()
SESSION_SECRET = "session-secret-for-tests-0123456789abcdef"
ORIGIN = "https://apps.test"
DEMO_HOOK = "https://acme.bitrix24.ru/rest/1/demohooksecret123456/"
TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)  # a Tuesday, 14:00 in Tashkent
EVIL = '<script>alert(1)</script>" onmouseover="x'


def at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=7)


def demo_history() -> tuple[list[Purchase], dict[str, str]]:
    """The demo plan as the account holds it (company ids 1–5), two pool deals of the live tests on company 9
    and a stranger's history on company 8 that missed its cycle but is not «Демо»."""
    out: list[Purchase] = []
    names: dict[str, str] = {}
    n = 0
    for i, buyer in enumerate(plan.BUYERS, 1):
        names[str(i)] = buyer.company
        for day, basket in plan.plan(buyer, TODAY):
            n += 1
            lines = tuple(
                DealLine(f"Товар {sku}", Decimal(q), Decimal(1000), product_id=sku) for sku, q in basket
            )
            amount = sum((x.total for x in lines), Decimal(0))
            title = plan.TITLE.format(company=buyer.company, day=day.isoformat())
            out.append(Purchase(str(n), title, at(day), amount, "UZS", str(i), None, lines))
    names["9"], names["8"] = "E2E Co pool", "Чужая компания"
    for k, days in enumerate((400, 300, 200, 100)):
        out.append(
            Purchase(f"p{k}", "E2E purchase pool 1", at(TODAY - timedelta(days=days)), Decimal(5), "UZS", "9")
        )
        out.append(Purchase(f"s{k}", "Покупка", at(TODAY - timedelta(days=days)), Decimal(7), "UZS", "8"))
    return out, names


def own_history() -> tuple[list[Purchase], dict[str, str]]:
    """An own CRM: a client named with markup that missed its cycle; its habit product, named with markup too,
    is gone from the last order; a contact without a company that keeps buying."""
    out: list[Purchase] = []
    for k, days in enumerate((300, 270, 240, 210, 180)):
        habit = DealLine('<b>Кабель</b> "ВВГ"', Decimal(10), Decimal(50), product_id="77")
        other = DealLine("Розетка", Decimal(2), Decimal(30), product_id="78")
        lines = (other,) if k == 4 else (habit, other)
        amount = sum((x.total for x in lines), Decimal(0))
        out.append(
            Purchase(
                f"o{k}", f"Заказ {k}", at(TODAY - timedelta(days=days)), amount, "RUB", "500", None, lines
            )
        )
    for k, days in enumerate((60, 40, 20)):
        out.append(
            Purchase(f"c{k}", "Контакт", at(TODAY - timedelta(days=days)), Decimal(10), "RUB", None, "600")
        )
    return out, {"500": EVIL, "c600": "Алишер Каримов"}


class FakeCRM:
    """The side of an adapter the solution uses."""

    def __init__(
        self, purchases: list[Purchase], names: dict[str, str], kind: CrmKind = CrmKind.BITRIX24
    ) -> None:
        self.kind = kind
        self.items = purchases
        self.names = names
        self.tasks: dict[str, tuple[str, str, TaskIn, bool]] = {}  # id -> (kind, owner id, task, open)
        self.calls: list[str] = []
        self.fail_on: str | None = None
        self.seq = 0

    async def __aenter__(self) -> "FakeCRM":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _fail(self, step: str) -> None:
        if self.fail_on == step:
            self.fail_on = None
            raise CrmRateLimited("busy", crm=self.kind.value)

    async def purchases(
        self,
        *,
        company_id: str | None = None,
        contact_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        lines: bool = True,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Purchase]:
        self.calls.append(f"purchases:{company_id or contact_id or 'all'}:{lines}")
        self._fail("purchases")
        got = [
            p
            for p in self.items
            if (since is None or p.closed_at >= since) and (until is None or p.closed_at < until)
        ]
        if not lines:
            got = [
                Purchase(p.deal_id, p.title, p.closed_at, p.amount, p.currency, p.company_id, p.contact_id)
                for p in got
            ]
        if company_id or contact_id:
            mine = [
                p
                for p in got
                if (company_id and p.company_id == company_id) or (contact_id and p.contact_id == contact_id)
            ]
            return Page(tuple(sorted(mine, key=lambda p: p.closed_at, reverse=True)[:limit]))
        start = int(cursor or 0)
        page = got[start : start + limit]
        return Page(tuple(page), str(start + limit) if start + limit < len(got) else None)

    async def recent_purchases(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Purchase]:
        return await self.purchases(since=since, until=until, lines=False, cursor=cursor, limit=limit)

    async def client_names(self, ids: Iterable[str], *, contacts: bool = False) -> dict[str, str]:
        self.calls.append("names")
        key = "c" if contacts else ""
        return {i: self.names[key + i] for i in ids if key + i in self.names}

    async def client_tasks(
        self, *, company_id: str | None = None, contact_id: str | None = None
    ) -> list[ClientTask]:
        self.calls.append("client_tasks")
        owner = company_id or contact_id
        return [
            ClientTask(i, t.text, t.due)
            for i, (_k, o, t, is_open) in self.tasks.items()
            if o == owner and is_open
        ]

    async def add_client_task(
        self, task: TaskIn, *, company_id: str | None = None, contact_id: str | None = None
    ) -> str:
        self.calls.append("add_client_task")
        self._fail("task")
        self.seq += 1
        self.tasks[str(self.seq)] = (
            "company" if company_id else "contact",
            str(company_id or contact_id),
            task,
            True,
        )
        return str(self.seq)

    async def discard_task(self, task_id: str) -> None:
        self.calls.append(f"discard_task:{task_id}")
        if task_id in self.tasks:
            k, o, t, _ = self.tasks[task_id]
            self.tasks[task_id] = (k, o, t, False)

    def open_tasks(self, owner: str) -> list[TaskIn]:
        return [t for _k, o, t, is_open in self.tasks.values() if o == owner and is_open]

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the solution called {name} of the CRM")


@pytest.fixture(scope="session")
async def migrated() -> str:
    await asyncio.to_thread(upgrade, DB_URL)
    return DB_URL


TABLES = (
    "winback_scans",
    "winback_tasks",
    "orders_runs",
    "orders_model_calls",
    "crm_writes",
    "crm_connections",
)


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated)
    async with eng.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - our own table names
    yield eng
    await eng.dispose()


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.now = NOW

    def __call__(self) -> float:
        return self.t

    def later(self, minutes: float) -> None:
        self.t += minutes * 60
        self.now += timedelta(minutes=minutes)


class Harness:
    def __init__(self, app: FastAPI, winback: Winback, demo: FakeCRM, own: FakeCRM, clock: Clock) -> None:
        self.app, self.winback, self.demo, self.own, self.clock = app, winback, demo, own, clock


@pytest.fixture
async def make(engine: AsyncEngine, migrated: str) -> AsyncIterator[Callable[..., Any]]:
    started: list[Any] = []

    async def build(**kw: Any) -> Harness:
        clock = Clock()
        winback = Winback(WinbackSettings(**kw), clock=clock, now=lambda: clock.now)
        orders = Orders(OrderSettings(anthropic_api_key="sk-ant-test-0123456789", demo_b24_webhook=DEMO_HOOK))
        app = create_app(
            Settings(
                database_url=migrated,
                secret_keys=KEYS,
                session_secret=SESSION_SECRET,
                public_url=ORIGIN,
                app_env="test",
                rate_per_minute=10_000,
                rate_post_per_minute=10_000,
                sweep_minutes=0,
            ),
            solutions=[orders_solution(orders), make_solution(winback)],
        )
        demo = FakeCRM(*demo_history())
        own = FakeCRM(*own_history())
        app.state.connectors.adapter = lambda info, secrets, store: demo if info.owner == "demo" else own
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        started.append(ctx)
        return Harness(app, winback, demo, own, clock)

    yield build
    for ctx in started:
        await ctx.__aexit__(None, None, None)


def csrf_of(html: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found, "no CSRF token on the page"
    return found.group(1)


class Browser:
    """One visitor: its own cookie jar, its own address."""

    def __init__(self, app: FastAPI, ip: str = "203.0.113.7") -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url=ORIGIN, follow_redirects=False
        )

    async def scan(self, target: str = "demo:bitrix24", lang: str = "ru") -> httpx.Response:
        page = await self.http.get(f"/{lang}/winback")
        assert page.status_code == 200, page.text[:300]
        return await self.http.post(
            f"/{lang}/winback",
            data={"csrf": csrf_of(page.text), "target": target},
            headers={"origin": ORIGIN},
        )

    async def scan_id(self, target: str = "demo:bitrix24") -> str:
        resp = await self.scan(target)
        assert resp.status_code == 303, (resp.status_code, resp.text[:500])
        return resp.headers["location"].rsplit("/", 1)[-1]

    async def task(self, scan: str, n: int, text: str | None = None, lang: str = "ru") -> httpx.Response:
        """The task form of the screen; where a task stands and there is no form, the token of the start page."""
        page = await self.http.get(f"/{lang}/winback/{scan}/{n}")
        assert page.status_code == 200, page.text[:500]
        body = re.search(r'<textarea name="text"[^>]*>([^<]*)</textarea>', page.text)
        token = csrf_of(
            page.text if 'name="csrf"' in page.text else (await self.http.get(f"/{lang}/winback")).text
        )
        data = {
            "csrf": token,
            "action": "task",
            "text": text if text is not None else (body.group(1) if body else "x"),
        }
        return await self.http.post(f"/{lang}/winback/{scan}/{n}", data=data, headers={"origin": ORIGIN})


def notice(resp: httpx.Response) -> str:
    assert resp.status_code == 303, (resp.status_code, resp.text[:500])
    return resp.headers["location"].split("n=", 1)[1] if "n=" in resp.headers["location"] else ""
