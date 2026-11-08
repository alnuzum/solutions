"""HTTP API of the platform: health only is public."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from alnuzum_platform.config import Settings, get_settings
from alnuzum_platform.db import make_engine, make_sessionmaker, ping


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessions = make_sessionmaker(engine)
        app.state.settings = settings
        yield
        await engine.dispose()

    app = FastAPI(
        title="Al-Nuzum platform", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        ok = await ping(app.state.engine)
        return JSONResponse({"status": "ok" if ok else "db_unavailable"}, status_code=200 if ok else 503)

    return app
