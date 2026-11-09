"""Tests of the mail triage: a real Postgres (DATABASE_URL or the local compose database); the CRM and the model
are stand-ins at their own boundaries, so the code that talks to them runs for real."""

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
from alnuzum_mail.model import FIELDS, MailReader
from alnuzum_mail.service import Mail, MailSettings
from alnuzum_mail.web import make_solution
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_crm.errors import CrmRateLimited
from alnuzum_crm.types import (
    AccountInfo,
    CompanyHit,
    CompanyIn,
    ContactIn,
    CreatedDeal,
    CrmKind,
    Deal,
    DealHit,
    DealIn,
    DealStatus,
    Stage,
    TaskIn,
    User,
)
from alnuzum_orders.web import Orders, OrderSettings
from alnuzum_orders.web import make_solution as orders_solution
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings
from alnuzum_platform.migrate import upgrade

DB_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum")
KEYS = "k1:" + base64.b64encode(b"\x03" * 32).decode()
SESSION_SECRET = "session-secret-for-tests-0123456789abcdef"
ORIGIN = "https://apps.test"
API_KEY = "sk-ant-test-key-0123456789"
DEMO_HOOK = "https://acme.bitrix24.ru/rest/1/demohooksecret123456/"

# what the model reads out of each sample letter: value and the fragment it quotes
ROMASHKA = {
    "company": ("ООО «Ромашка»", "ООО «Ромашка»"),
    "inn": ("305123456", "ИНН 305123456"),
    "person": ("Алишер Каримов", "Алишер Каримов"),
    "position": ("Главный инженер", "Главный инженер"),
    "phone": ("+998 90 100 11 01", "моб.: +998 90 100 11 01"),
    "email": ("a.karimov@romashka-demo.uz", "a.karimov@romashka-demo.uz"),
    "website": ("romashka-demo.uz", "romashka-demo.uz"),
}
NAVRUZ = {
    "company": ("ООО «Navruz Stroy»", "ООО «Navruz Stroy»"),
    "inn": ("306234567", "ИНН 306234567"),
    "person": ("Дилшод Расулов", "Дилшод Расулов"),
    "phone": ("+998 71 200 22 02", "+998 71 200 22 02"),
    "email": ("d.rasulov@navruzstroy-demo.uz", "d.rasulov@navruzstroy-demo.uz"),
    "website": ("navruzstroy-demo.uz", "navruzstroy-demo.uz"),
}


def answer(
    cls: str = "new", fields: dict[str, tuple[str, str]] | None = None, **extra: Any
) -> dict[str, Any]:
    """The model's answer by the schema: ``fields`` as (value, quote)."""
    got = fields if fields is not None else ROMASHKA
    return {
        "class": cls,
        "language": extra.pop("language", "ru"),
        "request": extra.pop("request", "Просит КП на щиты учёта для второго цеха."),
        "why": extra.pop("why", "Просит цену на новый объект."),
        "fields": {
            k: {"value": got[k][0], "quote": got[k][1]} if k in got else {"value": None, "quote": None}
            for k in FIELDS
        },
        "scans": extra.pop("scans", []),
    }


class FakeMessages:
    """The model: each call answers with the next item of the script, or with ``answer()``."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        reply = self.script.pop(0) if self.script else answer()
        if callable(reply) and not isinstance(reply, BaseException):
            reply = reply(request)
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1000, output_tokens=300),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(reply, ensure_ascii=False))],
        )


def _deal(id_: str, title: str, company: str, status: DealStatus = DealStatus.OPEN) -> DealHit:
    return DealHit(id_, title, company, None, "C0:NEW", Decimal(0), status)


class FakeCRM:
    """What the solution uses of an adapter: companies with requisites, contacts, deals, records made."""

    def __init__(self) -> None:
        self.kind = CrmKind.BITRIX24
        self.companies = {
            "1": CompanyHit("1", "Ромашка", "305123456", ("+998 71 200 11 01",), ("info@romashka-demo.uz",),
                            ("https://romashka-demo.uz",)),
            "2": CompanyHit("2", "Navruz Stroy", "306234567", ("+998 71 200 22 02",), (), ("navruzstroy-demo.uz",)),
            "3": CompanyHit("3", "Ромашка-2", "399999999", (), (), ()),
        }  # fmt: skip
        self.contacts = {"a.karimov@romashka-demo.uz": "c1", "d.rasulov@navruzstroy-demo.uz": "c2"}
        self.deals = {
            "11": _deal("11", "Демо · Ромашка: электрощитовая", "1"),
            "12": _deal("12", "Демо · Навруз Строй: освещение склада", "2"),
            "13": _deal("13", "Ромашка: чужая сделка", "1"),
            "14": _deal("14", "Демо · Ромашка: старый заказ", "1", DealStatus.WON),
        }
        self.writes: list[tuple[str, Any, Any]] = []
        self.looked: list[dict[str, Any]] = []
        self.fail_on: str | None = None
        self.made = 0

    async def __aenter__(self) -> "FakeCRM":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _maybe_fail(self, step: str) -> None:
        if self.fail_on == step:
            self.fail_on = None
            raise CrmRateLimited("busy", crm="bitrix24")

    async def find_companies(self, **keys: Any) -> list[CompanyHit]:
        self._maybe_fail("look")
        self.looked.append(keys)
        return list(self.companies.values())

    async def find_contact(self, contact: ContactIn) -> str | None:
        return self.contacts.get((contact.email or "").casefold())

    async def deal_history(
        self, *, company_id: str | None = None, contact_id: str | None = None, limit: int = 200
    ) -> list[Deal]:
        found = [d for d in self.deals.values() if company_id and d.company == company_id]
        return [Deal(d.id, d.title, None, "UZS", d.status) for d in found][:limit]

    async def find_deals(self, query: str, *, limit: int = 8) -> list[DealHit]:
        q = query.casefold()
        return [d for d in self.deals.values() if q in d.title.casefold()][:limit]

    async def get_deal(self, deal_id: str) -> Deal:
        hit = self.deals[deal_id]
        return Deal(hit.id, hit.title, None, "UZS", hit.status)

    async def create_company(self, company: CompanyIn) -> str:
        self._maybe_fail("company")
        self.made += 1
        self.writes.append(("company", None, company))
        return f"co{self.made}"

    async def create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> str:
        self._maybe_fail("contact")
        self.made += 1
        self.writes.append(("contact", company_id, contact))
        return f"ct{self.made}"

    async def link_contact(self, contact_id: str, company_id: str) -> bool:
        self.writes.append(("link", contact_id, company_id))
        return True

    async def create_deal(self, deal: DealIn) -> CreatedDeal:
        self._maybe_fail("deal")
        self.made += 1
        self.writes.append(("deal", None, deal))
        return CreatedDeal(f"d{self.made}")

    async def add_note(self, deal_id: str, body: str) -> str:
        self._maybe_fail("note")
        self.writes.append(("note", deal_id, body))
        return "n1"

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        self._maybe_fail("task")
        self.writes.append(("task", deal_id, task))
        return "t1"

    async def stages(self) -> list[Stage]:
        self._maybe_fail("stages")
        return [
            Stage("C0:NEW", "Новая", DealStatus.OPEN, "Продажи"),
            Stage("C0:QUOTE", "КП", DealStatus.OPEN, "Продажи"),
            Stage("C0:WON", "Успех", DealStatus.WON, "Продажи"),
        ]

    async def users(self) -> list[User]:
        return [User("7", "Мария Петрова"), User("8", "Иван Сидоров")]

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        return f"https://acme.bitrix24.ru/crm/deal/details/{deal_id}/"

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the solution called {name} of the CRM")


@pytest.fixture(scope="session")
async def migrated() -> str:
    await asyncio.to_thread(upgrade, DB_URL)
    return DB_URL


TABLES = ("mail_runs", "mail_rules", "mail_spend", "mail_demo_records", "orders_runs", "orders_model_calls",
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
    def __init__(self, app: FastAPI, mail: Mail, crm: FakeCRM, messages: FakeMessages) -> None:
        self.app, self.mail, self.crm, self.messages = app, mail, crm, messages


@pytest.fixture
async def make(engine: AsyncEngine, migrated: str) -> AsyncIterator[Callable[..., Any]]:
    started: list[Any] = []

    async def build(*script: Any, **kw: Any) -> Harness:
        reader = MailReader(API_KEY)
        reader.retry_pause = 0.0
        messages = FakeMessages(list(script))
        reader._client = SimpleNamespace(beta=SimpleNamespace(messages=messages))  # type: ignore[assignment]
        mail = Mail(MailSettings(**({"anthropic_api_key": API_KEY} | kw)), reader=reader)
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
            solutions=[orders_solution(orders), make_solution(mail)],
        )
        crm = FakeCRM()
        app.state.connectors.adapter = lambda info, secrets, store: crm
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        started.append(ctx)
        return Harness(app, mail, crm, messages)

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
        page = await self.http.get(f"/{lang}/mail")
        assert page.status_code == 200, page.text[:300]
        return csrf_of(page.text)

    async def send(
        self,
        *,
        target: str = "demo:bitrix24",
        sample: str | None = "new-signature",
        file: tuple[str, bytes] | None = None,
        text: str | None = None,
        lang: str = "ru",
    ) -> httpx.Response:
        data = {"csrf": await self.csrf(lang), "target": target}
        if sample:
            data["sample"] = sample
        if text is not None:
            data["text"] = text
        files = {
            "file": (file[0], file[1], "message/rfc822") if file else ("", b"", "application/octet-stream")
        }
        return await self.http.post(f"/{lang}/mail", data=data, files=files, headers={"origin": ORIGIN})

    async def act(self, run: str, action: str, lang: str = "ru", **fields: str) -> httpx.Response:
        page = await self.http.get(f"/{lang}/mail/{run}")
        token = csrf_of(page.text)
        return await self.http.post(
            f"/{lang}/mail/{run}",
            data={"csrf": token, "action": action, **fields},
            headers={"origin": ORIGIN},
        )

    async def write(self, run: str, lang: str = "ru", **fields: str) -> str:
        done = await self.act(run, "write", lang, **fields)
        assert done.status_code == 303, done.text[:300]
        return done.headers["location"].split("n=", 1)[1]


def run_id(resp: httpx.Response) -> str:
    assert resp.status_code == 303, (resp.status_code, resp.text[:500])
    return resp.headers["location"].rsplit("/", 1)[-1]
