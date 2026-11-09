"""The solution's promises, each as a probe: nothing is written before the button, a link binds once and
briefly, the webhook takes nothing without its secret, money is booked before it is spent (speech too),
the speech cannot ask for more than the allowed changes, and nobody reaches another's run or connection."""

import asyncio
import io
import logging
import math
import socket
import subprocess  # nosec B404: probes of our own decoder
import sys
import threading
import time
import uuid
import wave
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib import resources
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from alnuzum_voice import audio
from alnuzum_voice.store import chat_owner
from alnuzum_voice.telegram import Bot
from sqlalchemy import text

from alnuzum_crm.types import CrmKind

from .conftest import (
    BOT_TOKEN,
    ORIGIN,
    TG_SECRET,
    Harness,
    callback,
    csrf_of,
    intent,
    message,
    rows,
    session_cookie,
)

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


async def test_two_presses_at_once_write_once(make: Any) -> None:
    h: Harness = await make(intent(stage={"id": "1:20", "phrases": [1]}))
    await _pick_demo(h, 521)
    await h.update(message(521, 1, text="По сделке Ромашка переведи на переговоры."))
    button = next(data for label, data in h.tg.buttons() if data.startswith("w:"))
    await asyncio.gather(*(h.update(callback(521, n, button)) for n in (2, 3, 4)))
    assert [k for k, _ in h.crm.writes] == ["update", "note"]


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


async def test_speech_has_its_own_minutes_a_day_and_says_so(make: Any, engine: Any) -> None:
    h: Harness = await make(intent(stage={"id": "1:20", "phrases": [1]}), voice_stt_minutes_per_day=1)
    await _pick_demo(h, 811)
    h.tg.files["m1"] = SAMPLE
    h.speech.fail = 503  # heard without an answer: its 8 seconds stay booked
    await h.update(message(811, 1, voice={"file_id": "m1", "duration": 7, "file_size": len(SAMPLE)}))
    assert h.speech.calls == 1
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO voice_spend (id, owner, kind, usd, units) VALUES (:i, 'x', 'stt', 0, 45)"),
            {"i": uuid.uuid4()},
        )
    h.speech.fail = None
    await h.update(message(811, 2, voice={"file_id": "m1", "duration": 7, "file_size": len(SAMPLE)}))
    assert h.speech.calls == 1  # 8 + 45 + 8 > 60: not sent to the service at all
    assert "На сегодня лимит распознавания исчерпан" in h.tg.texts()[-1]
    await h.update(message(811, 3, text="По сделке Ромашка переведи на переговоры."))
    assert "Переговоры" in h.tg.texts()[-1]  # a typed command needs no speech


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
    with pytest.raises(audio.AudioError, match="e_format"):
        audio.normalize(b"not audio at all" * 100)


def _listener() -> tuple[socket.socket, list[int]]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    srv.settimeout(1.5)
    hits: list[int] = []

    def accept() -> None:
        try:
            conn, _ = srv.accept()
            hits.append(1)
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    return srv, hits


def _playlists(port: int) -> list[bytes]:
    return [
        f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1:{port}/x.ts\n#EXT-X-ENDLIST\n".encode(),
        b"ffconcat version 1.0\nfile 'file:///etc/passwd'\n",
        f"[playlist]\nFile1=http://127.0.0.1:{port}/a.mp3\n".encode(),
    ]


def test_a_playlist_or_concat_never_reaches_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    srv, hits = _listener()
    for data in _playlists(srv.getsockname()[1]):
        assert audio.sniff(data[:16]) is None
        with pytest.raises(audio.AudioError, match="e_format"):
            audio.decode(data)  # in process: refused before FFmpeg is even imported
        child = subprocess.run(  # noqa: S603  # nosec B603 - our own module
            [*audio.COMMAND], input=data, capture_output=True, timeout=15, check=False
        )
        assert child.returncode == 6 and child.stdout == b""
    monkeypatch.setattr(audio.subprocess, "run", _no_child)
    for data in _playlists(1):
        with pytest.raises(audio.AudioError, match="e_format"):
            audio.normalize(data)
    time.sleep(0.3)
    srv.close()
    assert hits == []


def _no_child(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("a refused format must not start the decoder")


def test_a_nested_open_is_refused_even_when_the_demuxer_asks() -> None:
    srv, hits = _listener()
    playlist = _playlists(srv.getsockname()[1])[0]
    with pytest.raises(PermissionError):
        audio._open(playlist, "hls")  # the demuxer we never name, forced: its open is still refused
    time.sleep(0.3)
    srv.close()
    assert hits == []


def test_the_decoder_child_cannot_open_a_file_or_a_socket() -> None:
    probe = (
        "import socket, av\n"
        "from alnuzum_voice import audio\n"
        "audio._load()\n"
        "audio._seal()\n"
        "out = []\n"
        "for attempt in (lambda: open('/etc/hosts', 'rb'), lambda: socket.socket()):\n"
        "    try:\n"
        "        attempt(); out.append('opened')\n"
        "    except OSError:\n"
        "        out.append('refused')\n"
        "print(','.join(out))\n"
    )
    child = subprocess.run(  # noqa: S603  # nosec B603 - fixed probe
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=15, check=False
    )
    assert child.stdout.strip() == "refused,refused", child.stderr[-300:]


def _encoded(form: str, codec: str, seconds: float = 2.0) -> bytes:
    import av  # type: ignore[import-not-found,unused-ignore]

    out = io.BytesIO()
    with av.open(out, "w", format=form) as box:
        stream = box.add_stream(codec, rate=48000 if codec == "libopus" else 44100)
        stream.layout = "mono"
        rate = stream.rate
        count = int(rate * seconds)
        tone = b"".join(
            int(8000 * math.sin(2 * math.pi * 440 * i / rate)).to_bytes(2, "little", signed=True)
            for i in range(count)
        )
        frame = av.AudioFrame(format="s16", layout="mono", samples=count)
        frame.planes[0].update(tone)
        frame.rate = rate
        for packet in stream.encode(frame):
            box.mux(packet)
        for packet in stream.encode(None):
            box.mux(packet)
    return out.getvalue()


@pytest.mark.parametrize(
    ("form", "codec", "head"),
    [("mp3", "libmp3lame", "mp3"), ("ipod", "aac", "mp4"), ("webm", "libopus", "matroska")],
)
def test_mp3_m4a_and_webm_decode(form: str, codec: str, head: str) -> None:
    data = _encoded(form, codec)
    assert audio.sniff(data[:16]) == head
    got = audio.normalize(data)
    assert got.seconds in (2, 3) and got.wav.startswith(b"RIFF")


def test_the_demo_origin_is_https() -> None:
    assert ORIGIN.startswith("https://") and datetime.now(UTC) - timedelta(seconds=1) < datetime.now(UTC)


def test_the_decoder_itself_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    wav, seconds = audio.decode(SAMPLE)
    assert wav.startswith(b"RIFF") and 7 < seconds <= 8
    with pytest.raises(audio.AudioError, match="e_too_long"):
        audio.decode(_wav(125))
    with pytest.raises(audio.AudioError, match="e_no_audio"):
        audio.decode(_wav(0.1))
    sealed: list[int] = []
    monkeypatch.setattr(audio, "_seal", lambda: sealed.append(1))  # the test process keeps its descriptors
    monkeypatch.setattr(audio.resource, "setrlimit", lambda *_: None)

    def child(data: bytes) -> tuple[int, bytes]:
        out = io.BytesIO()
        monkeypatch.setattr(audio.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
        monkeypatch.setattr(audio.sys, "stdout", SimpleNamespace(buffer=out))
        return audio.main(["audio", str(audio.MEMORY)]), out.getvalue()

    code, out = child(SAMPLE)
    assert code == 0 and out.startswith(b"RIFF") and sealed == [1]
    assert child(_wav(125))[0] == 3
    assert child(_wav(0.1))[0] == 4
    assert child(b"OggS" + b"\x00" * 400)[0] == 5  # a signature of Ogg, not an Ogg file
    assert child(b"#EXTM3U\nhttp://127.0.0.1:9/x.ts\n")[0] == 6
    assert child(b"OggS" + b"\x00" * (audio.MAX_BYTES + 1))[0] == 5
    assert sealed == [1, 1, 1, 1]  # sealed before every decode, never before a refusal


async def test_the_page_takes_an_audio_file_and_refuses_a_playlist(make: Any) -> None:
    h: Harness = await make(intent(stage={"id": "1:20", "phrases": [1]}))
    async with h.client() as c:
        token = csrf_of((await c.get("/ru/voice")).text)
        sent = await c.post(
            "/ru/voice",
            data={"csrf": token, "target": "demo:bitrix24"},
            files={"audio": ("note.ogg", SAMPLE, "audio/ogg")},
        )
        assert sent.status_code == 303 and h.speech.calls == 1
        shown = await c.get(sent.headers["location"])
        assert "Переговоры" in shown.text and h.crm.writes == []
        playlist = await c.post(
            "/ru/voice",
            data={"csrf": token, "target": "demo:bitrix24"},
            files={"audio": ("x.m3u8", b"#EXTM3U\nhttp://127.0.0.1:9/x.ts\n", "audio/x-mpegurl")},
        )
        assert playlist.status_code == 422 and "Формат не поддерживается" in playlist.text
        big = await c.post(
            "/ru/voice",
            data={"csrf": token, "target": "demo:bitrix24"},
            files={"audio": ("big.ogg", b"OggS" + b"\x00" * audio.MAX_BYTES, "audio/ogg")},
        )
        assert big.status_code == 413
        empty = await c.post("/ru/voice", data={"csrf": token, "target": "demo:bitrix24"})
        assert empty.status_code == 400
    assert h.speech.calls == 1  # neither the playlist nor the big file reached the speech service


async def test_a_crm_that_fails_while_reading_or_writing_writes_nothing_and_says_so(make: Any) -> None:
    from alnuzum_crm.errors import CrmUnavailable

    h: Harness = await make(
        intent(stage={"id": "1:20", "phrases": [1]}), intent(stage={"id": "1:20", "phrases": [1]})
    )
    calls = {"n": 0}
    found = h.crm.find_deals

    async def flaky(query: str, *, limit: int = 8) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise CrmUnavailable("down", crm="bitrix24")
        return await found(query, limit=limit)

    async def down(*_a: Any, **_k: Any) -> None:
        raise CrmUnavailable("down", crm="bitrix24")

    h.crm.find_deals = flaky  # type: ignore[method-assign]
    async with h.client() as c:
        token = csrf_of((await c.get("/ru/voice")).text)
        form = {"csrf": token, "target": "demo:bitrix24", "text": "По сделке Ромашка переведи на переговоры."}
        first = await c.post("/ru/voice", data=form)
        assert first.status_code == 422 and "CRM не ответила" in first.text
        sent = await c.post("/ru/voice", data=form)
        assert sent.status_code == 303
        h.crm.update_deal = down  # type: ignore[method-assign]
        done = await c.post(sent.headers["location"], data={"csrf": token, "action": "write"})
        assert done.headers["location"].endswith("?n=failed")
        again = await c.post(sent.headers["location"], data={"csrf": token, "action": "write"})
        assert again.headers["location"].endswith("?n=failed")  # the retry meets the same error
    assert [k for k, _ in h.crm.writes] == []


async def test_the_bot_names_the_demo_deals_each_crm_has(make: Any) -> None:
    from alnuzum_voice.i18n import demo_deals

    h: Harness = await make()
    await h.update(callback(532, 2, "c:bitrix24"))
    assert "Ромашка, Навруз Строй, Самарканд Текстиль, Бахор Маркет, Ташкент Сити Молл." in h.tg.texts()[-1]
    assert demo_deals("ru", "kommo") == "Ромашка, Навруз Строй"  # Kommo holds two demo deals
    assert demo_deals("en", "kommo") == "Romashka, Navruz Stroy"


async def test_a_stage_the_crm_does_not_have_is_refused_by_name(make: Any) -> None:
    h: Harness = await make(intent(refused=[{"what": "stage", "phrases": [1]}]))
    await _pick_demo(h, 541)
    await h.update(message(541, 1, text="По сделке Ромашка переведи на этап сверка с бухгалтерией."))
    assert "такой стадии в этой CRM нет" in h.tg.texts()[-1]


AV16 = "Автоматический выключатель IEK ВА47-29 1P C16 4,5 кА"
NYM = "Кабель NYM 3x2,5 (Севкабель)"


def _two_products_stage_task() -> dict[str, Any]:
    return intent(
        stage={"id": "1:20", "phrases": [1]},
        products=[
            {"name": AV16, "quantity": 20, "unit": "шт", "phrases": [1]},
            {"name": NYM, "quantity": 50, "unit": "м", "phrases": [1]},
        ],
        task={
            "kind": "call",
            "due": (date.today() + timedelta(days=3)).isoformat(),
            "time": None,
            "phrases": [1],
        },
    )


async def test_a_retry_after_a_partial_write_writes_each_step_once(make: Any, engine: Any) -> None:
    from alnuzum_crm.errors import CrmRateLimited

    h: Harness = await make(_two_products_stage_task())
    await _pick_demo(h, 561)
    await h.update(
        message(561, 1, text="По Ромашке на переговоры, 20 автоматов C16 и 50 метров NYM, позвонить.")
    )
    button = next(d for _, d in h.tg.buttons() if d.startswith("w:"))
    lines, failed = h.crm.add_lines, {"n": 0}

    async def second_line_fails_once(deal_id: str, got: Any) -> None:
        if got[0].name == NYM and not failed["n"]:
            failed["n"] = 1
            raise CrmRateLimited("429", crm="bitrix24")
        await lines(deal_id, got)

    h.crm.add_lines = second_line_fails_once  # type: ignore[method-assign]
    await h.update(callback(561, 2, button))
    said = h.tg.texts()[-1]
    assert "Записано шагов: 1 из 5, повтор допишет остальное" in said and "Ничего не потеряно" not in said
    ((steps, status),) = await rows(engine, "SELECT steps, status FROM voice_runs")
    assert steps == ["line:0"] and status == "failed"
    await h.update(callback(561, 3, button))  # the retry
    assert "Записано" in h.tg.texts()[-1]
    kinds = [(k, v[1][0].name if k == "lines" else None) for k, v in h.crm.writes]
    assert kinds == [("lines", AV16), ("lines", NYM), ("update", None), ("task", None), ("note", None)]
    await h.update(callback(561, 4, button))  # pressed again: written, nothing more
    assert len(h.crm.writes) == 5


class FakeAmo:
    """amoCRM's own add_lines as an httpx transport: a link of a product the deal has replaces its quantity,
    so the adapter adds to what it read; a retry that linked a product again would count it twice."""

    def __init__(self, names: dict[int, str]) -> None:
        self.names = names
        self.links: dict[int, float] = {}
        self.link_calls: list[list[int]] = []
        self.tasks: list[Any] = []
        self.notes: list[Any] = []
        self.patches: list[Any] = []
        self.fail_link: int | None = None  # the first link of this product meets 429

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == "/api/v4/account":
            return httpx.Response(200, json={"id": 1, "currency": "RUB"})
        if path == "/api/v4/catalogs":
            return httpx.Response(200, json={"_embedded": {"catalogs": [{"id": 7, "type": "products"}]}})
        if path == "/api/v4/catalogs/7/custom_fields":
            return httpx.Response(200, json={"_embedded": {"custom_fields": [{"id": 70, "code": "PRICE"}]}})
        if path == "/api/v4/catalogs/7/elements":
            ids = [int(x) for x in request.url.params.get_list("filter[id][]")]
            return httpx.Response(
                200, json={"_embedded": {"elements": [{"id": i, "name": self.names[i]} for i in ids]}}
            )
        if path == "/api/v4/leads/11" and method == "GET":
            have = [{"id": i, "metadata": {"quantity": q}} for i, q in self.links.items()]
            lead = {"id": 11, "name": "Демо · Ромашка", "price": 0, "pipeline_id": 1, "status_id": 10}
            return httpx.Response(200, json=lead | {"_embedded": {"catalog_elements": have}})
        if path == "/api/v4/leads/11/link":
            self.link_calls.append([int(x["to_entity_id"]) for x in body])
            if self.fail_link in self.link_calls[-1]:
                self.fail_link = None
                return httpx.Response(429, json={"title": "Too Many Requests"})
            for x in body:
                self.links[int(x["to_entity_id"])] = x["metadata"]["quantity"]  # replaces, as amoCRM does
            return httpx.Response(200, json={"_embedded": {"links": body}})
        if path == "/api/v4/leads/11" and method == "PATCH":
            self.patches.append(body)
            return httpx.Response(200, json={"id": 11})
        if path == "/api/v4/tasks":
            self.tasks.append(body)
            return httpx.Response(200, json={"_embedded": {"tasks": [{"id": len(self.tasks)}]}})
        if path == "/api/v4/leads/notes":
            self.notes.append(body)
            return httpx.Response(200, json={"_embedded": {"notes": [{"id": len(self.notes)}]}})
        raise AssertionError(f"unexpected {method} {path}")


async def test_a_retry_into_amocrm_does_not_add_the_products_twice(make: Any, engine: Any) -> None:
    from dataclasses import replace

    from alnuzum_crm.amocrm import AmoCRM
    from alnuzum_crm.http import RetryPolicy
    from alnuzum_crm.tokens import StaticToken

    h: Harness = await make(_two_products_stage_task())
    h.crm.products = [replace(p, id=str(500 + n)) for n, p in enumerate(h.crm.products, 1)]
    names = {int(p.id): p.name for p in h.crm.products}
    av16, nym = (next(int(p.id) for p in h.crm.products if p.name == n) for n in (AV16, NYM))
    amo = FakeAmo(names)
    amo.links[av16] = 5.0  # the deal already has five of them
    amo.fail_link = nym
    async with h.client() as c:
        token = csrf_of((await c.get("/ru/voice")).text)
        said = "По Ромашке на переговоры, 20 автоматов C16 и 50 метров NYM, позвонить."
        sent = await c.post("/ru/voice", data={"csrf": token, "target": "demo:bitrix24", "text": said})
        assert sent.status_code == 303
        h.app.state.connectors.adapter = lambda info, secrets, store: AmoCRM(
            "example.amocrm.ru",
            StaticToken("T", crm="amocrm"),
            rate=1000,
            retry=RetryPolicy(max_retries=0),
            transport=httpx.MockTransport(amo),
        )
        first = await c.post(sent.headers["location"], data={"csrf": token, "action": "write"})
        assert first.headers["location"].endswith("?n=failed")  # the second line met 429 after the first
        shown = await c.get(first.headers["location"])
        assert "Записано шагов: 1 из 5, повтор допишет остальное" in shown.text
        skip = {f"p{i}": "skip" for i in range(8)}  # a part is written: the plan no longer changes
        again = await c.post(sent.headers["location"], data={"csrf": token, "action": "write"} | skip)
        assert again.headers["location"].endswith("?n=written")
    assert amo.link_calls == [[av16], [nym], [nym]]  # the retry did not link the first product again
    assert amo.links == {av16: 25.0, nym: 50.0}  # 5 + 20, not 5 + 20 + 20
    assert (
        len(amo.tasks) == 1 and len(amo.notes) == 1 and amo.patches == [{"pipeline_id": 1, "status_id": 20}]
    )


async def test_voice_writes_into_demo_kommo_leave_the_deals_of_solution_1_alone(
    make: Any, engine: Any
) -> None:
    from alnuzum_orders.store import DemoLimits, OrderStore

    h: Harness = await make(
        *([intent(stage={"id": "1:20", "phrases": [1]})] * 3),
        voice_kommo_writes_per_day=2,
        orders_kw={"demo_kommo_domain": "acme.kommo.com", "demo_kommo_token": "AT-demo-kommo-secret"},
    )
    orders, limits = OrderStore(h.app.state.sessionmaker), {"kommo": DemoLimits(5, 5, 40, total=2)}
    assert await orders.demo_full(limits) == {}
    await h.update(callback(571, 1, "c:kommo"))
    for n in (2, 4, 6):
        await h.update(message(571, n, text="По сделке Ромашка переведи на переговоры."))
        await h.update(callback(571, n + 1, next(d for _, d in h.tg.buttons() if d.startswith("w:"))))
    journal = await rows(engine, "SELECT action FROM crm_writes WHERE crm = 'kommo' AND ok")
    assert [a for (a,) in journal] == ["voice_update", "voice_update"]
    assert "Демо Kommo на сегодня заполнено" in h.tg.texts()[-1]  # the third: voice's own ceiling a day
    assert await orders.demo_full(limits) == {}  # two voice writes, a total of two deals: still all there


async def test_speech_minutes_are_bounded_per_chat_and_others_keep_theirs(make: Any, engine: Any) -> None:
    h: Harness = await make(*([intent()] * 3))
    two_minutes = _wav(119.9)
    h.tg.files["two"] = two_minutes
    voice = {"file_id": "two", "duration": 120, "file_size": len(two_minutes)}
    await _pick_demo(h, 581)
    await _pick_demo(h, 582, n=2)
    for n in (1, 2, 3):
        await h.update(message(581, n, voice=voice))
    assert h.speech.calls == 2  # 2 + 2 minutes heard; the third two would pass 4
    assert "Ваш лимит распознавания на сегодня исчерпан" in h.tg.texts()[-1]
    await h.update(message(582, 4, voice=voice))
    assert h.speech.calls == 3 and "лимит" not in h.tg.texts()[-1]  # another chat hears its own
    heard = await rows(engine, "SELECT owner, units FROM voice_spend WHERE kind = 'stt' ORDER BY created_at")
    assert [(o, u) for o, u in heard] == [(chat_owner(581), 120)] * 2 + [(chat_owner(582), 120)]


async def test_speech_minutes_are_bounded_per_address_across_sessions(make: Any, engine: Any) -> None:
    h: Harness = await make(*([intent()] * 2))
    two_minutes = _wav(119.9)
    for n in range(3):  # three sessions from one address (the test client's)
        async with h.client() as c:
            token = csrf_of((await c.get("/ru/voice")).text)
            sent = await c.post(
                "/ru/voice",
                data={"csrf": token, "target": "demo:bitrix24"},
                files={"audio": ("note.wav", two_minutes, "audio/wav")},
            )
            if n < 2:
                assert sent.status_code == 303
    assert sent.status_code == 422 and "Ваш лимит распознавания на сегодня исчерпан" in sent.text
    assert h.speech.calls == 2


async def test_a_huge_text_is_refused_before_it_is_read(make: Any) -> None:
    h: Harness = await make()
    async with h.client() as c:
        token = csrf_of((await c.get("/ru/voice")).text)
        form = {"csrf": token, "target": "demo:bitrix24", "text": "x" * (8 * 1024 * 1024)}
        started = time.monotonic()
        plain = await c.post("/ru/voice", data=form)
        multipart = await c.post("/ru/voice", data=form, files={"audio": ("", b"", "audio/ogg")})
        took = time.monotonic() - started
        assert plain.status_code == multipart.status_code == 413
        assert "Текст длиннее 2000 знаков" in plain.text and "Текст длиннее 2000 знаков" in multipart.text
        fits = await c.post("/ru/voice", data=form | {"text": "я" * 2001})  # 4 KB: read, then refused
        assert fits.status_code == 422 and "Текст длиннее 2000 знаков" in fits.text
    assert took < 2 and h.messages.requests == []


# --- what a visitor reads before an upload ---

OFF_RU, OFF_EN = "Голосовые сейчас не принимаются", "Voice notes are not taken right now"
# ElevenLabs on a request its credits do not cover (a key's own limit or the plan's)
OUT_OF_CREDITS = {
    "detail": {
        "status": "quota_exceeded",
        "message": "This request exceeds your quota of 100000. You have 2 credits remaining, while 3 credits "
        "are required for this request.",
    }
}


async def test_the_reference_numbers_read_as_a_reader_writes_them(make: Any) -> None:
    from alnuzum_platform.web.i18n import percent

    assert [percent(v, "ru") for v in (1, 0.985, 0.0847, 0)] == [
        "100\u00a0%",
        "98,5\u00a0%",
        "8,5\u00a0%",
        "0\u00a0%",
    ]
    assert [percent(v, "en") for v in (1, 0.985, 0.0847, 0)] == ["100%", "98.5%", "8.5%", "0%"]
    h: Harness = await make()
    async with h.client() as c:
        ru, en = (await c.get("/ru/voice")).text, (await c.get("/en/voice")).text
    assert "распознавания 8,5\u00a0%, сделка найдена в 100\u00a0%, изменения без ошибок в 100\u00a0%" in ru
    assert "speech 8.5%, deal found in 100%, changes without a mistake in 100%" in en
    assert "8.5 %" not in ru + en and OFF_RU not in ru and OFF_EN not in en


async def test_out_of_credits_the_page_says_so_before_an_upload_and_the_bot_in_reply(
    make: Any, engine: Any
) -> None:
    h: Harness = await make(*[intent(stage={"id": "1:20", "phrases": [1]})] * 2)  # the typed and the heard
    note = {"target": "demo:bitrix24"}
    async with h.client() as c:
        token = csrf_of((await c.get("/ru/voice")).text)
        h.speech.fail, h.speech.body = (
            401,
            {"detail": {"status": "invalid_api_key", "message": "Invalid key"}},
        )
        wrong_key = await c.post(
            "/ru/voice", data={"csrf": token, **note}, files={"audio": ("a.ogg", SAMPLE)}
        )
        assert wrong_key.status_code == 422 and OFF_RU not in wrong_key.text  # not a matter of credits
        h.speech.body = OUT_OF_CREDITS
        out = await c.post("/ru/voice", data={"csrf": token, **note}, files={"audio": ("a.ogg", SAMPLE)})
        assert out.status_code == 422 and out.text.count(OFF_RU) == 1
        assert OFF_RU in (await c.get("/ru/voice")).text and OFF_EN in (await c.get("/en/voice")).text
    spent = await rows(engine, "SELECT kind, usd, units FROM voice_spend WHERE units = 0 ORDER BY created_at")
    assert [(k, Decimal(u), n) for k, u, n in spent] == [("stt", 0, 0), ("stt_out", 0, 0)]  # refused: free
    await _pick_demo(h, 931)
    h.tg.files["q1"] = SAMPLE
    await h.update(message(931, 1, voice={"file_id": "q1", "duration": 7, "file_size": len(SAMPLE)}))
    assert OFF_RU in h.tg.texts()[-1]  # the bot answers a voice note the same
    await h.update(message(931, 2, text="По сделке Ромашка переведи на переговоры."))
    assert "Переговоры" in h.tg.texts()[-1]  # a typed command still works
    h.speech.fail = None  # credits are back: the first note heard clears the line
    async with h.client() as c:
        token = csrf_of((await c.get("/ru/voice")).text)
        sent = await c.post("/ru/voice", data={"csrf": token, **note}, files={"audio": ("a.ogg", SAMPLE)})
        assert sent.status_code == 303
        assert OFF_RU not in (await c.get("/ru/voice")).text


async def test_the_day_s_minutes_spent_or_no_key_the_page_says_so(make: Any, engine: Any) -> None:
    h: Harness = await make(voice_stt_minutes_per_day=1)
    async with h.client() as c:
        assert OFF_RU not in (await c.get("/ru/voice")).text
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO voice_spend (id, owner, kind, usd, units) VALUES (:i, 'x', 'stt', 0, 56)"),
                {"i": uuid.uuid4()},
            )
        assert OFF_RU in (await c.get("/ru/voice")).text  # 4 s left: no spoken command fits
    keyless: Harness = await make(elevenlabs_api_key="")
    async with keyless.client() as c:
        assert OFF_EN in (await c.get("/en/voice")).text


def test_the_telegram_hook_is_the_path_outside_the_limit_of_an_address() -> None:
    from alnuzum_voice.web import HOOK

    from alnuzum_platform.web.security import UNLIMITED

    assert HOOK in UNLIMITED


async def test_a_write_or_a_reading_that_breaks_outside_the_crm_errors_leaves_no_run_stuck(
    make: Any, engine: Any
) -> None:
    h: Harness = await make(*[intent(stage={"id": "1:20", "phrases": [1]})] * 2)
    await _pick_demo(h, 571)
    found = h.crm.find_deals

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("a bug of an adapter")

    h.crm.find_deals = broken  # type: ignore[method-assign]
    await h.update(message(571, 1, text="По сделке Ромашка переведи на переговоры."))
    assert "Не удалось разобрать" in h.tg.texts()[-1]  # the person gets an answer
    assert await rows(engine, "SELECT status, error FROM voice_runs") == [("rejected", "error")]
    h.crm.find_deals = found  # type: ignore[method-assign]
    await h.update(message(571, 2, text="По сделке Ромашка переведи на переговоры."))
    button = next(d for _, d in h.tg.buttons() if d.startswith("w:"))
    h.crm.update_deal = broken  # type: ignore[method-assign]
    await h.update(callback(571, 3, button))
    assert "Не удалось записать" in h.tg.texts()[-1]
    assert await rows(engine, "SELECT status, error FROM voice_runs WHERE result IS NOT NULL") == [
        ("failed", "error")
    ]
    assert await rows(engine, "SELECT ok, error FROM crm_writes") == [(False, "error")]


async def test_runs_a_stopped_process_left_writing_or_reading_are_released_at_start(
    make: Any, engine: Any
) -> None:
    from alnuzum_voice import web

    h: Harness = await make(*[intent(stage={"id": "1:20", "phrases": [1]})] * 3)
    await _pick_demo(h, 572)
    for n in range(3):
        await h.update(message(572, 10 + n, text="По сделке Ромашка переведи на переговоры."))
    ids = [r[0] for r in await rows(engine, "SELECT id FROM voice_runs ORDER BY created_at")]
    stale = (
        "UPDATE voice_runs SET status = CAST(:s AS varchar), created_at = now() - make_interval(mins => :c), "
        "writing_at = CASE WHEN CAST(:s AS varchar) = 'writing' THEN now() - make_interval(mins => :w) END WHERE id = :i"
    )
    async with engine.begin() as conn:
        for rid, state, age in zip(ids, ("writing", "writing", "reading"), (31, 5, 31), strict=True):
            await conn.execute(text(stale), {"s": state, "c": age + 1, "w": age, "i": rid})
    await web._startup(h.app)
    got = {r[0]: (r[1], r[2]) for r in await rows(engine, "SELECT id, status, error FROM voice_runs")}
    assert got == {
        ids[0]: ("failed", "interrupted"),
        ids[1]: ("writing", None),
        ids[2]: ("rejected", "interrupted"),
    }


async def test_our_busy_decoder_does_not_take_a_persons_attempt(
    make: Any, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h: Harness = await make(intent(stage={"id": "1:20", "phrases": [1]}), voice_runs_per_owner=1)
    await _pick_demo(h, 591)
    taken = threading.BoundedSemaphore(1)
    taken.acquire()  # both slots of the decoder are busy with others
    monkeypatch.setattr(audio, "_SLOTS", taken)
    monkeypatch.setattr(audio, "SECONDS", 0.05)
    h.tg.files["b1"] = SAMPLE
    note = {"file_id": "b1", "duration": 7, "file_size": len(SAMPLE)}
    await h.update(message(591, 1, voice=note))
    assert "Сейчас занято" in h.tg.texts()[-1]
    assert await rows(engine, "SELECT count(*) FROM voice_runs") == [(0,)]
    monkeypatch.undo()  # the decoder is free again
    await h.update(message(591, 2, voice=note))  # the only attempt of the day is still there
    assert "Лимит разборов" not in h.tg.texts()[-1]
    assert await rows(engine, "SELECT status FROM voice_runs") == [("ready",)]


async def test_the_webhook_keeps_at_most_a_few_updates_in_work(make: Any) -> None:
    from alnuzum_voice.web import MAX_TASKS

    h: Harness = await make()
    gate = asyncio.Event()
    for _ in range(MAX_TASKS):
        h.voice.tasks.add(asyncio.ensure_future(gate.wait()))
    async with h.client() as c:
        resp = await c.post(
            "/hooks/voice/telegram",
            json=message(592, 1, text="/start"),
            headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
        )
    assert resp.status_code == 503  # Telegram sends the update again later; nothing is lost
    assert len(h.voice.tasks) == MAX_TASKS
    gate.set()
    await asyncio.gather(*list(h.voice.tasks))
    h.voice.tasks.clear()
    assert (await h.update(message(592, 1, text="/start"))).status_code == 200
    assert h.tg.texts()  # the same update, sent again, is answered


async def test_a_test_account_over_its_records_limit_says_the_demo_is_unavailable(
    make: Any, engine: Any
) -> None:
    from alnuzum_crm.amocrm import ACCOUNT_LIMIT
    from alnuzum_crm.errors import CrmPermissionError

    h: Harness = await make(*[intent(stage={"id": "1:20", "phrases": [1]})] * 2)
    await _pick_demo(h, 593)

    async def full(*_a: Any, **_k: Any) -> None:
        raise CrmPermissionError("Payment Required", crm="bitrix24", status=400, code=ACCOUNT_LIMIT)

    h.crm.update_deal = full  # type: ignore[method-assign]
    await h.update(message(593, 1, text="По сделке Ромашка переведи на переговоры."))
    button = next(d for _, d in h.tg.buttons() if d.startswith("w:"))
    await h.update(callback(593, 2, button))
    assert "Демо Битрикс24 временно недоступно" in h.tg.texts()[-1]
    # the next visitor does not go to the CRM for half an hour: the same words, no write
    h.crm.update_deal = None  # type: ignore[assignment,method-assign]
    await h.update(message(593, 3, text="По сделке Ромашка переведи на переговоры."))
    button = next(d for _, d in h.tg.buttons() if d.startswith("w:"))
    await h.update(callback(593, 4, button))
    assert "Демо Битрикс24 временно недоступно" in h.tg.texts()[-1]
    assert await rows(engine, "SELECT count(*) FROM crm_writes") == [(1,)]
