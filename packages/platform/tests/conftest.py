"""Platform tests run against a real Postgres: DATABASE_URL or the local compose database."""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_platform.migrate import downgrade, upgrade

DB_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum")


@pytest.fixture(scope="session")
async def migrated() -> str:
    await asyncio.to_thread(downgrade, DB_URL, "base")
    await asyncio.to_thread(upgrade, DB_URL)
    return DB_URL


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated)
    yield eng
    await eng.dispose()
