"""Pages of the solution: a letter in, the letter beside what was read out of it and what the CRM holds, the
lead, the note and the task by a button; the routing rules of a CRM. The pages run no script; hovering a field
lights its place in the letter through CSS only."""

import json
import logging
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from alnuzum_contracts.engine import TZ
from alnuzum_contracts.web import dollars, ip_key
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from alnuzum_crm.errors import CrmError
from alnuzum_mail import intake
from alnuzum_mail.engine import DEMO_PREFIX, lit
from alnuzum_mail.i18n import texts
from alnuzum_mail.intake import Letter
from alnuzum_mail.model import CLASSES, FIELDS, Reading
from alnuzum_mail.rules import KINDS, LANGUAGES, MAX_RULES, RuleError, load, parse
from alnuzum_mail.service import Mail, MailError, crm_error, sample_bytes, samples, target_key
from alnuzum_mail.store import KEEP, MailStore, NotFound, RunInfo, RunsExceeded
from alnuzum_platform.solutions import Kit, Solution
from alnuzum_platform.store import ConnectionDisabled, ConnectionInfo, ConnectionNotFound
from alnuzum_platform.web.routes import PageError, redirect

log = logging.getLogger("alnuzum_mail.web")
HERE = Path(__file__).parent
MAX_BODY = intake.MAX_BYTES + 512 * 1024  # Caddy lets 9 MB through for this path
MAX_FIELD = 4 * intake.MAX_PASTE + 4096  # a pasted letter in UTF-8


def quality() -> dict[str, Any] | None:
    try:
        data = json.loads(resources.files("alnuzum_mail").joinpath("quality.json").read_text())
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lang(value: str) -> str:
    if value not in ("ru", "en"):
        raise PageError(404, "e_404_t", "e_404")
    return {"ru": "ru", "en": "en"}[value]


def _uuid(value: str, lang: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise PageError(404, "e_404_t", "e_404", lang) from None


def _body(request: Request, lang: str) -> None:
    declared = request.headers.get("content-length", "")
    if not declared.isdigit() or int(declared) > MAX_BODY:
        raise PageError(413, "e_size_t", "e_size", lang)


async def _file(form: Any) -> bytes | None:
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        data = await upload.read(intake.MAX_BYTES + 1)
        await upload.close()
        return data
    return None


def build(kit: Kit, mail: Mail) -> APIRouter:
    router = APIRouter()

    async def target(request: Request, lang: str, value: str) -> tuple[ConnectionInfo, bool]:
        st = kit.store_of(request)
        if value.startswith("demo:"):
            ids = await mail.demo_ids(request.app)
            if value[5:] not in ids:
                raise PageError(404, "e_404_t", "e_demo_off", lang)
            return await st.get(ids[value[5:]], "demo"), True
        if value.startswith("conn:"):
            info = await kit.own_connection(request, lang, _uuid(value[5:], lang))
            if info.status != "active":
                raise PageError(409, "e_404_t", "e_reconnect", lang)
            return info, False
        raise PageError(404, "e_404_t", "e_404", lang)

    async def start(request: Request, lang: str, *, status: int = 200, **ctx: Any) -> HTMLResponse:
        sid = kit.ensure_session(request)
        st = kit.store_of(request)
        own = [c for c in await st.list(kit.owner(sid)) if c.status == "active"]
        return kit.page(
            request,
            "mail.html",
            lang,
            status,
            own=own,
            demos=sorted(await mail.demo_ids(request.app)),
            samples=list(samples().values()),
            q=quality(),
            **ctx,
        )

    @router.get("/{lang}/mail")
    async def index(request: Request, lang: str) -> HTMLResponse:
        return await start(request, _lang(lang))

    @router.post("/{lang}/mail")
    async def submit(request: Request, lang: str) -> Response:
        lang = _lang(lang)
        _body(request, lang)
        try:
            form = await request.form(max_files=1, max_fields=8, max_part_size=MAX_FIELD)
        except HTTPException:
            return await start(request, lang, status=413, error="e_size")
        sid = kit.need_csrf(request, form, lang)
        owner = kit.owner(sid)
        info, demo = await target(request, lang, str(form.get("target") or ""))
        # only what costs nothing is checked before the quota: which input and its size
        sample = str(form.get("sample") or "")
        data = await _file(form)
        text = form.get("text")
        pasted = text if isinstance(text, str) and text.strip() else None
        if data is None and pasted is None and sample:
            if sample not in samples() or not demo:
                return await start(request, lang, status=400, error="e_sample")
            data = sample_bytes(samples()[sample]["file"])
        if not data and not pasted:
            return await start(request, lang, status=400, error="e_empty")
        if (data and len(data) > intake.MAX_BYTES) or (pasted and len(pasted) > intake.MAX_PASTE):
            return await start(request, lang, status=413, error="e_size")
        ms = mail.store(request.app, kit)
        try:
            run_id = await ms.reserve_run(
                owner=owner,
                ip_key=ip_key(request),
                connection_id=info.id,
                crm=info.crm.value,
                demo=demo,
                limits=mail.settings.limits(),
            )
        except RunsExceeded:
            return await start(request, lang, status=429, error="e_runs")
        try:
            await mail.read(
                request.app,
                kit,
                ms,
                run_id=run_id,
                owner=owner,
                info=info,
                demo=demo,
                data=data,
                text=pasted,
                lang=lang,
            )
        except MailError as exc:
            return await start(request, lang, status=422, error=exc.code, error_n=_limit(exc.code))
        return redirect(f"/{lang}/mail/{run_id}")

    async def own_run(request: Request, lang: str, run_id: str) -> RunInfo:
        sid = kit.need_session(request, lang)
        try:
            return await mail.store(request.app, kit).get_run(_uuid(run_id, lang), kit.owner(sid))
        except NotFound:
            raise PageError(404, "e_404_t", "e_404", lang) from None

    def show(request: Request, lang: str, run: RunInfo, *, notice: str | None = None) -> HTMLResponse:
        if run.result is None or run.status not in ("ready", "writing", "written", "failed", "closed"):
            raise PageError(404, "e_404_t", "e_404", lang)
        r = run.result
        letter, reading = Letter.of(r["letter"]), Reading.of(r["reading"])
        return kit.page(
            request,
            "mail_run.html",
            lang,
            run=run,
            r=r,
            letter=letter,
            reading=reading,
            fields=[(k, reading.fields[k]) for k in FIELDS if k in reading.fields],
            src=lit(letter, reading),
            b=r["binding"],
            route=r["route"],
            choice=r.get("choice") or {},
            classes=CLASSES,
            demo_prefix=DEMO_PREFIX,
            until=(run.created_at + KEEP).astimezone(TZ).strftime("%d.%m.%Y %H:%M"),
            cost=dollars(r.get("usage", {}).get("usd", "0"), lang),
            notice=notice,
        )

    @router.get("/{lang}/mail/{run_id}")
    async def view(request: Request, lang: str, run_id: str) -> HTMLResponse:
        lang = _lang(lang)
        n = request.query_params.get("n")
        return show(
            request,
            lang,
            await own_run(request, lang, run_id),
            notice=n if n and n.replace("_", "").isalpha() else None,
        )

    @router.post("/{lang}/mail/{run_id}")
    async def decide(request: Request, lang: str, run_id: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=10, max_part_size=1024)
        sid = kit.need_csrf(request, form, lang)
        run = await own_run(request, lang, run_id)
        back = f"/{lang}/mail/{run.id}"
        ms = mail.store(request.app, kit)
        action = form.get("action")
        if action == "erase":
            if await ms.erase(run.id, run.owner) == "pending":  # being written: erased when it ends
                return redirect(f"{back}?n=erase_pending")
            return redirect(f"/{lang}/mail?n=erased")
        if run.status not in ("ready", "failed") or (run.steps and action != "write"):
            return redirect(back)
        if action in ("look", "search"):
            query = str(form.get("q") or "") if action == "search" else None
            notice = await mail.look_again(request.app, kit, run, conn_owner=kit.owner(sid), query=query)
            return redirect(f"{back}?n={notice}")
        if action == "write":
            if not run.steps:
                choice = _choice(form, run)
                if choice is None:
                    return redirect(f"{back}?n=no_choice")
                if choice["cls"] == "junk":
                    await ms.close_junk(run.id, run.owner)
                    return redirect(f"{back}?n=closed")
                result = dict(run.result or {}) | {"choice": choice}
                await ms.save_result(run.id, run.owner, result)
                run = await ms.get_run(run.id, run.owner)
            notice = await mail.confirm(
                request.app, kit, run, conn_owner=kit.owner(sid), ip_key=ip_key(request)
            )
            return redirect(f"{back}?n={notice}")
        return redirect(back)

    # --- the routing rules ---

    async def rules_target(request: Request, lang: str, value: str) -> tuple[ConnectionInfo, bool, str]:
        if value.startswith("demo:"):
            info, demo = await target(request, lang, value)
        else:
            info, demo = await target(request, lang, "conn:" + value)
        return info, demo, target_key(info, demo)

    async def choices(
        request: Request, info: ConnectionInfo, demo: bool
    ) -> tuple[list[Any], list[Any], str | None]:
        """The stages of the CRM and its users (an own CRM only: the test accounts' people are not shown)."""
        try:
            async with await mail.adapter(request.app, kit, info) as crm:
                stages = await crm.stages()
                users = [] if demo else await crm.users()
        except (ConnectionDisabled, ConnectionNotFound):
            return [], [], "auth"
        except CrmError as exc:
            return [], [], "auth" if crm_error(exc) == "auth" else "unavailable"
        return [s for s in stages if s.kind.value == "open"], users, None

    async def rules_page(
        request: Request,
        lang: str,
        value: str,
        *,
        status: int = 200,
        typed: dict[str, str] | None = None,
        **ctx: Any,
    ) -> HTMLResponse:
        sid = kit.need_session(request, lang)
        info, demo, key = await rules_target(request, lang, value)
        stages, users, problem = await choices(request, info, demo)
        saved = await mail.store(request.app, kit).get_rules(kit.owner(sid), key)
        return kit.page(
            request,
            "mail_rules.html",
            lang,
            status,
            info=info,
            demo=demo,
            target=value,
            rules=load(saved),
            stages=stages,
            users=users,
            problem=problem,
            kinds=KINDS,
            languages=LANGUAGES,
            rows=MAX_RULES,
            typed=typed,
            **ctx,
        )

    @router.get("/{lang}/mail/rules/{value}")
    async def rules_view(request: Request, lang: str, value: str) -> HTMLResponse:
        return await rules_page(request, _lang(lang), value)

    @router.post("/{lang}/mail/rules/{value}")
    async def rules_save(request: Request, lang: str, value: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=4 * MAX_RULES + 4, max_part_size=1024)
        sid = kit.need_csrf(request, form, lang)
        info, demo, key = await rules_target(request, lang, value)
        stages, users, problem = await choices(request, info, demo)
        typed = {k: v for k, v in form.items() if isinstance(v, str)}
        if problem:
            return await rules_page(request, lang, value, status=503, typed=typed, error="e_rules_crm")
        try:
            rules = parse(typed, {s.id for s in stages}, {u.id for u in users})
        except RuleError as exc:
            return await rules_page(
                request, lang, value, status=400, typed=typed, error=exc.code, error_n=exc.n
            )
        names = {f"stage:{s.id}": (s.name + (f" · {s.pipeline}" if s.pipeline else ""))[:120] for s in stages}
        names |= {f"user:{u.id}": u.name[:120] for u in users}
        used = {f"stage:{r.stage}" for r in rules if r.stage} | {f"user:{r.user}" for r in rules if r.user}
        await mail.store(request.app, kit).set_rules(
            kit.owner(sid),
            key,
            {"rules": [r.to_json() for r in rules], "names": {k: v for k, v in names.items() if k in used}},
        )
        return redirect(f"/{lang}/mail/rules/{value}?n=saved")

    return router


def _limit(code: str) -> int | None:
    return {"e_too_long": intake.MAX_BODY}.get(code)


def _choice(form: Any, run: RunInfo) -> dict[str, Any] | None:
    """What the person chose, checked against what the page offered."""
    r = run.result or {}
    cls = str(form.get("cls") or "")
    if cls not in CLASSES:
        return None
    binding = r.get("binding") or {}
    if cls == "junk":
        return {"cls": cls}
    if cls == "reply":
        deal = str(form.get("deal") or "")
        if deal not in {str(d["id"]) for d in binding.get("deals") or []}:
            return None
        return {"cls": cls, "deal": deal}
    company = str(form.get("company") or "none")
    has_name = "company" in (r.get("reading") or {}).get("fields", {})
    if company not in {str(c["id"]) for c in binding.get("companies") or []} | {"none"} | (
        {"new"} if has_name else set()
    ):
        return None
    if company == "new" and not binding.get("checked"):
        # unbound: a new company could be a duplicate of one the CRM did not show; look again first
        return None
    return {"cls": cls, "company": company}


class _Solution:
    mail = Mail()


def _store(app: FastAPI) -> MailStore | None:
    maker = getattr(app.state, "sessionmaker", None)
    conf = getattr(getattr(app.state, "connectors", None), "settings", None)
    keys = getattr(conf, "secret_keys", None)
    if maker is None or keys is None or not keys.get_secret_value():
        return None
    from alnuzum_contracts.store import Seal

    from alnuzum_platform.crypto import Keyring

    return MailStore(maker, Seal(Keyring.from_env(keys.get_secret_value())))


async def _startup(app: FastAPI) -> None:
    """The runs a stopped process left "writing" or "reading" are released."""
    if (ms := _store(app)) is None:
        return
    written, read = await ms.release_stuck()
    if written or read:
        log.warning("mail: %d runs left writing and %d left reading released", written, read)


async def _sweep(app: FastAPI) -> None:
    await _startup(app)
    if (ms := _store(app)) is None:
        return
    runs, rules = await ms.purge()
    log.info("mail sweep: %d runs and %d rules erased", runs, rules)


def make_solution(mail: Mail) -> Solution:
    _Solution.mail = mail
    return Solution(
        slug="mail",
        titles={"ru": "Разбор входящей почты", "en": "Incoming mail sorted"},
        templates=HERE / "templates",
        static=HERE / "static",
        texts=texts,
        router=lambda kit: build(kit, mail),
        startup=_startup,
        sweep=_sweep,
    )


solution = make_solution(Mail())
