import base64

import pytest
import uvicorn

from alnuzum_platform import cli
from alnuzum_platform.config import get_settings

KEYS = "k1:" + base64.b64encode(b"\x01" * 32).decode()


def test_migrate_and_rotate_keys(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated)
    monkeypatch.setenv("SECRET_KEYS", KEYS)
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate"]) == 0
        assert cli.main(["rotate-keys"]) == 0
    finally:
        get_settings.cache_clear()
    assert "re-encrypted: 0" in capsys.readouterr().out


def test_serve_runs_uvicorn(migrated: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setenv("DATABASE_URL", migrated)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    get_settings.cache_clear()
    try:
        assert cli.main(["serve", "--port", "9999"]) == 0
    finally:
        get_settings.cache_clear()
    assert seen["port"] == 9999
    assert seen["factory"] is True


async def _tables(url: str) -> set[str]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


def test_a_rollback_takes_the_database_back_to_the_head_of_the_older_image(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What ``apps-deploy`` does when a deploy fails after its migration: the older image names its head (no
    database), the newer one downgrades to it, and the older image's own ``migrate`` then finds a revision it
    knows. Up again afterwards: the other tests share the database."""
    import asyncio
    import re

    from alnuzum_platform.migrate import head

    assert cli.main(["head"]) == 0
    newest = capsys.readouterr().out.strip()
    assert newest == head() and re.fullmatch(r"[0-9a-z_]{1,32}", newest)
    monkeypatch.setenv("DATABASE_URL", migrated)
    get_settings.cache_clear()
    try:
        assert cli.main(["downgrade", "0013"]) == 0
        assert not {"winback_scans", "winback_tasks"} & asyncio.run(_tables(migrated))
        assert "mail_runs" in asyncio.run(_tables(migrated))
        assert cli.main(["migrate"]) == 0
    finally:
        get_settings.cache_clear()
    assert {"winback_scans", "winback_tasks"} <= asyncio.run(_tables(migrated))


def test_the_automatic_rollback_downgrades_with_the_new_image_before_the_old_one_starts() -> None:
    from pathlib import Path

    script = (Path(__file__).resolve().parents[3] / "deploy" / "server" / "apps-deploy").read_text()
    body = script[script.index("rollback() {") : script.index("\n  }\n", script.index("rollback() {"))]
    old_head = body.index("get_current_head")
    down = body.index("alnuzum-platform downgrade")
    restore = body.index('cp -p "$DIR/.env.prev" "$DIR/.env"')
    assert old_head < down < restore, "the database goes back before the old image starts"
    assert "--network none" in body
