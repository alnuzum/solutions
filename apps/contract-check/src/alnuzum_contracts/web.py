"""Pages of the solution: a draft against a standard, the comparison beside the draft, the note and the task
in a deal. The pages run no script; hovering a change lights its fragments through CSS only."""

import json
import logging
import uuid
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from alnuzum_contracts import intake
from alnuzum_contracts.engine import KINDS, RISKS, TOPICS, TZ, clause_name, comparison, ordered, quotes, rows
from alnuzum_contracts.i18n import texts
from alnuzum_contracts.model import SIDES
from alnuzum_contracts.service import (
    ContractError,
    Contracts,
    Standard,
    demo_standards,
    sample_bytes,
    samples,
)
from alnuzum_contracts.store import KEEP, ContractStore, NotFound, RunInfo, RunsExceeded, Seal
from alnuzum_files import FileError
from alnuzum_orders.model import ModelError
from alnuzum_platform.solutions import Kit, Solution
from alnuzum_platform.store import ConnectionInfo
from alnuzum_platform.web.routes import PageError, redirect
from alnuzum_platform.web.security import RateLimiter

log = logging.getLogger("alnuzum_contracts.web")
HERE = Path(__file__).parent
MAX_BODY = intake.MAX_BYTES + 256 * 1024  # Caddy lets 9 MB through for these paths
MAX_FIELD = 4 * 1024
MAX_LINKED = 200  # changes the hover links on a page


def link_css(limit: int = MAX_LINKED) -> str:
    """Hovering a change lights its fragments in the draft, and the other way round."""
    rules = [
        f'.oc:has(.it[data-c="{n}"]:hover,.it[data-c="{n}"]:focus-within) .src [data-c="{n}"],'
        f'.oc:has(.src [data-c="{n}"]:hover) .it[data-c="{n}"]'
        "{background:var(--hl);box-shadow:inset 3px 0 0 var(--hl-edge)}"
        for n in range(1, limit + 1)
    ]
    return "\n".join(rules) + "\n"


LINK_CSS = link_css()


def quality() -> dict[str, Any] | None:
    try:
        data = json.loads(resources.files("alnuzum_contracts").joinpath("quality.json").read_text())
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def dollars(value: str, lang: str) -> str:
    """The cost of a run as a reader writes it: four decimals at most, no trailing zeros, a decimal comma
    in Russian (0,0425), a point in English (0.0425)."""
    try:
        shown = f"{Decimal(str(value)).quantize(Decimal('0.0001')):f}".rstrip("0").rstrip(".") or "0"
    except ArithmeticError:
        shown = "0"
    return shown.replace(".", ",") if lang == "ru" else shown


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


def _body(request: Request, lang: str) -> None:
    declared = request.headers.get("content-length", "")
    if not declared.isdigit() or int(declared) > MAX_BODY:
        raise PageError(413, "e_size_t", "e_size", lang)


async def _file(form: Any, name: str = "file") -> bytes | None:
    upload = form.get(name)
    if isinstance(upload, UploadFile) and upload.filename:
        data = await upload.read(intake.MAX_BYTES + 1)
        await upload.close()
        return data
    return None


def build(kit: Kit, contracts: Contracts) -> APIRouter:
    router = APIRouter()

    @router.get("/contracts/link.css")
    async def css() -> Response:
        return Response(LINK_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})

    async def target(request: Request, lang: str, value: str) -> tuple[ConnectionInfo, bool]:
        st = kit.store_of(request)
        if value.startswith("demo:"):
            ids = await contracts.demo_ids(request.app)
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
        owner = kit.owner(sid)
        st = kit.store_of(request)
        own = [c for c in await st.list(owner) if c.status == "active"]
        cs = contracts.store(request.app, kit)
        standards = await cs.standards(owner) if own else {}
        return kit.page(
            request,
            "contracts.html",
            lang,
            status,
            own=own,
            standards=standards,
            demos=sorted(await contracts.demo_ids(request.app)),
            demo_standards=demo_standards(),
            samples=list(samples().values()),
            sides=SIDES,
            q=quality(),
            **ctx,
        )

    @router.get("/{lang}/contracts")
    async def index(request: Request, lang: str) -> HTMLResponse:
        return await start(request, _lang(lang))

    @router.post("/{lang}/contracts")
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
        cs = contracts.store(request.app, kit)
        # which standard: a demo's own for a demo CRM, the connection's for an own CRM
        choice = str(form.get("standard") or "")
        standard: Standard
        try:
            if demo:
                if not choice.startswith("demo:"):
                    return await start(request, lang, status=400, error="e_standard")
                standard = await contracts.demo_standard(choice[5:])
            else:
                mine = (await cs.standards(owner)).get(info.id)
                if mine is None:
                    return await start(request, lang, status=400, error="e_no_standard")
                standard = await contracts.own_standard(cs, mine.id, owner)
        except (ContractError, NotFound):
            return await start(request, lang, status=400, error="e_standard")
        # only what costs nothing is checked before the quota: which input and its size
        sample = str(form.get("sample") or "")
        data = await _file(form)
        if data is None and sample:
            if sample not in samples() or not demo or samples()[sample]["standard"] != standard.key[5:]:
                return await start(request, lang, status=400, error="e_sample")
            data = sample_bytes(samples()[sample]["file"])
        if not data:
            return await start(request, lang, status=400, error="e_empty")
        if len(data) > intake.MAX_BYTES:
            return await start(request, lang, status=413, error="e_size")
        try:
            run_id = await cs.reserve_run(
                owner=owner,
                ip_key=ip_key(request),
                connection_id=info.id,
                crm=info.crm.value,
                demo=demo,
                standard=standard.key,
                limits=contracts.settings.limits(),
            )
        except RunsExceeded:
            return await start(request, lang, status=429, error="e_runs")
        try:
            await contracts.read(cs, run_id=run_id, owner=owner, standard=standard, data=data, lang=lang)
        except ContractError as exc:
            return await start(request, lang, status=422, error=exc.code, error_n=exc.n)
        return redirect(f"/{lang}/contracts/{run_id}")

    @router.post("/{lang}/contracts/standard/{conn_id}")
    async def standard(request: Request, lang: str, conn_id: str) -> Response:
        lang = _lang(lang)
        _body(request, lang)
        try:
            form = await request.form(max_files=1, max_fields=6, max_part_size=MAX_FIELD)
        except HTTPException:
            return await start(request, lang, status=413, error="e_size")
        sid = kit.need_csrf(request, form, lang)
        owner = kit.owner(sid)
        info = await kit.own_connection(request, lang, _uuid(conn_id, lang))
        cs = contracts.store(request.app, kit)
        if form.get("action") == "delete":
            await cs.delete_standard(owner, info.id)
            return redirect(f"/{lang}/contracts?n=standard_deleted")
        if info.status != "active":  # a standard lives while its connection is active
            raise PageError(409, "e_404_t", "e_reconnect", lang)
        if str(form.get("side") or "") not in SIDES:  # checked before the quota: a wrong field costs nothing
            return await start(request, lang, status=400, error="e_side")
        data = await _file(form)
        if not data:
            return await start(request, lang, status=400, error="e_empty")
        if len(data) > intake.MAX_BYTES:
            return await start(request, lang, status=413, error="e_size")
        try:
            # counted like a run: reading a standard may ask the model (a scan)
            run_id = await cs.reserve_run(
                owner=owner,
                ip_key=ip_key(request),
                connection_id=info.id,
                crm=info.crm.value,
                demo=False,
                standard="upload",
                limits=contracts.settings.limits(),
            )
        except RunsExceeded:
            return await start(request, lang, status=429, error="e_runs")
        try:
            await contracts.put_standard(
                cs, owner=owner, connection_id=info.id, side=str(form.get("side") or ""), data=data
            )
        except (ContractError, FileError, ModelError) as exc:
            await cs.reject_run(run_id, exc.code)
            return await start(request, lang, status=422, error=exc.code, error_n=getattr(exc, "n", None))
        await cs.reject_run(run_id, "standard")  # the attempt stays counted; there is no page of it
        return redirect(f"/{lang}/contracts?n=standard_saved")

    async def own_run(request: Request, lang: str, run_id: str) -> RunInfo:
        sid = kit.need_session(request, lang)
        try:
            return await contracts.store(request.app, kit).get_run(_uuid(run_id, lang), kit.owner(sid))
        except NotFound:
            raise PageError(404, "e_404_t", "e_404", lang) from None

    def show(
        request: Request, lang: str, run: RunInfo, *, status: int = 200, notice: str | None = None
    ) -> HTMLResponse:
        if run.result is None or run.status not in ("ready", "writing", "written", "failed"):
            raise PageError(404, "e_404_t", "e_404", lang)
        cmp = comparison(run.result)
        items = []
        for c in ordered(cmp):
            was, now = quotes(cmp, c)
            items.append({"c": c, "name": clause_name(cmp, c), "was": was, "now": now})
        return kit.page(
            request,
            "contracts_run.html",
            lang,
            status,
            run=run,
            r=run.result,
            s=run.summary or {},
            items=items,
            rows=rows(cmp),
            topics=TOPICS[lang],
            risks=RISKS[lang],
            kinds=KINDS[lang],
            until=(run.created_at + KEEP).astimezone(TZ).strftime("%d.%m.%Y %H:%M"),
            cost=dollars(run.result.get("usage", {}).get("usd", "0"), lang),
            notice=notice,
            linked=MAX_LINKED,
        )

    @router.get("/{lang}/contracts/{run_id}")
    async def view(request: Request, lang: str, run_id: str) -> HTMLResponse:
        lang = _lang(lang)
        n = request.query_params.get("n")
        return show(
            request,
            lang,
            await own_run(request, lang, run_id),
            notice=n if n and n.replace("_", "").isalpha() else None,
        )

    @router.post("/{lang}/contracts/{run_id}")
    async def decide(request: Request, lang: str, run_id: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=8, max_part_size=1024)
        sid = kit.need_csrf(request, form, lang)
        run = await own_run(request, lang, run_id)
        back = f"/{lang}/contracts/{run.id}"
        cs = contracts.store(request.app, kit)
        action = form.get("action")
        if action == "erase":
            if await cs.erase(run.id, run.owner) == "pending":  # being written: erased when it ends
                return redirect(f"{back}?n=erase_pending")
            return redirect(f"/{lang}/contracts?n=erased")
        if run.status not in ("ready", "failed") or (run.steps and action != "write"):
            return redirect(back)
        if action == "search":
            notice = await contracts.search(
                request.app, kit, run, conn_owner=kit.owner(sid), query=str(form.get("q") or "")
            )
            return redirect(f"{back}?n={notice}")
        if action == "write":
            if not run.steps:
                chosen = str(form.get("deal") or "")
                found = next((c for c in run.candidates if str(c.get("id")) == chosen), None)
                if found is None:
                    return redirect(f"{back}?n=no_deal")
                await cs.choose_deal(run.id, run.owner, chosen, str(found.get("title") or ""))
            fresh = await cs.get_run(run.id, run.owner)
            notice = await contracts.confirm(
                request.app, kit, fresh, conn_owner=kit.owner(sid), ip_key=ip_key(request), lang=lang
            )
            return redirect(f"{back}?n={notice}")
        return redirect(back)

    return router


class _Solution:
    contracts = Contracts()


def _store(app: FastAPI) -> ContractStore | None:
    maker = getattr(app.state, "sessionmaker", None)
    conf = getattr(getattr(app.state, "connectors", None), "settings", None)
    keys = getattr(conf, "secret_keys", None)
    if maker is None or keys is None or not keys.get_secret_value():
        return None
    from alnuzum_platform.crypto import Keyring

    return ContractStore(maker, Seal(Keyring.from_env(keys.get_secret_value())))


async def _startup(app: FastAPI) -> None:
    """The runs a stopped process left "writing" or "reading" are released."""
    if (cs := _store(app)) is None:
        return
    written, read = await cs.release_stuck()
    if written or read:
        log.warning("contracts: %d runs left writing and %d left reading released", written, read)


async def _sweep(app: FastAPI) -> None:
    await _startup(app)
    if (cs := _store(app)) is None:
        return
    runs, standards = await cs.purge()
    log.info("contracts sweep: %d runs and %d standards erased", runs, standards)


def make_solution(contracts: Contracts) -> Solution:
    _Solution.contracts = contracts
    return Solution(
        slug="contracts",
        titles={"ru": "Сверка договора с типовым", "en": "A contract against your standard"},
        templates=HERE / "templates",
        static=HERE / "static",
        texts=texts,
        router=lambda kit: build(kit, contracts),
        startup=_startup,
        sweep=_sweep,
    )


solution = make_solution(Contracts())

__all__ = ["make_solution", "solution"]
