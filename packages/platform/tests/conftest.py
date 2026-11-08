"""Platform tests run against a real Postgres: DATABASE_URL or the local compose database."""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alnuzum_platform.crypto import Keyring
from alnuzum_platform.db import make_sessionmaker
from alnuzum_platform.migrate import downgrade, upgrade
from alnuzum_platform.store import ConnectionStore

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
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM crm_connections"))
    await eng.dispose()


@pytest.fixture
def keyring() -> Keyring:
    return Keyring([("k2", b"\x02" * 32), ("k1", b"\x01" * 32)])


@pytest.fixture
def store(engine: AsyncEngine, keyring: Keyring) -> ConnectionStore:
    return ConnectionStore(make_sessionmaker(engine), keyring)
