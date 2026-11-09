"""Tests of the contract check: a real Postgres (DATABASE_URL or the local compose database); the CRM and the
model are stand-ins at their own boundaries, so the code that talks to them runs for real."""

import asyncio
import base64
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from alnuzum_contracts.model import ContractReader
from alnuzum_contracts.service import Contracts, ContractSettings
from alnuzum_contracts.web import make_solution
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_crm.errors import CrmRateLimited
from alnuzum_crm.types import AccountInfo, CrmKind, Deal, DealHit, DealStatus, TaskIn
from alnuzum_orders.web import Orders, OrderSettings
from alnuzum_orders.web import make_solution as orders_solution
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings
from alnuzum_platform.migrate import upgrade

DB_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum")
KEYS = "k1:" + base64.b64encode(b"\x02" * 32).decode()
SESSION_SECRET = "session-secret-for-tests-0123456789abcdef"
ORIGIN = "https://apps.test"
API_KEY = "sk-ant-test-key-0123456789"
DEMO_HOOK = "https://acme.bitrix24.ru/rest/1/demohooksecret123456/"


class FakeMessages:
    """The model: each call answers with the next item of the script, or with ``rate_all`` for a rating."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        answer = self.script.pop(0) if self.script else rate_all
        if callable(answer) and not isinstance(answer, BaseException):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1000, output_tokens=300),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(answer, ensure_ascii=False))],
        )


def rate_all(request: dict[str, Any], risk: str = "high") -> dict[str, Any]:
    """Every change it was given: payment, ``risk``."""
    prompt = request["messages"][0]["content"][-1]["text"]
    ids = [int(x) for x in re.findall(r'<change id="(\d+)"', prompt)]
    return {
        "changes": [
            {"id": i, "topic": "payment", "risk": risk, "substantive": True, "why": "Оплата позже."}
            for i in ids
        ]
    }


class FakeCRM:
    """An adapter's side that the solution uses: deals to find, notes and tasks written, one failure on demand."""

    def __init__(self) -> None:
        self.kind = CrmKind.BITRIX24
        self.deals = {
            "11": DealHit("11", "Демо · Ромашка: электрощитовая", "Ромашка", "Алишер Каримов", "1:10", Decimal(0), DealStatus.OPEN),
            "12": DealHit("12", "Демо · Навруз Строй: освещение склада", "Navruz Stroy", None, "1:10", Decimal(0), DealStatus.OPEN),
            "13": DealHit("13", "Ромашка: чужая сделка", "Ромашка", None, "1:10", Decimal(0), DealStatus.OPEN),
        }  # fmt: skip
        self.writes: list[tuple[str, str, Any]] = []
        self.fail_on: str | None = None

    async def __aenter__(self) -> "FakeCRM":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def find_deals(self, query: str, *, limit: int = 8) -> list[DealHit]:
        q = query.casefold()
        return [
            d for d in self.deals.values() if q in d.title.casefold() or q in (d.company or "").casefold()
        ][:limit]

    async def get_deal(self, deal_id: str) -> Deal:
        hit = self.deals[deal_id]
        return Deal(hit.id, hit.title, None, "RUB", DealStatus.OPEN)

    async def add_note(self, deal_id: str, body: str) -> str:
        if self.fail_on == "note":
            self.fail_on = None
            raise CrmRateLimited("busy", crm="bitrix24")
        self.writes.append(("note", deal_id, body))
        return "n1"

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        if self.fail_on == "task":
            self.fail_on = None
            raise CrmRateLimited("busy", crm="bitrix24")
        self.writes.append(("task", deal_id, task))
        return "t1"

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        return f"https://acme.bitrix24.ru/crm/deal/details/{deal_id}/"

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the solution called {name} of the CRM")


@pytest.fixture(scope="session")
async def migrated() -> str:
    await asyncio.to_thread(upgrade, DB_URL)
    return DB_URL


TABLES = ("contract_runs", "contract_standards", "contract_spend", "orders_runs", "orders_model_calls",
          "crm_writes", "crm_connections")  # fmt: skip


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


class Harness:
    def __init__(self, app: FastAPI, contracts: Contracts, crm: FakeCRM, messages: FakeMessages) -> None:
        self.app, self.contracts, self.crm, self.messages = app, contracts, crm, messages


@pytest.fixture
async def make(engine: AsyncEngine, migrated: str) -> AsyncIterator[Callable[..., Any]]:
    started: list[Any] = []

    async def build(*script: Any, **kw: Any) -> Harness:
        reader = ContractReader(API_KEY)
        reader.retry_pause = 0.0
        messages = FakeMessages(list(script))
        reader._client = SimpleNamespace(beta=SimpleNamespace(messages=messages))  # type: ignore[assignment]
        contracts = Contracts(ContractSettings(**({"anthropic_api_key": API_KEY} | kw)), reader=reader)
        orders = Orders(OrderSettings(anthropic_api_key=API_KEY, demo_b24_webhook=DEMO_HOOK))
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
            solutions=[orders_solution(orders), make_solution(contracts)],
        )
        crm = FakeCRM()
        app.state.connectors.adapter = lambda info, secrets, store: crm
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        started.append(ctx)
        return Harness(app, contracts, crm, messages)

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

    async def csrf(self, lang: str = "ru") -> str:
        page = await self.http.get(f"/{lang}/contracts")
        assert page.status_code == 200, page.text[:300]
        return csrf_of(page.text)

    async def compare(
        self,
        *,
        target: str = "demo:bitrix24",
        standard: str = "demo:supply-ru",
        sample: str | None = "supply-ru-docx",
        file: tuple[str, bytes] | None = None,
        lang: str = "ru",
        csrf: str | None = None,
    ) -> httpx.Response:
        token = csrf or await self.csrf(lang)
        data = {"csrf": token, "target": target, "standard": standard}
        if sample and not file:
            data["sample"] = sample
        files = (
            {"file": (file[0], file[1], "application/octet-stream")}
            if file
            else {"file": ("", b"", "application/octet-stream")}
        )
        return await self.http.post(f"/{lang}/contracts", data=data, files=files, headers={"origin": ORIGIN})

    async def act(self, run: str, action: str, lang: str = "ru", **fields: str) -> httpx.Response:
        page = await self.http.get(f"/{lang}/contracts/{run}")
        token = csrf_of(page.text)
        return await self.http.post(
            f"/{lang}/contracts/{run}",
            data={"csrf": token, "action": action, **fields},
            headers={"origin": ORIGIN},
        )


def run_id(resp: httpx.Response) -> str:
    assert resp.status_code == 303, (resp.status_code, resp.text[:500])
    return resp.headers["location"].rsplit("/", 1)[-1]
