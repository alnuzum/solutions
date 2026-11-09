"""Pages of the solution: an order in, a deal with products out.

Who may do what (the platform's rules): pages that show a run need the session cookie (401 without it)
and show only the session's own runs and connections (404 for anybody else's); every POST needs the
session's CSRF token and our Origin (403). The demo writes into Al-Nuzum's own demo accounts, which
nobody outside can open: the page shows the deal as the CRM returns it through the API.
"""

import ipaddress
import json
import logging
import threading
import uuid
from collections.abc import Callable
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

import anyio
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.errors import CrmAuthError, CrmError, CrmRateLimited, CrmUnavailable
from alnuzum_crm.oauth import ZOHO_ACCOUNTS_HOSTS, ZOHO_API_HOSTS, zoho_host
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.types import CrmKind
from alnuzum_crm.zoho import ZohoCRM
from alnuzum_orders import intake
from alnuzum_orders.catalog import Catalog, CatalogCache, read_catalog
from alnuzum_orders.demo_catalog import PREFIX
from alnuzum_orders.engine import Result, apply_choices, clean_customer, plan, priced, run
from alnuzum_orders.i18n import texts
from alnuzum_orders.model import ClaudeReader, Gate, ModelError, Reader, Usage
from alnuzum_orders.pricing import Rules, RulesError, demo_rules, parse_rules
from alnuzum_orders.store import DEMO_OWNER, CeilingReached, DemoLimits, OrderStore, RunNotFound, RunsExceeded
from alnuzum_orders.writer import write
from alnuzum_platform.solutions import Kit, Solution
from alnuzum_platform.store import ConnectionDisabled, ConnectionInfo, ConnectionStore, QuotaExceeded
from alnuzum_platform.web.routes import PageError, redirect
from alnuzum_platform.web.security import RateLimiter

log = logging.getLogger("alnuzum_orders.web")

HERE = Path(__file__).parent
CRMS = tuple(k.value for k in CrmKind)
MAX_BODY = intake.MAX_BYTES + 256 * 1024
MAX_LINKED_LINES = 400
# CRMs whose deal lines keep their own price; the rest get catalog prices and the total as the budget
LINE_PRICES = frozenset(
    c.kind for c in (AmoCRM, Kommo, Bitrix24, ZohoCRM, Odoo) if "line_prices" in c.capabilities
)
# uploads stay in memory: a stranger's file is never spooled to disk
MultiPartParser.spool_max_size = MAX_BODY


class OrderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    anthropic_api_key: SecretStr = SecretStr("")
    orders_model: str = "claude-opus-5-5"
    # the money ceiling of the whole solution per 24 hours: dollars (each call booked at its worst cost
    # until it has cost what it cost), and model calls as a second bound
    orders_model_usd_per_day: Decimal = Decimal("1.5")
    orders_model_calls_per_day: int = 200
    orders_runs_per_session: int = 10
    orders_runs_per_ip: int = 20
    # deals the demo accounts take per 24 hours: from a session, from an address, into one demo CRM (apart
    # from the platform's quota of a connection); Kommo lower, and bounded in all: its technical account
    # keeps at most 100 deals, 30 were there on 2026-11-09 and the E2E suite needs room
    orders_demo_writes_per_session: int = 5
    orders_demo_writes_per_ip: int = 8
    orders_demo_writes_per_crm: int = 40
    orders_demo_writes_kommo: int = 5
    orders_demo_kommo_total: int = 40
    # reading a file (pictures, PDF, spreadsheets) runs in threads: at most this many at a time, this long
    orders_parse_slots: int = 2
    orders_parse_seconds: float = 20.0

    demo_amocrm_domain: str = ""
    demo_amocrm_token: SecretStr = SecretStr("")
    demo_kommo_domain: str = ""
    demo_kommo_token: SecretStr = SecretStr("")
    demo_b24_webhook: SecretStr = SecretStr("")
    demo_zoho_refresh_token: SecretStr = SecretStr("")
    demo_odoo_url: str = ""
    demo_odoo_db: str = ""
    demo_odoo_login: str = ""
    demo_odoo_key: SecretStr = SecretStr("")

    def demo_secrets(self) -> dict[CrmKind, tuple[str, dict[str, str]]]:
        out: dict[CrmKind, tuple[str, dict[str, str]]] = {}
        if self.demo_amocrm_domain and self.demo_amocrm_token.get_secret_value():
            out[CrmKind.AMOCRM] = (
                self.demo_amocrm_domain,
                {"access_token": self.demo_amocrm_token.get_secret_value()},
            )
        if self.demo_kommo_domain and self.demo_kommo_token.get_secret_value():
            out[CrmKind.KOMMO] = (
                self.demo_kommo_domain,
                {"access_token": self.demo_kommo_token.get_secret_value()},
            )
        if hook := self.demo_b24_webhook.get_secret_value():
            out[CrmKind.BITRIX24] = ("", {"webhook_url": hook})
        if refresh := self.demo_zoho_refresh_token.get_secret_value():
            out[CrmKind.ZOHO] = (
                zoho_host("https://www.zohoapis.com", ZOHO_API_HOSTS),
                {
                    "access_token": "expired",  # nosec B105: a placeholder, refreshed on first use
                    "refresh_token": refresh,
                    "expires_at": "2000-01-01T00:00:00+00:00",
                    "accounts_server": zoho_host("https://accounts.zoho.com", ZOHO_ACCOUNTS_HOSTS),
                },
            )
        if self.demo_odoo_url and self.demo_odoo_key.get_secret_value():
            out[CrmKind.ODOO] = (
                self.demo_odoo_url,
                {
                    "database": self.demo_odoo_db,
                    "login": self.demo_odoo_login,
                    "api_key": self.demo_odoo_key.get_secret_value(),
                },
            )
        return out

    def demo_limits(self, crm: str) -> DemoLimits:
        if crm == CrmKind.KOMMO.value:
            return DemoLimits(
                self.orders_demo_writes_per_session,
                self.orders_demo_writes_per_ip,
                self.orders_demo_writes_kommo,
                self.orders_demo_kommo_total,
            )
        return DemoLimits(
            self.orders_demo_writes_per_session,
            self.orders_demo_writes_per_ip,
            self.orders_demo_writes_per_crm,
        )


def link_css(limit: int = MAX_LINKED_LINES) -> str:
    """Hovering an item lights its lines in the document, and the other way round: plain CSS, the
    pages run no script."""
    rules = []
    for n in range(1, limit + 1):
        rules.append(
            f'.oc:has(.it[data-s~="{n}"]:hover,.it[data-s~="{n}"]:focus-within) .src [data-n="{n}"],'
            f'.oc:has(.src [data-n="{n}"]:hover) .it[data-s~="{n}"]'
            "{background:var(--hl);box-shadow:inset 3px 0 0 var(--hl-edge)}"
        )
    return "\n".join(rules) + "\n"


LINK_CSS = link_css()


def ip_key(request: Request) -> str:
    ip = request.client.host if request.client else "?"
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    return RateLimiter.client(ip)[:64]


def samples() -> dict[str, str]:
    """Sample orders offered on the page: file name -> label key."""
    return {
        "photo-list.jpg": "s_photo",
        "order-ru.xlsx": "s_xlsx",
        "zayavka-418.pdf": "s_pdf",
        "email-ru.txt": "s_email",
        "request-en.txt": "s_email_en",
        "messy-ru.xlsx": "s_messy",
    }


def quality() -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = json.loads((HERE / "quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


T = TypeVar("T")


class _Busy(Exception):
    pass


class Parsing:
    """Reading a file (PIL, pypdf, openpyxl) takes seconds of CPU: it runs in a thread, so the event loop
    keeps answering everybody else, the CRM hooks and /health. At most ``slots`` files are read at a
    time (the container has 512 MB), each within ``seconds``. A thread cannot be stopped: one that runs
    over its time is left to end and keeps its slot until it does, so no more than ``slots`` are ever
    being read; the slot is taken inside the thread, so a job dropped before it started holds none."""

    def __init__(self, slots: int, seconds: float) -> None:
        self.slots, self.seconds = slots, seconds
        self._lock = threading.Lock()
        self.busy = 0

    def _take(self) -> bool:
        with self._lock:
            if self.busy >= self.slots:
                return False
            self.busy += 1
            return True

    def _give(self) -> None:
        with self._lock:
            self.busy -= 1

    async def read(self, fn: Callable[..., T], *args: Any) -> T:
        started = False

        def work() -> T:
            nonlocal started
            if not self._take():
                raise _Busy
            started = True
            try:
                return fn(*args)
            finally:
                self._give()

        try:
            with anyio.fail_after(self.seconds):
                while True:
                    try:
                        return await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
                    except _Busy:
                        await anyio.sleep(0.05)
        except TimeoutError:
            raise intake.IntakeError("e_slow" if started else "e_busy") from None


class Orders:
    def __init__(self, settings: OrderSettings | None = None, reader: Reader | None = None) -> None:
        self.settings = settings or OrderSettings()
        self._reader = reader
        self.catalogs = CatalogCache()
        self.parsing = Parsing(self.settings.orders_parse_slots, self.settings.orders_parse_seconds)

    def reader(self) -> Reader | None:
        if self._reader is not None:
            return self._reader
        key = self.settings.anthropic_api_key.get_secret_value()
        if not key:
            return None
        self._reader = ClaudeReader(key, model=self.settings.orders_model)
        return self._reader

    @staticmethod
    def store(request: Request) -> OrderStore:
        maker = getattr(request.app.state, "sessionmaker", None)
        if maker is None:
            raise PageError(503, "e_unreachable", "e_unreachable")
        return OrderStore(maker)

    async def startup(self, app: FastAPI) -> None:
        """The demo accounts become connections of the owner ``demo`` (encrypted like any other)."""
        store: ConnectionStore | None = app.state.store
        app.state.orders_demo = {}
        if store is None:
            return
        have = {c.crm: c for c in await store.list(DEMO_OWNER) if c.status == "active"}
        for crm, (endpoint, secrets) in self.settings.demo_secrets().items():
            current = have.get(crm)
            if current is not None:
                kept = await store.secrets(current.id)
                key = next(
                    k for k in ("access_token", "webhook_url", "refresh_token", "api_key") if k in secrets
                )
                if crm is CrmKind.ZOHO or kept.get(key) == secrets[key]:
                    app.state.orders_demo[crm.value] = current.id
                    continue
            try:
                info = await store.create(
                    DEMO_OWNER,
                    crm,
                    endpoint or "demo",
                    secrets,
                    label=f"demo {crm.value}",
                    account=f"demo:{crm.value}",
                )
            except (ValueError, OSError) as exc:
                log.warning("demo %s not set up: %s", crm.value, type(exc).__name__)
                continue
            app.state.orders_demo[crm.value] = info.id
        await store.touch(DEMO_OWNER)
        log.info("demo accounts: %s", ", ".join(sorted(app.state.orders_demo)) or "none")

    async def sweep(self, app: FastAPI) -> None:
        maker = getattr(app.state, "sessionmaker", None)
        if maker is not None:
            runs, calls = await OrderStore(maker).purge()
            log.info("orders sweep: %d runs and %d old model calls erased", runs, calls)
        if app.state.store is not None:
            await app.state.store.touch(DEMO_OWNER)

    # --- helpers ---

    async def target(self, kit: Kit, request: Request, lang: str, value: str) -> tuple[ConnectionInfo, bool]:
        """The connection an order goes to: a demo account or one of the session's own."""
        st = kit.store_of(request)
        if value.startswith("demo:"):
            conn_id = getattr(request.app.state, "orders_demo", {}).get(value[5:])
            if conn_id is None:
                raise PageError(404, "e_404_t", "e_demo_off", lang)
            return await st.get(conn_id, DEMO_OWNER), True
        if value.startswith("conn:"):
            try:
                conn_id = uuid.UUID(value[5:])
            except ValueError:
                raise PageError(404, "e_404_t", "e_404", lang) from None
            info = await kit.own_connection(request, lang, conn_id)
            if info.status != "active":
                raise PageError(409, "e_404_t", "e_reconnect", lang)
            return info, False
        raise PageError(404, "e_404_t", "e_404", lang)

    async def catalog(self, kit: Kit, request: Request, info: ConnectionInfo, demo: bool) -> Catalog:
        key = str(info.id)
        if hit := self.catalogs.get(key):
            return hit
        st = kit.store_of(request)
        secrets = await st.secrets(info.id)
        async with kit.connectors.adapter(info, secrets, st) as crm:
            catalog = await read_catalog(crm, prefix=PREFIX if demo else None)
        self.catalogs.put(key, catalog)
        return catalog

    async def rules(self, request: Request, owner: str, target: str, currency: str | None) -> Rules:
        stored = await self.store(request).get_rules(owner, target)
        if stored is None:
            return demo_rules(currency)
        try:
            return parse_rules(stored)
        except RulesError:
            return demo_rules(currency)

    def gate(self, request: Request, owner: str) -> Gate:
        store, ceiling, key = self.store(request), self.settings.orders_model_calls_per_day, ip_key(request)
        budget = self.settings.orders_model_usd_per_day

        async def gate(kind: str, worst: Decimal):  # type: ignore[no-untyped-def]
            try:
                call_id = await store.reserve_call(owner, key, kind, ceiling, budget=budget, worst=worst)
            except CeilingReached:
                raise ModelError("e_ceiling") from None

            async def done(used: Usage | None, ok: bool) -> None:
                if used is None:
                    await store.finish_call(call_id, 0, 0, None, ok)
                else:
                    await store.finish_call(call_id, used.input_tokens, used.output_tokens, used.usd, ok)

            return done

        return gate


def _run_id(value: str, lang: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise PageError(404, "e_404_t", "e_404", lang) from None


def _lang(value: str) -> str:
    if value not in ("ru", "en"):
        raise PageError(404, "e_404_t", "e_404")
    return {"ru": "ru", "en": "en"}[value]


def _crm_error(exc: CrmError) -> str:
    if isinstance(exc, CrmAuthError):
        return "auth"
    if isinstance(exc, CrmUnavailable | CrmRateLimited):
        return "unavailable"
    return (exc.code or type(exc).__name__)[:40]


def build(kit: Kit, orders: Orders) -> APIRouter:
    router = APIRouter()
    s = orders.settings

    @router.get("/orders/link.css")
    async def css() -> Response:
        return Response(LINK_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=86400"})

    @router.get("/orders/sample/{name}")
    async def sample_file(name: str) -> Response:
        """Only the sample photo is shown next to what the system read; an uploaded one is never kept."""
        if name not in samples() or not name.endswith(".jpg"):
            raise PageError(404, "e_404_t", "e_404")
        data = (resources.files("alnuzum_orders") / "samples" / name).read_bytes()
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    async def start(request: Request, lang: str, *, status: int = 200, **ctx: Any) -> HTMLResponse:
        sid = kit.ensure_session(request)
        owner = kit.owner(sid)
        st = kit.store_of(request)
        await st.touch(owner)
        conns = [c for c in await st.list(owner) if c.status == "active"]
        demo = sorted(getattr(request.app.state, "orders_demo", {}), key=CRMS.index)
        used = await orders.store(request).runs_today(owner=owner)
        full = await orders.store(request).demo_full({k: s.demo_limits(k) for k in demo})
        return kit.page(
            request,
            "orders.html",
            lang,
            status,
            conns=conns,
            demo=demo,
            demo_full=full,
            samples=samples(),
            quality=quality(),
            left=max(0, s.orders_runs_per_session - used),
            model_on=orders.reader() is not None,
            **ctx,
        )

    @router.get("/{lang}/orders")
    async def index(request: Request, lang: str) -> HTMLResponse:
        return await start(request, _lang(lang))

    @router.post("/{lang}/orders")
    async def submit(request: Request, lang: str) -> Response:
        lang = _lang(lang)
        declared = request.headers.get("content-length", "")
        if not declared.isdigit() or int(declared) > MAX_BODY:
            raise PageError(413, "e_size_t", "e_size", lang)
        form = await request.form(max_files=1, max_fields=10, max_part_size=64 * 1024)
        sid = kit.need_csrf(request, form, lang)
        owner = kit.owner(sid)
        target = str(form.get("target") or "")
        info, demo = await orders.target(kit, request, lang, target)
        ost = orders.store(request)
        reader = orders.reader()
        # only what costs nothing is checked before the quota: which input, and its size
        sample = str(form.get("sample") or "")
        upload = form.get("file")
        typed = str(form.get("text") or "")
        name, data = "text", b""
        if sample:
            if sample not in samples():
                return await start(request, lang, status=400, error="e_type", target=target)
            name, data = sample, (resources.files("alnuzum_orders") / "samples" / sample).read_bytes()
        elif isinstance(upload, UploadFile) and upload.filename:
            name, data = upload.filename[:120], await upload.read(intake.MAX_BYTES + 1)
            await upload.close()
            if not data or len(data) > intake.MAX_BYTES:
                error = "e_size" if data else "e_empty"
                return await start(request, lang, status=400, error=error, target=target)
        elif not typed.strip():
            return await start(request, lang, status=400, error="e_empty", target=target)
        # the run is counted before the file is read or the model asked: a failing, a heavy or a parallel
        # attempt counts too
        try:
            run_id = await ost.reserve_run(
                owner,
                ip_key(request),
                info.id,
                info.crm.value,
                lang,
                demo=demo,
                per_session=s.orders_runs_per_session,
                per_ip=s.orders_runs_per_ip,
            )
        except RunsExceeded:
            return await start(request, lang, status=429, error="e_quota_runs", target=target)

        async def refuse(status: int, error: str) -> HTMLResponse:
            await ost.reject_run(run_id, error)
            return await start(request, lang, status=status, error=error, target=target)

        try:
            if data:
                source = await orders.parsing.read(intake.read, name, data)
            else:
                source = await orders.parsing.read(intake.from_text, "text", typed)
        except intake.IntakeError as exc:
            return await refuse(503 if exc.code == "e_busy" else 400, exc.code)
        finally:
            del data
        if reader is None and not source.columns:
            return await refuse(503, "e_model_off")
        try:
            catalog = await orders.catalog(kit, request, info, demo)
        except CrmError as exc:
            log.info("catalog of %s not read: %s", info.id, _crm_error(exc))
            return await refuse(502, "e_catalog")
        if not catalog.items:
            return await refuse(409, "e_catalog_empty")
        try:
            result = await run(source, catalog, reader or _NoReader(), orders.gate(request, owner))
        except ModelError as exc:
            return await refuse(503, exc.code)
        if not result.lines:
            return await refuse(422, "e_no_lines")
        result.catalog["line_prices"] = info.crm in LINE_PRICES
        if demo and not result.catalog.get("currency"):
            # Zoho gives products no currency; the demo account's own is known
            result.catalog["currency"] = _demo_currency(info)
        result.catalog["target"] = _target_key(info, demo)
        await ost.fill_run(run_id, result.to_json())
        log.info("order run %s: %d lines, %d calls", run_id, len(result.lines), result.usage["calls"])
        return redirect(f"/{lang}/orders/{run_id}")

    async def own_run(request: Request, lang: str, run_id: str) -> tuple[str, Any]:
        sid = kit.need_session(request, lang)
        try:
            return kit.owner(sid), await orders.store(request).get_run(_run_id(run_id, lang), kit.owner(sid))
        except RunNotFound:
            raise PageError(404, "e_404_t", "e_404", lang) from None

    async def show(
        request: Request, lang: str, owner: str, item: Any, *, status: int = 200, **ctx: Any
    ) -> HTMLResponse:
        result = Result.from_json(item.result)
        target = result.catalog.get("target", "")
        rules = await orders.rules(request, owner, target, result.catalog.get("currency"))
        p = priced(result, rules)
        demo_left = None
        full: dict[str, str] = {}
        others: list[str] = []
        if item.demo:
            demo_left = max(
                0, s.orders_demo_writes_per_session - await orders.store(request).writes_today(owner)
            )
            demo = sorted(getattr(request.app.state, "orders_demo", {}), key=CRMS.index)
            full = await orders.store(request).demo_full({k: s.demo_limits(k) for k in demo})
            others = [k for k in demo if k != item.crm and k not in full]
        photo = item.result["source"]["name"] if item.result["source"]["name"] in samples() else None
        marks: dict[int, str] = {}
        for ln in result.lines:
            for n in ln.src:
                marks[n] = "doubt" if ln.status != "ok" or marks.get(n) == "doubt" else "mk"
        return kit.page(
            request,
            "orders_run.html",
            lang,
            status,
            run=item,
            r=result,
            rules=rules,
            p=p,
            by_key={ln.key: ln for ln in p.lines},
            customer=clean_customer(result.customer),
            photo=photo if photo and photo.endswith(".jpg") else None,
            demo_left=demo_left,
            demo_full=full.get(item.crm) if item.status != "written" else None,
            demo_others=others,
            target=target,
            marks=marks,
            **ctx,
        )

    @router.get("/{lang}/orders/{run_id}")
    async def view(request: Request, lang: str, run_id: str) -> HTMLResponse:
        lang = _lang(lang)
        owner, item = await own_run(request, lang, run_id)
        return await show(request, lang, owner, item, notice=request.query_params.get("n", "")[:20])

    @router.post("/{lang}/orders/{run_id}")
    async def decide(request: Request, lang: str, run_id: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=MAX_LINKED_LINES, max_part_size=4096)
        kit.need_csrf(request, form, lang)
        owner, item = await own_run(request, lang, run_id)
        back = f"/{lang}/orders/{item.id}"
        if item.status in ("writing", "written"):
            return redirect(back)
        result = Result.from_json(item.result)
        apply_choices(result, {k: v for k, v in form.items() if isinstance(v, str)})
        ost = orders.store(request)
        await ost.save_result(item.id, owner, result.to_json())
        if form.get("action") != "write":
            return redirect(back)
        if not any(ln.status == "ok" for ln in result.lines):
            return redirect(f"{back}?n=empty")
        # the demo quota is counted and the run marked "writing" in one step, so parallel clicks cannot
        # pass it together
        state = await ost.start_writing(
            item.id,
            owner,
            demo=s.demo_limits(item.crm) if item.demo else None,
            ip_key=ip_key(request),
            crm=item.crm,
        )
        if state in ("quota", "full", "full_total"):
            return redirect(f"{back}?n={state}")
        if state != "ok":
            return redirect(back)
        st = kit.store_of(request)
        conn_owner = DEMO_OWNER if item.demo else owner
        # a demo connection is bounded by the demo limits above; the platform's quota stays a backstop
        quota = max(kit.quota, 2 * s.orders_demo_writes_per_crm) if item.demo else kit.quota
        try:
            info = await st.get(item.connection_id, conn_owner)
            write_id = await st.start_write(info.id, conn_owner, "order_deal", quota)
        except (QuotaExceeded, ConnectionDisabled) as exc:
            await ost.finish_writing(
                item.id, ok=False, error="quota" if isinstance(exc, QuotaExceeded) else "auth"
            )
            return redirect(f"{back}?n={'quota' if isinstance(exc, QuotaExceeded) else 'auth'}")
        rules = await orders.rules(
            request, owner, result.catalog.get("target", ""), result.catalog.get("currency")
        )
        the_plan = plan(result, rules, line_prices=bool(result.catalog.get("line_prices")), lang=lang)
        tags = ("al-nuzum", "demo") if item.demo else ("al-nuzum",)
        try:
            secrets = await st.secrets(info.id)
            async with kit.connectors.adapter(info, secrets, st) as crm:
                done = await write(crm, the_plan, clean_customer(result.customer), lang=lang, tags=tags)
                url = None if item.demo else crm.deal_link(kit.connectors.account_of(info), done.deal_id)
        except CrmError as exc:
            code = _crm_error(exc)
            await st.finish_write(write_id, ok=False, error=code)
            await ost.finish_writing(item.id, ok=False, error=code)
            if code == "auth" and not item.demo:
                await st.mark(info.id, status="reauth_required", error="auth")
            log.info("order run %s not written: %s", item.id, code)
            return redirect(f"{back}?n=failed")
        await st.finish_write(write_id, ok=True, record_id=done.deal_id, record_url=url)
        await ost.finish_writing(item.id, ok=True, deal_id=done.deal_id, deal_url=url, readback=done.readback)
        log.info("order run %s written as deal %s", item.id, done.deal_id)
        return redirect(f"{back}?n=written")

    @router.get("/{lang}/orders/rules/{target}")
    async def rules_page(request: Request, lang: str, target: str) -> HTMLResponse:
        lang = _lang(lang)
        sid = kit.need_session(request, lang)
        info, demo = await orders.target(kit, request, lang, target)
        rules = await orders.rules(
            request, kit.owner(sid), _target_key(info, demo), None if not demo else _demo_currency(info)
        )
        key = _target_key(info, demo)
        return kit.page(request, "orders_rules.html", lang, target=key, info=info, demo=demo, rules=rules,
                        back=request.query_params.get("back", ""))  # fmt: skip

    @router.post("/{lang}/orders/rules/{target}")
    async def rules_save(request: Request, lang: str, target: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=200, max_part_size=4096)
        sid = kit.need_csrf(request, form, lang)
        info, demo = await orders.target(kit, request, lang, target)
        f = {k: str(v) for k, v in form.items() if isinstance(v, str)}
        data = {
            "default": f.get("default", ""),
            "round": f.get("round", "0.01"),
            "categories": [
                {"name": f.get(f"cat{i}", ""), "percent": f.get(f"pct{i}", "")} for i in range(60)
            ],
            "volume": [{"from": f.get(f"from{i}", ""), "percent": f.get(f"vol{i}", "")} for i in range(6)],
        }
        try:
            rules = parse_rules(data)
        except RulesError as exc:
            key = _target_key(info, demo)
            return kit.page(request, "orders_rules.html", lang, 400, target=key, info=info, demo=demo,
                            rules=None, typed=f, error=exc.code, back=f.get("back", ""))  # fmt: skip
        # the key and every part of the address come from what is stored, never from what was posted
        saved_for = _target_key(info, demo)
        await orders.store(request).set_rules(kit.owner(sid), saved_for, rules.to_json())
        try:
            run_back = await orders.store(request).get_run(uuid.UUID(f.get("back", "")), kit.owner(sid))
        except (ValueError, RunNotFound):
            return redirect(f"/{lang}/orders/rules/{saved_for}?n=saved")
        return redirect(f"/{lang}/orders/{run_back.id}")

    return router


def _target_key(info: ConnectionInfo, demo: bool) -> str:
    """How a target is written in a run and keys its rules: always built from the stored connection."""
    return f"demo:{info.crm.value}" if demo else f"conn:{info.id}"


def _demo_currency(info: ConnectionInfo) -> str | None:
    return {"amocrm": "UZS", "kommo": "UZS", "bitrix24": "RUB", "zoho": "UZS", "odoo": "USD"}.get(
        info.crm.value
    )


class _NoReader:
    """A spreadsheet with a recognised header needs no model; anything else does."""

    async def extract(self, source: intake.Source, gate: Gate) -> Any:
        raise ModelError("e_model_off")

    async def choose(self, questions: list[Any], gate: Gate) -> tuple[list[Any], Usage]:
        raise ModelError("e_model_off")


def make_solution(orders: Orders) -> Solution:
    async def startup(app: FastAPI) -> None:
        await orders.startup(app)

    async def sweep(app: FastAPI) -> None:
        await orders.sweep(app)

    return Solution(
        slug="orders",
        titles={"ru": "Заявка клиента → сделка с товарами", "en": "Customer order → deal with products"},
        templates=HERE / "templates",
        static=HERE / "static",
        texts=texts,
        router=lambda kit: build(kit, orders),
        startup=startup,
        sweep=sweep,
    )


solution = make_solution(Orders())

__all__ = ["Decimal", "Orders", "make_solution", "solution"]
