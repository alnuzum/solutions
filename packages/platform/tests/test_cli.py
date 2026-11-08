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
