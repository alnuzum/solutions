"""Alembic run from the installed package: no alembic.ini is needed next to the code."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def head() -> str:
    """The newest revision this code knows; no database is asked. A rollback reads it from the older image
    (``deploy/server/apps-deploy``) and takes the database back to it with the newer one."""
    return str(ScriptDirectory.from_config(alembic_config("postgresql://")).get_current_head() or "")
