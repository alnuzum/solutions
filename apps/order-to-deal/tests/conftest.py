"""Tests of the solution: a real Postgres (DATABASE_URL or the local compose database); the CRM and the
model are fakes that answer the way they do (the model through the real ``ClaudeReader``, so every answer
goes through the same parsing and bounds as in production)."""

import asyncio
import base64
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_crm.types import (
    AccountInfo,
    CompanyIn,
    ContactIn,
    CreatedDeal,
    CrmKind,
    Deal,
    DealIn,
    DealLine,
    DealStatus,
    Page,
    Product,
    Ref,
    TaskIn,
)
from alnuzum_orders.catalog import Catalog, CatalogItem
from alnuzum_orders.demo_catalog import DEMO_CATALOG
from alnuzum_orders.model import ClaudeReader
from alnuzum_orders.web import Orders, OrderSettings, make_solution
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings
from alnuzum_platform.migrate import upgrade
from alnuzum_platform.web.security import Sessions

DB_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum")
KEYS = "k1:" + base64.b64encode(b"\x01" * 32).decode()
SESSION_SECRET = "session-secret-for-tests-0123456789abcdef"
ORIGIN = "https://apps.test"
DEMO_HOOK = "https://acme.bitrix24.ru/rest/1/demohooksecret123456/"
DEMO_AMO_TOKEN = "AT-demo-amo-secret"
DEMO_ZOHO_REFRESH = "1000.demo-zoho-refresh.secret"
API_KEY = "sk-ant-test-key-0123456789"


# --- the model ---


Answer = Callable[[dict[str, Any]], Any]


class FakeMessages:
    """``client.beta.messages``: each request gets the next scripted answer (a JSON-able dict, an
    exception, or a callable of the request)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("the model was asked more than the test expected")
        answer = self.script.pop(0)
        if callable(answer) and not isinstance(answer, BaseException):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        stop = "end_turn"
        if isinstance(answer, tuple):
            stop, answer = answer
        body = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            stop_reason=stop,
            content=[SimpleNamespace(type="text", text=body)],
        )


def reader_with(*script: Any) -> tuple[ClaudeReader, FakeMessages]:
    reader = ClaudeReader(API_KEY)
    reader.retry_pause = 0.0
    messages = FakeMessages(list(script))
    reader._client = SimpleNamespace(beta=SimpleNamespace(messages=messages))  # type: ignore[assignment]
    return reader, messages


def document(request: dict[str, Any]) -> list[str]:
    """The numbered lines the reader put into the prompt."""
    body = request["messages"][0]["content"][-1]["text"]
    return re.findall(r"^\d+: (.*)$", body, flags=re.M)


def questions(request: dict[str, Any]) -> list[tuple[int, list[str]]]:
    body = request["messages"][0]["content"][0]["text"]
    out = []
    for block in body.split("\n\n"):
        key = int(re.match(r"Line (\d+):", block).group(1))  # type: ignore[union-attr]
        out.append((key, re.findall(r"^  \d+\. (.*)$", block, flags=re.M)))
    return out


def extract_lines(request: dict[str, Any]) -> dict[str, Any]:
    """Every line «name - N unit» of the document is an item, as a careful reader would return it."""
    items = []
    for n, line in enumerate(document(request), 1):
        m = re.match(r"^(.+?)\s+[-—]\s+(\d+(?:[.,]\d+)?)\s*(\S+)?$", line)
        if m:
            items.append(
                {
                    "lines": [n],
                    "text": line,
                    "name": m.group(1),
                    "quantity": float(m.group(2).replace(",", ".")),
                    "unit": m.group(3),
                    "sku": None,
                }
            )
    return {"items": items, "customer": {}, "transcript": []}


def choose_first(confidence: float = 0.95) -> Answer:
    def answer(request: dict[str, Any]) -> dict[str, Any]:
        return {
            "answers": [{"line": k, "choice": 1, "confidence": confidence} for k, _ in questions(request)]
        }

    return answer


# --- a catalog ---


def item(
    i: int,
    name: str,
    *,
    sku: str | None = None,
    price: str = "100",
    unit: str = "шт",
    category: str | None = "Разное",
    stock: str | None = None,
    synonyms: tuple[str, ...] = (),
    packs: dict[str, Decimal] | None = None,
) -> CatalogItem:
    return CatalogItem(
        str(i),
        name,
        sku,
        Decimal(price),
        "RUB",
        unit,
        category,
        None if stock is None else Decimal(stock),
        synonyms,
        packs or {},
    )


def catalog(*items: CatalogItem) -> Catalog:
    return Catalog(items, "RUB", any(i.stock is not None for i in items))


# --- the database ---


@pytest.fixture(scope="session")
async def migrated() -> str:
    await asyncio.to_thread(upgrade, DB_URL)
    return DB_URL


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated)
    yield eng
    async with eng.begin() as conn:
        for table in ("orders_runs", "orders_rules", "orders_model_calls", "crm_writes", "crm_connections"):
            await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - our own table names
    await eng.dispose()


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


# --- the CRM ---


def demo_products() -> list[Product]:
    out = [
        Product(
            f"p{n}", i.name, i.sku, i.price_rub, "RUB", "шт" if i.unit == "шт" else None, i.category,
            i.description(), Decimal(i.stock),
        )
        for n, i in enumerate(DEMO_CATALOG, 1)
    ]  # fmt: skip
    # a test record of the shared account: never offered by the demo
    out.append(Product("x1", "E2E test product", "E2E-1", Decimal(1), "RUB"))
    return out


class FakeCRM:
    """Stands in for an adapter: a catalog, and everything written, readable back."""

    def __init__(self, kind: CrmKind = CrmKind.BITRIX24) -> None:
        self.kind = kind
        self.products = demo_products()
        self.deals: dict[str, DealIn] = {}
        self.notes: list[tuple[str, str]] = []
        self.tasks: list[tuple[str, TaskIn]] = []
        self.companies: list[CompanyIn] = []
        self.contacts: list[ContactIn] = []
        self.fail: Exception | None = None
        self.fail_list: Exception | None = None
        self.list_calls = 0
        self.gate: asyncio.Event | None = None

    async def __aenter__(self) -> "FakeCRM":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_products(self, *, cursor: str | None = None, limit: int = 200) -> Page[Product]:
        self.list_calls += 1
        if self.fail_list is not None:
            raise self.fail_list
        start = int(cursor or 0)
        chunk = self.products[start : start + limit]
        nxt = str(start + limit) if start + limit < len(self.products) else None
        return Page(tuple(chunk), nxt)

    async def find_or_create_company(self, company: CompanyIn) -> Ref:
        self.companies.append(company)
        return Ref(f"co{len(self.companies)}", True)

    async def find_or_create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> Ref:
        self.contacts.append(contact)
        return Ref(f"ct{len(self.contacts)}", True)

    async def create_deal(self, deal: DealIn) -> CreatedDeal:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        deal_id = str(500 + len(self.deals))
        self.deals[deal_id] = deal
        return CreatedDeal(deal_id)

    async def add_note(self, deal_id: str, body: str) -> str:
        self.notes.append((deal_id, body))
        return f"n{len(self.notes)}"

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        self.tasks.append((deal_id, task))
        return f"t{len(self.tasks)}"

    async def get_deal(self, deal_id: str) -> Deal:
        deal = self.deals[deal_id]
        lines = tuple(DealLine(x.name, x.quantity, x.unit_price, x.product_id, x.sku) for x in deal.lines)
        return Deal(deal_id, deal.title, deal.amount, "RUB", DealStatus.OPEN, datetime.now(UTC), None, lines)

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        return f"https://crm.example/deal/{deal_id}"


# --- the app ---


def settings(url: str, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": url,
        "secret_keys": KEYS,
        "session_secret": SESSION_SECRET,
        "public_url": ORIGIN,
        "app_env": "test",
        "quota_writes_per_day": 20,
        "rate_per_minute": 10_000,
        "rate_post_per_minute": 10_000,
        "sweep_minutes": 0,
    }
    return Settings(**(base | kw))


def order_settings(**kw: Any) -> OrderSettings:
    base: dict[str, Any] = {
        "anthropic_api_key": API_KEY,
        "demo_b24_webhook": DEMO_HOOK,
        "demo_amocrm_domain": "acme.amocrm.ru",
        "demo_amocrm_token": DEMO_AMO_TOKEN,
        "demo_zoho_refresh_token": DEMO_ZOHO_REFRESH,
    }
    return OrderSettings(**(base | kw))


class Harness:
    def __init__(self, app: FastAPI, orders: Orders, crm: FakeCRM, messages: FakeMessages) -> None:
        self.app, self.orders, self.crm, self.messages = app, orders, crm, messages


@pytest.fixture
async def make(engine: AsyncEngine, migrated: str) -> AsyncIterator[Callable[..., Any]]:
    started: list[Any] = []

    async def build(
        *script: Any, platform: dict[str, Any] | None = None, model: bool = True, **kw: Any
    ) -> Harness:
        reader, messages = reader_with(*script)
        orders = Orders(order_settings(**kw), reader if model else None)
        app = create_app(settings(migrated, **(platform or {})), solutions=[make_solution(orders)])
        crm = FakeCRM()
        app.state.connectors.adapter = lambda info, secrets, store: crm
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        started.append(ctx)
        return Harness(app, orders, crm, messages)

    yield build
    for ctx in started:
        await ctx.__aexit__(None, None, None)


class Browser:
    """One browser: its own cookie jar, so two of them are two sessions."""

    def __init__(self, app: FastAPI, ip: str = "203.0.113.7") -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 5555)), base_url=ORIGIN
        )

    async def csrf(self, path: str = "/ru/orders") -> str:
        page = await self.http.get(path)
        found = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert found, f"no csrf on {path}: {page.status_code}"
        return found.group(1)

    def owner(self) -> str:
        sid = Sessions(SESSION_SECRET, secure=True).verify(self.http.cookies["__Host-an_sid"])
        assert sid
        return Sessions.owner(sid)

    async def order(
        self, target: str = "demo:bitrix24", *, sample: str = "", text: str = "", csrf: str | None = None,
        file: tuple[str, bytes] | None = None, lang: str = "ru",
    ) -> httpx.Response:  # fmt: skip
        token = csrf if csrf is not None else await self.csrf(f"/{lang}/orders")
        data = {"csrf": token, "target": target, "sample": sample, "text": text}
        files = {"file": (file[0], file[1], "application/octet-stream")} if file else None
        return await self.http.post(f"/{lang}/orders", data=data, files=files)

    async def decide(self, run: str, form: dict[str, str], lang: str = "ru") -> httpx.Response:
        token = await self.csrf(f"/{lang}/orders/{run}")
        return await self.http.post(f"/{lang}/orders/{run}", data={"csrf": token} | form)


def run_id(resp: httpx.Response) -> str:
    assert resp.status_code == 303, resp.text[:500]
    return resp.headers["location"].rsplit("/", 1)[1]
