"""Pages of the solution: a CRM chosen and scanned, the clients who missed their cycle, the screen of one
client (its purchase history beside the reasons) and the task on it. The pages run no script; hovering a
reason lights its orders through CSS only."""

import logging
import re
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from alnuzum_crm.errors import CrmError
from alnuzum_platform.solutions import Kit, Solution
from alnuzum_platform.store import ConnectionDisabled, ConnectionInfo, ConnectionNotFound
from alnuzum_platform.web.routes import PageError, redirect
from alnuzum_platform.web.security import RateLimiter
from alnuzum_winback.i18n import texts
from alnuzum_winback.rule import TZ, due
from alnuzum_winback.service import MAX_TEXT, Winback, WinbackError, crm_error
from alnuzum_winback.store import NotFound, ScanInfo, ScansExceeded, WinbackStore

log = logging.getLogger("alnuzum_winback.web")
HERE = Path(__file__).parent
NOTICES = frozenset(
    ("task_set", "standing", "busy", "quota", "full", "down", "auth", "unavailable", "failed", "gone", "text")
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


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


def money(value: str | Decimal | None, currency: str | None, lang: str) -> str:
    """12 345,5 UZS in Russian, 12,345.5 UZS in English; whole sums without decimals."""
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        amount = Decimal(0)
    whole = amount == amount.to_integral_value()
    shown = f"{amount:,.0f}" if whole else f"{amount:,.2f}"
    if lang == "ru":
        shown = shown.replace(",", " ").replace(".", ",")
    return f"{shown} {currency}" if currency else shown


def day_of(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y") if value else ""


def decimal_of(value: Any, lang: str) -> str:
    return str(value).replace(".", ",") if lang == "ru" else str(value)


def moment(value: datetime | None) -> str:
    """A due in Tashkent; a CRM that keeps only a date (Odoo) gives midnight UTC: the date alone."""
    if value is None:
        return ""
    if value.utcoffset() == timedelta(0) and value.time() == time(0):
        return value.strftime("%d.%m.%Y")
    return value.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def reason_text(t: dict[str, Any], r: dict[str, Any], currency: str | None, lang: str) -> str:
    d = dict(r["data"])
    for k in ("last", "next"):
        if d.get(k):
            d[k] = day_of(d[k])
    for k in ("amount", "usual"):
        if k in d:
            d[k] = money(d[k], currency, lang)
    if "cycles" in d:
        d["cycles"] = decimal_of(d["cycles"], lang)
    template = t.get(f"w_r_{r['kind']}")
    return template.format(**d) if template else ""


def build(kit: Kit, winback: Winback) -> APIRouter:
    router = APIRouter()

    def store_of(request: Request, lang: str) -> WinbackStore:
        try:
            return winback.store(request.app)
        except WinbackError:  # no database or no keys: a page that says so, not a 500
            raise PageError(503, "e_unreachable", "e_unreachable", lang) from None

    async def target(request: Request, lang: str, value: str) -> tuple[ConnectionInfo, bool]:
        st = kit.store_of(request)
        if value.startswith("demo:"):
            ids = await winback.demo_ids(request.app)
            if value[5:] not in ids:
                raise PageError(404, "e_404_t", "e_demo_off", lang)
            try:  # the demo connection may be gone since the list was read
                return await st.get(ids[value[5:]], "demo"), True
            except ConnectionNotFound:
                raise PageError(404, "e_404_t", "e_demo_off", lang) from None
        if value.startswith("conn:"):
            info = await kit.own_connection(request, lang, _uuid(value[5:], lang))
            if info.status != "active":
                raise PageError(409, "e_404_t", "e_reconnect", lang)
            return info, False
        raise PageError(404, "e_404_t", "e_404", lang)

    async def start(request: Request, lang: str, *, status: int = 200, **ctx: Any) -> HTMLResponse:
        sid = kit.ensure_session(request)
        own = [c for c in await kit.store_of(request).list(kit.owner(sid)) if c.status == "active"]
        return kit.page(
            request,
            "winback.html",
            lang,
            status,
            own=own,
            demos=sorted(await winback.demo_ids(request.app)),
            **ctx,
        )

    @router.get("/{lang}/winback")
    async def index(request: Request, lang: str) -> HTMLResponse:
        return await start(request, _lang(lang))

    @router.post("/{lang}/winback")
    async def submit(request: Request, lang: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=4, max_part_size=1024)
        sid = kit.need_csrf(request, form, lang)
        info, demo = await target(request, lang, str(form.get("target") or ""))
        ws = store_of(request, lang)
        try:
            scan_id = await ws.reserve_scan(
                owner=kit.owner(sid),
                ip_key=ip_key(request),
                connection_id=info.id,
                crm=info.crm.value,
                demo=demo,
                limits=winback.settings.limits(),
                now=winback.now(),
            )
        except ScansExceeded:
            return await start(request, lang, status=429, error="e_scans")
        try:
            await winback.scan(request.app, scan_id=scan_id, info=info, demo=demo)
        except WinbackError as exc:
            return await start(request, lang, status=422, error=exc.code)
        return redirect(f"/{lang}/winback/{scan_id}")

    async def own_scan(request: Request, lang: str, scan_id: str) -> ScanInfo:
        sid = kit.need_session(request, lang)
        try:
            return await store_of(request, lang).get_scan(_uuid(scan_id, lang), kit.owner(sid), winback.now())
        except NotFound:
            raise PageError(404, "e_404_t", "e_404", lang) from None

    def number(scan: ScanInfo, n: str, lang: str) -> int:
        if not n.isdigit() or not 1 <= int(n) <= len(scan.result.get("clients", [])):
            raise PageError(404, "e_404_t", "e_404", lang)
        return int(n)

    @router.get("/{lang}/winback/{scan_id}")
    async def listing(request: Request, lang: str, scan_id: str) -> HTMLResponse:
        lang = _lang(lang)
        scan = await own_scan(request, lang, scan_id)
        r = scan.result
        cur = r.get("currency")
        rows = [
            c
            | {
                "last_shown": day_of(c["last"]),
                "lost_shown": money(c["lost"], cur, lang),
                "average_shown": money(c["average"], cur, lang),
            }
            for c in r.get("clients", [])
        ]
        return kit.page(request, "winback_list.html", lang, scan=scan, r=r, rows=rows)

    async def screen(
        request: Request, lang: str, scan: ScanInfo, n: int, *, status: int = 200, notice: str | None = None
    ) -> HTMLResponse:
        sid = kit.need_session(request, lang)
        t = kit.texts(lang)
        row = scan.result["clients"][n - 1]
        error, shown, standing = None, None, []
        try:
            shown = await winback.client(request.app, scan, n, conn_owner=kit.owner(sid))
            standing = await winback.standing(request.app, scan, n, conn_owner=kit.owner(sid))
        except CrmError as exc:
            error = f"e_crm_{crm_error(exc)}"
        except (ConnectionDisabled, ConnectionNotFound):
            error = "e_reconnect"
        except WinbackError as exc:
            error = exc.code
        cur = (shown or {}).get("currency") or scan.result.get("currency")
        orders = [
            o
            | {
                "day_shown": day_of(o["day"]),
                "amount_shown": money(o["amount"], cur, lang),
                "lines_shown": [x | {"total_shown": money(x["total"], cur, lang)} for x in o["lines"]],
            }
            for o in (shown or {}).get("orders", [])
        ]
        reasons = [r | {"text": reason_text(t, r, cur, lang)} for r in (shown or {}).get("reasons", [])]
        why = reasons[0]["text"] if reasons else ""
        mine = await store_of(request, lang).last_task(scan.id, scan.owner, row["key"])
        return kit.page(
            request,
            "winback_client.html",
            lang,
            status,
            scan=scan,
            n=n,
            row=row,
            last_shown=day_of(row.get("last")),
            orders=orders,
            reasons=reasons,
            standing=standing,
            mine=mine,
            mine_due=moment(mine.due) if mine else "",
            due=moment(due(winback.now())),
            text=t["w_task_text"].format(name=row.get("name", ""), why=why)[:MAX_TEXT],
            notice=notice,
            error=error,
            moment=moment,
        )

    @router.get("/{lang}/winback/{scan_id}/{n}")
    async def client(request: Request, lang: str, scan_id: str, n: str) -> HTMLResponse:
        lang = _lang(lang)
        scan = await own_scan(request, lang, scan_id)
        notice = request.query_params.get("n")
        return await screen(
            request, lang, scan, number(scan, n, lang), notice=notice if notice in NOTICES else None
        )

    @router.post("/{lang}/winback/{scan_id}/{n}")
    async def task(request: Request, lang: str, scan_id: str, n: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=4, max_part_size=4096)
        sid = kit.need_csrf(request, form, lang)
        scan = await own_scan(request, lang, scan_id)
        k = number(scan, n, lang)
        back = f"/{lang}/winback/{scan.id}/{k}"
        if form.get("action") != "task":
            return redirect(back)
        text = " ".join(_CONTROL.sub(" ", str(form.get("text") or "")).split())[:MAX_TEXT]
        if not text:
            return redirect(f"{back}?n=text")
        try:
            notice = await winback.set_task(
                request.app,
                scan,
                k,
                text,
                conn_owner=kit.owner(sid),
                ip_key=ip_key(request),
                quota=kit.quota,
            )
        except WinbackError:
            raise PageError(503, "e_unreachable", "e_unreachable", lang) from None
        return redirect(f"{back}?n={notice}")

    return router


class _Solution:
    winback = Winback()


async def _startup(app: FastAPI) -> None:
    """The scans and tasks a stopped process left "reading" or "writing" are released."""
    try:
        ws = _Solution.winback.store(app)
    except WinbackError:
        return
    scans, tasks = await ws.release_stuck()
    if scans or tasks:
        log.warning("win-back: %d scans left reading and %d tasks left writing released", scans, tasks)


async def _sweep(app: FastAPI) -> None:
    await _startup(app)
    try:
        ws = _Solution.winback.store(app)
    except WinbackError:
        return
    scans, tasks = await ws.purge()
    closed = await _Solution.winback.close_stale(app)
    made = await _Solution.winback.seed(app)
    log.info(
        "win-back sweep: %d scans and %d journal rows erased, %d demo tasks closed, demo topped up %s",
        scans,
        tasks,
        closed,
        made or "-",
    )


def make_solution(winback: Winback) -> Solution:
    _Solution.winback = winback
    return Solution(
        slug="winback",
        titles={"ru": "Возврат ушедших клиентов", "en": "Win back lapsed clients"},
        templates=HERE / "templates",
        static=HERE / "static",
        texts=texts,
        router=lambda kit: build(kit, winback),
        startup=_startup,
        sweep=_sweep,
    )


solution = make_solution(Winback())

__all__ = ["make_solution", "solution"]
