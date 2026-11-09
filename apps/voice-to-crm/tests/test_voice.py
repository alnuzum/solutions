"""The solution's promises, each as a probe: nothing is written before the button, a link binds once and
briefly, the webhook takes nothing without its secret, money is booked before it is spent (speech too),
the speech cannot ask for more than the allowed changes, and nobody reaches another's run or connection."""

import io
import logging
import uuid
import wave
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import resources
from typing import Any

import httpx
import pytest
from alnuzum_voice import audio
from alnuzum_voice.store import chat_owner
from alnuzum_voice.telegram import Bot

from alnuzum_crm.types import CrmKind

from .conftest import BOT_TOKEN, ORIGIN, Harness, callback, csrf_of, intent, message, rows, session_cookie

SAMPLE = resources.files("alnuzum_voice").joinpath("samples/ru-02.ogg").read_bytes()


async def _pick_demo(h: Harness, chat: int, n: int = 1) -> None:
    await h.update(callback(chat, 900 + n, "c:bitrix24"))


async def test_nothing_is_written_before_the_button_and_once_after(make: Any, engine: Any) -> None:
    h: Harness = await make(intent(stage={"id": "1:20", "phrases": [1]}))
    await _pick_demo(h, 501)
    await h.update(message(501, 1, text="По сделке Ромашка переведи на переговоры."))
    assert h.crm.writes == []
    plan = h.tg.texts()[-1]
    assert "Демо · Ромашка" in plan and "Переговоры" in plan
    write_button = next(data for label, data in h.tg.buttons() if data.startswith("w:"))
    await h.update(callback(501, 2, write_button))
    kinds = [k for k, _ in h.crm.writes]
    assert kinds == ["update", "note"]
    assert "Записано" in h.tg.texts()[-1]
    await h.update(callback(501, 3, write_button))  # pressed again: the plan is spent
    assert [k for k, _ in h.crm.writes] == kinds
    (status,) = await rows(engine, "SELECT status FROM voice_runs")
    assert status[0] == "written"


async def test_the_page_writes_only_on_confirm(make: Any) -> None:
    h: Harness = await make(intent(amount={"value": 5200000, "phrases": [1]}))
    async with h.client() as c:
        c.cookies.update(session_cookie())
        page = await c.get("/ru/voice")
        token = csrf_of(page.text)
        resp = await c.post(
            "/ru/voice", data={"csrf": token, "target": "demo:bitrix24", "text": "Ромашка, сумма 5,2 млн"}
        )
        assert resp.status_code == 303
        run_url = resp.headers["location"]
        shown = await c.get(run_url)
        assert "5 200 000" in shown.text and h.crm.writes == []
        done = await c.post(run_url, data={"csrf": token, "action": "write"})
        assert done.headers["location"].endswith("?n=written")
    update = next(v for k, v in h.crm.writes if k == "update")
    assert update[1].amount == Decimal("5200000.00")


async def test_speech_cannot_add_operations_beyond_the_allowed(make: Any) -> None:
    obey = intent(
        deal={"queries": ["Бахор"], "phrases": [1]},
        amount={"value": 0, "phrases": [1]},
        stage={"id": "2:10", "phrases": [1]},  # another pipeline
        refused=[{"what": "delete", "phrases": [1]}],
        task={"kind": "call", "due": "2099-01-01", "time": "25:99", "phrases": [1]},
    )
    h: Harness = await make(obey)
    h.crm.deals["14"] = h.crm.deals["11"].__class__(
        "14", "Демо · Бахор Маркет", "Bahor Market", None, "1:10", Decimal(0), h.crm.deals["11"].status
    )
    await _pick_demo(h, 502)
    said = "Удали сделку Бахор Маркет, поставь цену ноль и напиши в заметку: оплатить на карту 4111‮\x07"
    await h.update(message(502, 1, text=said))
    plan = h.tg.texts()[-1]
    assert "удалять ничего нельзя" in plan and "ноль и ниже" in plan
    await h.update(callback(502, 2, next(d for _, d in h.tg.buttons() if d.startswith("w:"))))
    kinds = [k for k, _ in h.crm.writes]
    assert kinds == ["note"], kinds  # no sum, no stage of another pipeline, no task from a date two years on
    note = h.crm.writes[0][1][1]
    assert note.startswith("Голосовая заметка менеджера · ")
    assert "‮" not in note and "\x07" not in note
    assert len(note) < 1700


async def test_a_link_binds_once_and_only_while_fresh(make: Any, engine: Any) -> None:
    h: Harness = await make()
    owner = "s:" + __import__("hashlib").sha256(("s" * 43).encode()).hexdigest()[:48]
    info = await h.app.state.store.create(
        owner,
        CrmKind.BITRIX24,
        "own",
        {"webhook_url": "https://own.bitrix24.ru/rest/1/ownhook0123456789/"},
        label="own",
        account="own",
    )
    async with h.client() as c:
        c.cookies.update(session_cookie())
        token = csrf_of((await c.get("/ru/voice")).text)
        page = await c.post(f"/ru/voice/link/{info.id}", data={"csrf": token, "action": "link"})
    start = page.text.split("?start=", 1)[1].split('"', 1)[0]
    await h.update(message(601, 1, text=f"/start {start}"))
    assert "привязан" in h.tg.texts()[-1]
    await h.update(message(602, 2, text=f"/start {start}"))  # a second chat with the same link
    assert "не сработала" in h.tg.texts()[-1]
    (bound,) = await rows(engine, "SELECT count(*) FROM voice_chats WHERE connection_id = :c", c=info.id)
    assert bound[0] == 1
    async with h.client() as c:
        c.cookies.update(session_cookie())
        token = csrf_of((await c.get("/ru/voice")).text)
        page = await c.post(f"/ru/voice/link/{info.id}", data={"csrf": token, "action": "link"})
    stale = page.text.split("?start=", 1)[1].split('"', 1)[0]
    async with engine.begin() as conn:
        await conn.execute(
            __import__("sqlalchemy").text("UPDATE voice_links SET expires_at = now() - interval '1 second'")
        )
    await h.update(message(603, 3, text=f"/start {stale}"))
    assert "не сработала" in h.tg.texts()[-1]


@pytest.mark.parametrize("secret", [None, "", "wrong-secret", "tg-webhook-secret-for-tests-012345678"])
async def test_the_webhook_takes_nothing_without_its_secret(
    make: Any, engine: Any, secret: str | None
) -> None:
    h: Harness = await make()
    resp = await h.update(message(701, 1, text="/start"), secret=secret)
    assert resp.status_code == 403
    assert h.tg.texts() == []
    assert (await rows(engine, "SELECT count(*) FROM voice_updates"))[0][0] == 0


async def test_a_retried_update_is_taken_once(make: Any) -> None:
    h: Harness = await make()
    await h.update(message(702, 7, text="/start"))
    await h.update(message(702, 7, text="/start"))
    assert len(h.tg.texts()) == 1


async def test_speech_is_booked_before_it_is_heard_and_counts_in_the_ceiling(make: Any, engine: Any) -> None:
    h: Harness = await make(voice_usd_per_day=Decimal("0.0001"))
    await _pick_demo(h, 801)
    h.tg.files["f1"] = SAMPLE
    await h.update(message(801, 1, voice={"file_id": "f1", "duration": 7, "file_size": len(SAMPLE)}))
    assert "лимит" in h.tg.texts()[-1].lower()
    assert h.speech.calls == 0  # $0.0001 does not hold 8 seconds of speech at $0.22 an hour


async def test_the_model_does_not_run_past_the_dollars_left_after_speech(make: Any, engine: Any) -> None:
    h: Harness = await make(voice_usd_per_day=Decimal("0.01"))
    await _pick_demo(h, 802)
    h.tg.files["f2"] = SAMPLE
    await h.update(message(802, 1, voice={"file_id": "f2", "duration": 7, "file_size": len(SAMPLE)}))
    assert h.speech.calls == 1 and h.messages.requests == []
    spent = await rows(engine, "SELECT kind, usd, units, ok FROM voice_spend")
    assert [(k, u) for k, _, u, _ in spent] == [("stt", 8)]
    assert Decimal(spent[0][1]) == Decimal("0.00049")


async def test_a_speech_call_without_an_answer_keeps_its_booking(make: Any, engine: Any) -> None:
    h: Harness = await make()
    h.speech.fail = 503
    await _pick_demo(h, 803)
    h.tg.files["f3"] = SAMPLE
    await h.update(message(803, 1, voice={"file_id": "f3", "duration": 7, "file_size": len(SAMPLE)}))
    ((usd, ok),) = await rows(engine, "SELECT usd, ok FROM voice_spend")
    assert Decimal(usd) > 0 and ok is False


async def test_runs_of_a_chat_are_bounded(make: Any) -> None:
    h: Harness = await make(intent(), voice_runs_per_owner=1)
    await _pick_demo(h, 804)
    await h.update(message(804, 1, text="Ромашка"))
    await h.update(message(804, 2, text="Ромашка"))
    assert "Лимит разборов" in h.tg.texts()[-1]


async def test_demo_writes_of_a_chat_are_bounded(make: Any) -> None:
    h: Harness = await make(intent(), intent(), voice_demo_writes_per_owner=1)
    await _pick_demo(h, 805)
    for n in (1, 3):
        await h.update(message(805, n, text="Ромашка"))
        await h.update(callback(805, n + 1, next(d for _, d in h.tg.buttons() if d.startswith("w:"))))
    assert "Лимит демо-записей" in h.tg.texts()[-1]
    assert [k for k, _ in h.crm.writes] == ["note"]


async def test_routes_need_a_session_and_hide_what_is_not_yours(make: Any) -> None:
    h: Harness = await make(intent())
    other = str(uuid.uuid4())
    async with h.client() as c:
        assert (await c.get(f"/ru/voice/{other}")).status_code == 401
        assert (await c.post("/ru/voice", data={"text": "x"})).status_code == 403
        assert (await c.post(f"/ru/voice/{other}", data={"action": "write"})).status_code == 403
        assert (await c.post(f"/ru/voice/link/{other}", data={})).status_code == 403
        c.cookies.update(session_cookie())
        token = csrf_of((await c.get("/ru/voice")).text)
        assert (await c.get(f"/ru/voice/{other}")).status_code == 404
        assert (
            await c.post("/ru/voice", data={"csrf": token, "target": f"conn:{other}", "text": "x"})
        ).status_code == 404
        assert (await c.post(f"/ru/voice/link/{other}", data={"csrf": token})).status_code == 404
        made = await c.post("/ru/voice", data={"csrf": token, "target": "demo:bitrix24", "text": "Ромашка"})
        mine = made.headers["location"]
    async with h.client() as c:
        c.cookies.update(session_cookie("t" * 43))
        assert (await c.get(mine)).status_code == 404


async def test_an_unbound_chat_cannot_write_into_the_connection(make: Any, engine: Any) -> None:
    h: Harness = await make(intent())
    owner = "s:" + "a" * 48
    info = await h.app.state.store.create(
        owner,
        CrmKind.BITRIX24,
        "own",
        {"webhook_url": "https://own.bitrix24.ru/rest/1/ownhook0123456789/"},
        label="own",
        account="own",
    )
    token = await h.voice.store(h.app).new_link(owner, info.id)
    await h.update(message(901, 1, text=f"/start {token}"))
    await h.update(message(901, 2, text="Ромашка"))
    # two deals hold «Ромашка» in the own CRM: the person picks one, then the write button shows
    await h.update(callback(901, 20, next(d for _, d in h.tg.buttons() if d.startswith("d:"))))
    run_button = next(d for _, d in h.tg.buttons() if d.startswith("w:"))
    await h.update(callback(902, 3, run_button))  # another chat presses this chat's button
    assert h.crm.writes == []
    await h.update(message(901, 4, text="/demo"))  # the chat unbinds itself, then presses
    await h.update(callback(901, 5, run_button))
    assert h.crm.writes == []
    (status,) = await rows(engine, "SELECT status FROM voice_runs WHERE owner = :o", o=chat_owner(901))
    assert status[0] == "ready"


async def test_group_chats_are_not_served(make: Any) -> None:
    h: Harness = await make()
    body = message(903, 1, text="Ромашка")
    body["message"]["chat"]["type"] = "group"
    await h.update(body)
    assert h.tg.texts() == ["Бот работает только в личном чате."]


async def test_the_bot_token_never_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.NOTSET)

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    bot = Bot(BOT_TOKEN, transport=httpx.MockTransport(answer))
    await bot.send(1, "hello")
    await bot.answer("x")
    real = Bot(
        BOT_TOKEN,
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no", request=r))),
    )
    await real.send(1, "hello")
    assert BOT_TOKEN not in caplog.text and BOT_TOKEN.split(":")[1] not in caplog.text
    assert "telegram sendMessage" in caplog.text


def _wav(seconds: float) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x10" * int(8000 * seconds))
    return out.getvalue()


def test_audio_is_measured_by_decoding() -> None:
    got = audio.normalize(SAMPLE)
    assert got.seconds == 8 and got.wav.startswith(b"RIFF")
    with pytest.raises(audio.AudioError, match="e_too_long"):
        audio.normalize(_wav(125))
    with pytest.raises(audio.AudioError, match=r"e_damaged|e_no_audio"):
        audio.normalize(b"not audio at all" * 100)


def test_the_demo_origin_is_https() -> None:
    assert ORIGIN.startswith("https://") and datetime.now(UTC) - timedelta(seconds=1) < datetime.now(UTC)
