"""Pages of the solution and the Telegram webhook."""

import asyncio
import hmac
import json
import logging
import uuid
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from alnuzum_orders.catalog import CatalogItem
from alnuzum_orders.store import DEMO_OWNER
from alnuzum_platform.solutions import Kit, Solution
from alnuzum_platform.store import ConnectionInfo
from alnuzum_platform.web.routes import PageError, redirect
from alnuzum_platform.web.security import RateLimiter
from alnuzum_voice import audio
from alnuzum_voice.bot import Updates
from alnuzum_voice.engine import Change, Result, choose_product, steps
from alnuzum_voice.i18n import commands_word, texts
from alnuzum_voice.service import Note, Voice, VoiceError
from alnuzum_voice.store import RunNotFound, RunsExceeded
from alnuzum_voice.text import MAX_PHRASES

log = logging.getLogger("alnuzum_voice.web")
HERE = Path(__file__).parent
HOOK = "/hooks/voice/telegram"
MAX_UPDATE = 64 * 1024
MAX_FIELD = 20 * 1024  # a typed command is at most MAX_TEXT characters: 20 KB holds it in any script


def link_css(limit: int = MAX_PHRASES) -> str:
    """Hovering a change lights its phrases, and the other way round: plain CSS, the pages run no script."""
    rules = [
        f'.oc:has(.it[data-s~="{n}"]:hover,.it[data-s~="{n}"]:focus-within) .src [data-n="{n}"],'
        f'.oc:has(.src [data-n="{n}"]:hover) .it[data-s~="{n}"]'
        "{background:var(--hl);box-shadow:inset 3px 0 0 var(--hl-edge)}"
        for n in range(1, limit + 1)
    ]
    return "\n".join(rules) + "\n"


LINK_CSS = link_css()


def samples() -> list[dict[str, Any]]:
    truth = json.loads(resources.files("alnuzum_voice").joinpath("samples/truth.json").read_text())
    return [{"id": c["id"], "lang": c["lang"], "text": c["text"]} for c in truth["commands"]]


SAMPLES = {s["id"]: s for s in samples()}


def quality() -> dict[str, Any] | None:
    try:
        data = json.loads(resources.files("alnuzum_voice").joinpath("quality.json").read_text())
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ip_key(request: Request) -> str:
    return RateLimiter.client(request.client.host if request.client else "?")[:64]


def _lang(value: str) -> str:
    if value not in ("ru", "en"):
        raise PageError(404, "e_404_t", "e_404")
    return {"ru": "ru", "en": "en"}[value]


def _uuid(value: str, lang: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise PageError(404, "e_404_t", "e_404", lang) from None


def build(kit: Kit, voice: Voice) -> APIRouter:
    router = APIRouter()
    updates = Updates(voice, kit)

    # first: «/{lang}/voice/{run_id}» below would match «/hooks/voice/telegram» too
    @router.post(HOOK)
    async def webhook(request: Request) -> Response:
        secret = voice.settings.voice_tg_secret.get_secret_value()
        given = request.headers.get("x-telegram-bot-api-secret-token", "")
        # a request without the secret is refused before its body is read
        if not secret or not hmac.compare_digest(given.encode("utf-8", "replace"), secret.encode()):
            return JSONResponse({"ok": False}, status_code=403)
        body = await request.body()
        if len(body) > MAX_UPDATE:
            return JSONResponse({"ok": False}, status_code=413)
        try:
            update = json.loads(body)
        except ValueError:
            return JSONResponse({"ok": False}, status_code=400)
        if isinstance(update, dict):
            # answered at once: Telegram waits for no reading; the work goes on in the background
            task = asyncio.create_task(updates.handle(request.app, update))
            voice.tasks.add(task)
            task.add_done_callback(voice.tasks.discard)
        return JSONResponse({"ok": True})

    @router.get("/voice/link.css")
    async def css() -> Response:
        return Response(LINK_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})

    async def target(request: Request, lang: str, value: str) -> tuple[ConnectionInfo, bool]:
        st = kit.store_of(request)
        if value.startswith("demo:"):
            ids = await voice.demo_ids(request.app)
            if value[5:] not in ids:
                raise PageError(404, "e_404_t", "e_demo_off", lang)
            return await st.get(ids[value[5:]], DEMO_OWNER), True
        if value.startswith("conn:"):
            info = await kit.own_connection(request, lang, _uuid(value[5:], lang))
            if info.status != "active":
                raise PageError(409, "e_404_t", "e_reconnect", lang)
            return info, False
        raise PageError(404, "e_404_t", "e_404", lang)

    async def start(request: Request, lang: str, *, status: int = 200, **ctx: Any) -> HTMLResponse:
        sid = kit.ensure_session(request)
        owner = kit.owner(sid)
        st = kit.store_of(request)
        own = [c for c in await st.list(owner) if c.status == "active"]
        bound = await voice.store(request.app).bound_chats(owner) if own else {}
        demos = sorted(await voice.demo_ids(request.app))
        return kit.page(
            request,
            "voice.html",
            lang,
            status,
            own=own,
            bound=bound,
            demos=demos,
            samples=list(SAMPLES.values()),
            bot=voice.settings.voice_tg_bot,
            speech_off=await voice.speech_off(request.app),
            q=(q := quality()),
            q_word=commands_word(lang, int(q["commands"])) if q else "",
            **ctx,
        )

    @router.get("/{lang}/voice")
    async def index(request: Request, lang: str) -> HTMLResponse:
        return await start(request, _lang(lang))

    @router.post("/{lang}/voice")
    async def submit(request: Request, lang: str) -> Response:
        lang = _lang(lang)
        try:
            # a field (the text) holds at most MAX_FIELD bytes, the parser stops there; a file is no field
            form = await request.form(max_files=1, max_fields=8, max_part_size=MAX_FIELD)
        except HTTPException:
            return await start(request, lang, status=413, error="e_too_long_text")
        sid = kit.need_csrf(request, form, lang)
        owner = kit.owner(sid)
        info, demo = await target(request, lang, str(form.get("target") or ""))
        upload = form.get("audio")
        note: Note | None = None
        if isinstance(upload, UploadFile) and upload.filename:
            data = await upload.read(audio.MAX_BYTES + 1)
            if len(data) > audio.MAX_BYTES:
                return await start(request, lang, status=413, error="e_too_big")
            note = Note(audio=data)
        elif (sample := str(form.get("sample") or "")) in SAMPLES:
            note = Note(audio=resources.files("alnuzum_voice").joinpath(f"samples/{sample}.ogg").read_bytes())
        elif isinstance(form.get("text"), str) and str(form.get("text")).strip():
            note = Note(text=str(form.get("text")))
        if note is None:
            return await start(request, lang, status=400, error="e_empty")
        vs = voice.store(request.app)
        try:
            run_id = await vs.reserve_run(
                owner=owner,
                ip_key=ip_key(request),
                chat_id=None,
                connection_id=info.id,
                crm=info.crm.value,
                demo=demo,
                limits=voice.settings.limits(),
                count_ip=True,
            )
        except RunsExceeded:
            return await start(request, lang, status=429, error="e_runs")
        try:
            await voice.read(
                request.app,
                kit,
                run_id=run_id,
                owner=owner,
                info=info,
                demo=demo,
                note=note,
                ip_key=ip_key(request),
            )
        except VoiceError as exc:
            return await start(request, lang, status=422, error=exc.code)
        return redirect(f"/{lang}/voice/{run_id}")

    @router.post("/{lang}/voice/link/{conn_id}")
    async def link(request: Request, lang: str, conn_id: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=4, max_part_size=1024)
        sid = kit.need_csrf(request, form, lang)
        info = await kit.own_connection(request, lang, _uuid(conn_id, lang))
        vs = voice.store(request.app)
        if form.get("action") == "unbind":
            await vs.unbind_connection(kit.owner(sid), info.id)
            return redirect(f"/{lang}/voice")
        if info.status != "active" or not voice.settings.voice_tg_bot:
            raise PageError(409, "e_404_t", "e_reconnect", lang)
        token = await vs.new_link(kit.owner(sid), info.id)
        return await start(
            request,
            lang,
            link_for=str(info.id),
            link_url=f"https://t.me/{voice.settings.voice_tg_bot}?start={token}",
        )

    async def own_run(request: Request, lang: str, run_id: str) -> Any:
        sid = kit.need_session(request, lang)
        try:
            return await voice.store(request.app).get_run(_uuid(run_id, lang), kit.owner(sid))
        except RunNotFound:
            raise PageError(404, "e_404_t", "e_404", lang) from None

    async def show(
        request: Request, lang: str, run: Any, *, status: int = 200, notice: str | None = None
    ) -> HTMLResponse:
        if run.status not in ("ready", "writing", "written", "failed", "cancelled") or not run.result:
            raise PageError(404, "e_404_t", "e_404", lang)
        result = Result.from_json(run.result)
        total = len(steps(Result.from_json(run.result), lang=lang))
        return kit.page(
            request, "voice_run.html", lang, status, run=run, r=result, notice=notice, steps_total=total
        )

    @router.get("/{lang}/voice/{run_id}")
    async def view(request: Request, lang: str, run_id: str) -> HTMLResponse:
        lang = _lang(lang)
        n = request.query_params.get("n")
        return await show(
            request, lang, await own_run(request, lang, run_id), notice=n if n and n.isalpha() else None
        )

    @router.post("/{lang}/voice/{run_id}")
    async def decide(request: Request, lang: str, run_id: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=40, max_part_size=1024)
        sid = kit.need_csrf(request, form, lang)
        run = await own_run(request, lang, run_id)
        back = f"/{lang}/voice/{run.id}"
        if run.status not in ("ready", "failed") or not run.result:
            return redirect(back)
        vs = voice.store(request.app)
        if form.get("action") == "cancel":
            await vs.cancel(run.id, run.owner)
            return redirect(back)
        result = Result.from_json(run.result)
        apply_form(result, {k: v for k, v in form.items() if isinstance(v, str)})
        saved = await vs.save_result(run.id, run.owner, result.to_json())  # not once a part is written
        if form.get("action") != "write":
            return redirect(f"{back}?n=saved" if saved else back)
        if not result.deal_id:
            return redirect(f"{back}?n=no_deal")
        fresh = await vs.get_run(run.id, run.owner)
        notice = await voice.confirm(request.app, kit, fresh, conn_owner=kit.owner(sid), lang=lang)
        return redirect(f"{back}?n={notice}")

    return router


def apply_form(result: Result, form: dict[str, str]) -> None:
    """The person's choices: the deal among the candidates, a product among the candidates of a line."""
    chosen = form.get("deal")
    if chosen and any(str(c["id"]) == chosen for c in result.candidates):
        result.deal_id, result.deal_by = chosen, "person"
    for i, ch in enumerate(result.changes):
        pick = form.get(f"p{i}")
        if ch.kind != "product" or not pick:
            continue
        if pick == "skip":
            ch.state, ch.why = "off", "unsure"
            continue
        cand = next((c for c in ch.candidates if c["id"] == pick), None)
        if cand is not None:
            _choose(ch, cand)


def _choose(ch: Change, cand: dict[str, Any]) -> None:
    price = cand.get("price")
    item = CatalogItem(
        str(cand["id"]),
        str(cand["name"]),
        cand.get("sku"),
        None if price is None else Decimal(str(price)),
        None,
        str(cand.get("unit") or "шт"),
        None,
        None,
        packs={k: Decimal(v) for k, v in (cand.get("packs") or {}).items()},
    )
    choose_product(ch, item, cand.get("said_unit"))


class _Solution:
    voice = Voice()


async def _startup(app: FastAPI) -> None:
    bot, s = _Solution.voice.bot(), _Solution.voice.settings
    if bot is None or not s.voice_tg_secret.get_secret_value():
        log.info("voice bot: off")
        return
    conf = getattr(getattr(app.state, "connectors", None), "settings", None)
    public = str(getattr(conf, "public_url", "") or "")
    if not public.startswith("https://"):
        log.info("voice bot: no https address, webhook not set")
        return
    try:
        await bot.set_webhook(public.rstrip("/") + HOOK, s.voice_tg_secret.get_secret_value())
        log.info("voice bot: webhook set")
    except Exception as exc:  # the pages work without the bot
        log.warning("voice bot: webhook not set: %s", type(exc).__name__)


async def _sweep(app: FastAPI) -> None:
    if getattr(app.state, "sessionmaker", None) is not None:
        runs, spend = await Voice.store(app).purge()
        log.info("voice sweep: %d runs and %d old spends erased", runs, spend)


def make_solution(voice: Voice) -> Solution:
    _Solution.voice = voice
    return Solution(
        slug="voice",
        titles={"ru": "Голос менеджера → CRM", "en": "Manager's voice → CRM"},
        templates=HERE / "templates",
        static=HERE / "static",
        texts=texts,
        router=lambda kit: build(kit, voice),
        startup=_startup,
        sweep=_sweep,
    )


solution = make_solution(Voice())

__all__ = ["make_solution", "solution"]
