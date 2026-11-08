"""HTTP side of the platform.

``/health`` is public. ``/connections`` is an internal API for the solutions' backends: it needs
``Authorization: Bearer <ADMIN_TOKEN>``, and with ``ADMIN_TOKEN`` unset or shorter than 32 characters it
refuses everyone. The pages (``web``) connect CRMs from the browser under a session cookie. Answers
never carry secrets.
"""

import asyncio
import contextlib
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request as StarletteRequest

from alnuzum_crm.errors import CrmAuthError, CrmError
from alnuzum_crm.types import CrmKind
from alnuzum_platform.config import Settings, get_settings
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.db import make_engine, make_sessionmaker, ping
from alnuzum_platform.store import ConnectionDisabled, ConnectionInfo, ConnectionNotFound, ConnectionStore
from alnuzum_platform.web.connectors import Connectors
from alnuzum_platform.web.routes import HookRefused, PageError, build_router, render_error
from alnuzum_platform.web.security import SESSION_DAYS, RateLimiter, Sessions, make_middleware

WEB = Path(__file__).parent / "web"
# a connection its session has not opened for this long is erased: the cookie is gone by then
EXPIRE_DAYS = SESSION_DAYS

log = logging.getLogger("alnuzum_platform")


class ConnectionIn(BaseModel):
    owner: str = Field(min_length=1, max_length=128)
    crm: CrmKind
    endpoint: str = Field(default="", max_length=300)
    label: str = Field(default="", max_length=200)
    secrets: dict[str, str]


class ConnectionOut(BaseModel):
    id: uuid.UUID
    owner: str
    crm: CrmKind
    label: str
    endpoint: str
    status: str
    access_expires_at: datetime | None
    created_at: datetime | None

    @classmethod
    def of(cls, info: ConnectionInfo) -> "ConnectionOut":
        fields = set(cls.model_fields)
        return cls(**{k: v for k, v in info.__dict__.items() if k in fields})


def quiet_http_logs() -> None:
    """httpx logs request URLs at INFO, and a Bitrix24 webhook URL holds its token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def sweep(store: ConnectionStore, connectors: Connectors) -> tuple[int, int]:
    """Check every active connection once: access removed on the CRM side (an uninstalled amoCRM
    integration, a deleted Bitrix24 webhook, a revoked Zoho or Odoo key) turns it to
    ``reauth_required``. Returns (checked, lost)."""
    checked = lost = 0
    for info in await store.list_active():
        checked += 1
        try:
            secrets = await store.secrets(info.id)
            async with connectors.adapter(info, secrets, store) as crm:
                await crm.account()
        except (CrmAuthError, ConnectionDisabled):
            lost += 1
            await store.mark(info.id, status="reauth_required", error="auth")
        except CrmError as exc:
            await store.mark(info.id, error=(exc.code or type(exc).__name__)[:40])
        except Exception:  # one broken connection must not stop the round; no details: they may hold data
            log.warning("sweep: connection %s failed", info.id)
            await store.mark(info.id, error="internal")
        else:
            await store.mark(info.id)
    for kind, held in await store.expire_unseen(EXPIRE_DAYS):
        await connectors.revoke(kind, held)
    await store.purge_states()
    return checked, lost


async def sweep_forever(app: FastAPI, minutes: int) -> None:
    while True:
        await asyncio.sleep(minutes * 60)
        store: ConnectionStore | None = app.state.store
        if store is not None:
            try:
                checked, lost = await sweep(store, app.state.connectors)
                log.info("sweep: %d checked, %d lost access", checked, lost)
            except Exception:
                log.exception("sweep round failed")


def create_app(settings: Settings | None = None, *, transport: object | None = None) -> FastAPI:
    """``transport`` (an ``httpx.AsyncBaseTransport``) replaces the network of CRM calls in tests."""
    settings = settings or get_settings()
    quiet_http_logs()
    connectors = Connectors(settings, transport=transport)  # type: ignore[arg-type]
    web_on = settings.strong(settings.session_secret)
    sessions = Sessions(settings.session_secret.get_secret_value(), secure=settings.secure_cookies)
    templates = Jinja2Templates(directory=str(WEB / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        maker = make_sessionmaker(engine)
        keys = settings.secret_keys.get_secret_value()
        app.state.store = ConnectionStore(maker, Keyring.from_env(keys)) if keys else None
        task = None
        if settings.sweep_minutes > 0:
            task = asyncio.create_task(sweep_forever(app, settings.sweep_minutes))
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await engine.dispose()

    app = FastAPI(
        title="Al-Nuzum platform", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.connectors = connectors
    app.state.store = None
    limiter = RateLimiter(settings.rate_per_minute, settings.rate_post_per_minute)
    app.middleware("http")(make_middleware(lambda _: sessions if web_on else None, limiter))

    @app.exception_handler(PageError)
    async def page_error(request: StarletteRequest, exc: PageError) -> HTMLResponse:
        return render_error(templates, request, exc, settings.app_env)

    @app.exception_handler(HookRefused)
    async def hook_refused(request: StarletteRequest, exc: HookRefused) -> PlainTextResponse:
        return PlainTextResponse("refused", status_code=exc.status)

    def admin(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = settings.admin_token.get_secret_value()
        scheme, _, given = (authorization or "").partition(" ")
        ok = (
            settings.strong(settings.admin_token)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(given.encode(), expected.encode())
        )
        if not ok:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def store(request: Request) -> ConnectionStore:
        found: ConnectionStore | None = request.app.state.store
        if found is None:
            raise HTTPException(503, "SECRET_KEYS is not configured")
        return found

    Admin = Annotated[None, Depends(admin)]
    Store = Annotated[ConnectionStore, Depends(store)]

    @app.get("/health")
    async def health() -> JSONResponse:
        ok = await ping(app.state.engine)
        return JSONResponse(
            {"status": "ok" if ok else "db_unavailable", "version": settings.app_version},
            status_code=200 if ok else 503,
        )

    @app.get("/connections")
    async def list_connections(
        _: Admin, st: Store, owner: Annotated[str, Query(min_length=1)]
    ) -> list[ConnectionOut]:
        return [ConnectionOut.of(c) for c in await st.list(owner)]

    @app.post("/connections", status_code=201)
    async def create_connection(_: Admin, st: Store, body: ConnectionIn) -> ConnectionOut:
        try:
            info = await st.create(body.owner, body.crm, body.endpoint, body.secrets, label=body.label)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return ConnectionOut.of(info)

    @app.post("/connections/{conn_id}/disable", status_code=204)
    async def disable_connection(
        _: Admin, st: Store, conn_id: uuid.UUID, owner: Annotated[str, Query(min_length=1)]
    ) -> Response:
        try:
            await st.disable(conn_id, owner)
        except ConnectionNotFound:
            raise HTTPException(404, "not found") from None
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
    if web_on:
        app.include_router(build_router(settings.quota_writes_per_day, sessions, connectors, templates))
    return app
