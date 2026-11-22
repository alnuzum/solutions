import base64

import pytest
import uvicorn

from alnuzum_platform import cli
from alnuzum_platform.config import get_settings

KEYS = "k1:" + base64.b64encode(b"\x01" * 32).decode()
BOTH = "k2:" + base64.b64encode(b"\x02" * 32).decode() + "," + KEYS


def test_migrate_and_rotate_keys(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated)
    monkeypatch.setenv("SECRET_KEYS", BOTH)
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate"]) == 0
        assert cli.main(["rotate-keys"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "re-encrypted: 0" in out and "left under other keys: 0" in out


def test_rotate_keys_with_one_key_says_so_and_fails(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The container kept its old environment (a restart does not read .env again): with the old key alone
    rotate-keys would find nothing to do and pass for done."""
    monkeypatch.setenv("DATABASE_URL", migrated)
    monkeypatch.setenv("SECRET_KEYS", KEYS)
    get_settings.cache_clear()
    try:
        assert cli.main(["rotate-keys"]) == 2
    finally:
        get_settings.cache_clear()
    err = capsys.readouterr().err
    assert "SECRET_KEYS holds one key (k1)" in err and "docker compose up -d platform" in err


def test_rotate_keys_fails_when_a_solution_does_not_load(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from importlib.metadata import entry_points

    from alnuzum_platform import solutions

    class Broken:
        name = "mail"

        def load(self) -> object:
            raise ImportError("a broken install")

    monkeypatch.setattr(
        solutions,
        "entry_points",
        lambda group: [ep for ep in entry_points(group=group) if ep.name != "mail"] + [Broken()],
    )
    monkeypatch.setenv("DATABASE_URL", migrated)
    monkeypatch.setenv("SECRET_KEYS", BOTH)
    get_settings.cache_clear()
    try:
        assert cli.main(["rotate-keys"]) == 1
    finally:
        get_settings.cache_clear()
    assert "solution mail does not load: its data was not re-encrypted" in capsys.readouterr().err


def test_serve_runs_uvicorn(migrated: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setenv("DATABASE_URL", migrated)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw, app=a[0]))
    get_settings.cache_clear()
    try:
        assert cli.main(["serve", "--port", "9999"]) == 0
    finally:
        get_settings.cache_clear()
    assert seen["port"] == 9999
    assert seen["factory"] is True
    assert seen["app"] == "alnuzum_platform.api:serve_app"  # the container's log first, then the app


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


def test_the_readme_lists_every_solution_with_its_demo_and_measured_date() -> None:
    """What a stranger opens first: every installed solution with a link to its demo and the date of its
    reference set, in both languages; nothing «coming next»."""
    import json
    from pathlib import Path

    from alnuzum_platform.solutions import discover

    root = Path(__file__).resolve().parents[3]
    en, ru = (root / "README.md").read_text(), (root / "README.ru.md").read_text()
    assert "coming next" not in en and "следующий этап" not in ru
    assert "](https://apps.alnuzum.com/en)" in en and "](https://apps.alnuzum.com/ru)" in ru
    for s in discover():
        assert (
            f"](https://apps.alnuzum.com/en/{s.slug})" in en
            and f"](https://apps.alnuzum.com/ru/{s.slug})" in ru
        )
        for q in root.glob("apps/*/src/*/quality.json"):
            if q.parent.name.removeprefix("alnuzum_") == s.slug:
                y, m, d = json.loads(q.read_text())["date"].split("-")
                assert f"({y}-{m}-{d})" in en and f"({d}.{m}.{y})" in ru, (s.slug, q)
