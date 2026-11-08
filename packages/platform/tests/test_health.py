import httpx
from fastapi import FastAPI

from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


async def test_health_ok(migrated: str) -> None:
    resp = await _get(create_app(Settings(database_url=migrated)), "/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_health_reports_db_down() -> None:
    app = create_app(Settings(database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/none"))
    resp = await _get(app, "/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "db_unavailable"}


async def test_docs_are_not_exposed(migrated: str) -> None:
    resp = await _get(create_app(Settings(database_url=migrated)), "/openapi.json")
    assert resp.status_code == 404
