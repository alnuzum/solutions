"""Tests of the voice solution: a real Postgres (DATABASE_URL or the local compose database); the CRM, the
model, the speech service and Telegram are stand-ins at their own boundaries (httpx transports, the model
client), so the code that talks to them runs for real."""

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
from alnuzum_voice.model import Scribe, VoiceReader
from alnuzum_voice.service import Voice, VoiceSettings
from alnuzum_voice.telegram import Bot
from alnuzum_voice.web import make_solution
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_crm.types import (
    AccountInfo,
    CrmKind,
    Deal,
    DealHit,
    DealLine,
    DealStatus,
    DealUpdate,
    Page,
    Product,
    Stage,
    TaskIn,
)
from alnuzum_orders.demo_catalog import DEMO_CATALOG
from alnuzum_orders.web import Orders, OrderSettings
from alnuzum_orders.web import make_solution as orders_solution
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings
from alnuzum_platform.migrate import upgrade
from alnuzum_platform.web.security import Sessions

DB_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum")
KEYS = "k1:" + base64.b64encode(b"\x01" * 32).decode()
SESSION_SECRET = "session-secret-for-tests-0123456789abcdef"
ORIGIN = "https://apps.test"
API_KEY = "sk-ant-test-key-0123456789"
STT_KEY = "sk_eleven_test_key_0123456789"
BOT_TOKEN = "123456789:AAtestTOKENtestTOKENtestTOKENtestTOK"
TG_SECRET = "tg-webhook-secret-for-tests-0123456789"
DEMO_HOOK = "https://acme.bitrix24.ru/rest/1/demohooksecret123456/"

STAGES = [
    Stage("1:10", "Первичный контакт", DealStatus.OPEN, "Воронка"),
    Stage("1:20", "Переговоры", DealStatus.OPEN, "Воронка"),
    Stage("1:142", "Успешно реализовано", DealStatus.WON, "Воронка"),
    Stage("1:143", "Закрыто и не реализовано", DealStatus.LOST, "Воронка"),
    Stage("2:10", "Другая воронка", DealStatus.OPEN, "Сервис"),
]


class FakeMessages:
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
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(answer, ensure_ascii=False))],
        )


def intent(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "deal": {"queries": ["Ромашка"], "phrases": [1]},
        "stage": None,
        "amount": None,
        "close_date": None,
        "products": [],
        "task": None,
        "refused": [],
    }
    return base | kw


class FakeCRM:
    """An adapter's voice side: deals to find, everything written, readable back."""

    capabilities = frozenset({"close_date", "line_prices"})

    def __init__(self, kind: CrmKind = CrmKind.BITRIX24) -> None:
        self.kind = kind
        self.products = [
            Product(f"p{n}", i.name, i.sku, i.price_rub, "RUB", i.unit, i.category, i.description(), None)
            for n, i in enumerate(DEMO_CATALOG, 1)
        ]
        self.deals = {
            "11": DealHit("11", "Демо · Ромашка: электрощитовая", "Ромашка", "Алишер Каримов", "1:10", Decimal(0), DealStatus.OPEN),
            "12": DealHit("12", "Демо · Навруз Строй: освещение склада", "Navruz Stroy", "Дилшод Расулов", "1:10", Decimal(0), DealStatus.OPEN),
            "13": DealHit("13", "Ромашка: чужая сделка", "Ромашка", None, "1:10", Decimal(0), DealStatus.OPEN),
        }  # fmt: skip
        self.writes: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "FakeCRM":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_products(self, *, cursor: str | None = None, limit: int = 200) -> Page[Product]:
        start = int(cursor or 0)
        nxt = str(start + limit) if start + limit < len(self.products) else None
        return Page(tuple(self.products[start : start + limit]), nxt)

    async def stages(self) -> list[Stage]:
        return STAGES

    async def find_deals(self, query: str, *, limit: int = 8) -> list[DealHit]:
        q = query.casefold()
        return [
            d for d in self.deals.values() if q in d.title.casefold() or q in (d.company or "").casefold()
        ]

    async def update_deal(self, deal_id: str, update: DealUpdate) -> None:
        self.writes.append(("update", (deal_id, update)))

    async def add_lines(self, deal_id: str, lines: tuple[DealLine, ...]) -> None:
        self.writes.append(("lines", (deal_id, lines)))

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        self.writes.append(("task", (deal_id, task)))
        return "t1"

    async def add_note(self, deal_id: str, body: str) -> str:
        self.writes.append(("note", (deal_id, body)))
        return "n1"

    async def get_deal(self, deal_id: str) -> Deal:
        hit = self.deals[deal_id]
        return Deal(
            deal_id, hit.title, Decimal(100), "RUB", DealStatus.OPEN, datetime.now(UTC), None, (), "1:20"
        )

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        return f"https://crm.example/deal/{deal_id}"


class FakeTelegram:
    """api.telegram.org as an httpx transport: what the bot sent, and files to download."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, bytes] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(f"/file/bot{BOT_TOKEN}/"):
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[1]])
        method = path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        self.sent.append((method, body))
        if method == "getFile":
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": f"voice/{body['file_id']}", "file_size": 100}}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})

    def texts(self) -> list[str]:
        return [b.get("text", "") for m, b in self.sent if m in ("sendMessage", "editMessageText")]

    def buttons(self) -> list[tuple[str, str]]:
        for m, b in reversed(self.sent):
            if m in ("sendMessage", "editMessageText") and b.get("reply_markup", {}).get("inline_keyboard"):
                return [
                    (x["text"], x["callback_data"])
                    for row in b["reply_markup"]["inline_keyboard"]
                    for x in row
                ]
        return []


class FakeSpeech:
    """ElevenLabs as an httpx transport."""

    def __init__(self, text: str = "По сделке Ромашка переведи на переговоры.") -> None:
        self.text = text
        self.calls = 0
        self.fail: int | None = None
        self.body: Any = {"detail": "busy"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail:
            return httpx.Response(self.fail, json=self.body)
        return httpx.Response(200, json={"text": self.text, "language_code": "rus"})


@pytest.fixture(scope="session")
async def migrated() -> str:
    await asyncio.to_thread(upgrade, DB_URL)
    return DB_URL


TABLES = ("voice_runs", "voice_spend", "voice_links", "voice_chats", "voice_updates", "orders_runs",
          "orders_model_calls", "crm_writes", "crm_connections")  # fmt: skip


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
    def __init__(
        self,
        app: FastAPI,
        voice: Voice,
        crm: FakeCRM,
        messages: FakeMessages,
        tg: FakeTelegram,
        speech: FakeSpeech,
    ) -> None:
        self.app, self.voice, self.crm, self.messages, self.tg, self.speech = (
            app,
            voice,
            crm,
            messages,
            tg,
            speech,
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)

    async def update(self, body: dict[str, Any], secret: str | None = TG_SECRET) -> httpx.Response:
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
        async with self.client() as c:
            resp = await c.post("/hooks/voice/telegram", json=body, headers=headers)
        await asyncio.gather(*list(self.voice.tasks))
        return resp


def voice_settings(**kw: Any) -> VoiceSettings:
    base: dict[str, Any] = {
        "anthropic_api_key": API_KEY,
        "elevenlabs_api_key": STT_KEY,
        "voice_tg_token": BOT_TOKEN,
        "voice_tg_secret": TG_SECRET,
        "voice_tg_bot": "alnuzum_voice_test_bot",
    }
    return VoiceSettings(**(base | kw))


@pytest.fixture
async def make(engine: AsyncEngine, migrated: str) -> AsyncIterator[Callable[..., Any]]:
    started: list[Any] = []

    async def build(*script: Any, orders_kw: dict[str, Any] | None = None, **kw: Any) -> Harness:
        reader = VoiceReader(API_KEY)
        reader.retry_pause = 0.0
        messages = FakeMessages(list(script))
        reader._client = SimpleNamespace(beta=SimpleNamespace(messages=messages))  # type: ignore[assignment]
        tg, speech = FakeTelegram(), FakeSpeech()
        voice = Voice(
            voice_settings(**kw),
            reader=reader,
            scribe=Scribe(STT_KEY, transport=httpx.MockTransport(speech)),
            bot=Bot(BOT_TOKEN, transport=httpx.MockTransport(tg)),
        )
        orders = Orders(
            OrderSettings(anthropic_api_key=API_KEY, demo_b24_webhook=DEMO_HOOK, **(orders_kw or {}))
        )
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
            solutions=[orders_solution(orders), make_solution(voice)],
        )
        crm = FakeCRM()
        app.state.connectors.adapter = lambda info, secrets, store: crm
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        started.append(ctx)
        return Harness(app, voice, crm, messages, tg, speech)

    yield build
    for ctx in started:
        await ctx.__aexit__(None, None, None)


def session_cookie(sid: str = "s" * 43) -> dict[str, str]:
    return {"__Host-an_sid": Sessions(SESSION_SECRET, secure=True).sign(sid)}


def csrf_of(html: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found, "no CSRF token on the page"
    return found.group(1)


def message(chat_id: int, update_id: int, **kw: Any) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "language_code": "ru"}, **kw},
    }  # fmt: skip


def callback(chat_id: int, update_id: int, data: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {"id": f"cb{update_id}", "data": data, "from": {"id": chat_id, "language_code": "ru"},
                           "message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"}}},
    }  # fmt: skip
