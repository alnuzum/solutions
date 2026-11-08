"""HTTP API of the platform.

``/health`` is public. ``/connections`` is an internal API for the solutions' backends: it needs
``Authorization: Bearer <ADMIN_TOKEN>``, and with ``ADMIN_TOKEN`` unset it refuses everyone.
Answers never carry secrets.
"""

import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from alnuzum_crm.types import CrmKind
from alnuzum_platform.config import Settings, get_settings
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.db import make_engine, make_sessionmaker, ping
from alnuzum_platform.store import ConnectionInfo, ConnectionNotFound, ConnectionStore

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
        return cls(**info.__dict__)


def quiet_http_logs() -> None:
    """httpx logs request URLs at INFO, and a Bitrix24 webhook URL holds its token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    quiet_http_logs()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        sessions = make_sessionmaker(engine)
        keys = settings.secret_keys.get_secret_value()
        app.state.store = ConnectionStore(sessions, Keyring.from_env(keys)) if keys else None
        yield
        await engine.dispose()

    app = FastAPI(
        title="Al-Nuzum platform", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    def admin(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = settings.admin_token.get_secret_value()
        scheme, _, given = (authorization or "").partition(" ")
        ok = (
            bool(expected)
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
        return JSONResponse({"status": "ok" if ok else "db_unavailable"}, status_code=200 if ok else 503)

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

    return app
