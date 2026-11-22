"""Command line: migrate, serve; head and downgrade for a rollback to an older image."""

import argparse
import asyncio
import sys

import uvicorn

from alnuzum_platform.config import get_settings
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.db import make_engine, make_sessionmaker
from alnuzum_platform.migrate import downgrade, head, upgrade
from alnuzum_platform.solutions import discover
from alnuzum_platform.store import ConnectionStore, Rotation


async def rotate(database_url: str, keys: str) -> Rotation:
    engine = make_engine(database_url)
    try:
        failed: list[str] = []
        sealed = [column for solution in discover(failed=failed) for column in solution.sealed]
        result = await ConnectionStore(make_sessionmaker(engine), Keyring.from_env(keys)).rotate_keys(sealed)
        result.unloaded = failed
        return result
    finally:
        await engine.dispose()


def rotate_command(database_url: str, keys: str) -> int:
    """0 only when nothing is left under another key: then the old key may leave SECRET_KEYS."""
    keyring = Keyring.from_env(keys)
    if len(keyring.ids) < 2:
        print(
            f"rotate-keys: SECRET_KEYS holds one key ({keyring.primary}). Put the new key first and"
            " the old one after it, then recreate the container (docker compose up -d platform):"
            " a restart keeps the old environment.",
            file=sys.stderr,
        )
        return 2
    result = asyncio.run(rotate(database_url, keys))
    print(f"re-encrypted: {result.done}")
    for name in result.unloaded:
        print(f"solution {name} does not load: its data was not re-encrypted", file=sys.stderr)
    for where in result.broken:
        print(f"does not decrypt, left as it is: {where}", file=sys.stderr)
    left = ", ".join(f"{where} {n}" for where, n in result.left.items())
    print(f"left under other keys: {sum(result.left.values())}" + (f" ({left})" if left else ""))
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alnuzum-platform")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser(
        "rotate-keys",
        help="re-encrypt everything sealed (CRM secrets, OAuth windows, solutions' data) under the first key",
    )
    serve = sub.add_parser("serve", help="migrate, then run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("head", help="print the newest migration this code knows (no database is asked)")
    down = sub.add_parser(
        "downgrade", help="take the database back to a revision, before an older image runs"
    )
    down.add_argument("revision")
    args = parser.parse_args(argv)
    if args.cmd == "head":
        print(head())
        return 0
    settings = get_settings()
    if args.cmd == "downgrade":
        downgrade(settings.database_url, args.revision)
        print(f"downgraded to {args.revision}")
        return 0
    upgrade(settings.database_url)
    if args.cmd == "rotate-keys":
        return rotate_command(settings.database_url, settings.secret_keys.get_secret_value())
    if args.cmd == "serve":
        # behind Caddy only (no published port): the client address comes from X-Forwarded-For.
        # The access log is off: query strings carry OAuth codes; Caddy keeps its own log without them.
        # The app's own lines (INFO of alnuzum_*) go to stdout through serve_app's configure_logs.
        uvicorn.run(
            "alnuzum_platform.api:serve_app",
            factory=True,
            host=args.host,
            port=args.port,
            proxy_headers=True,
            forwarded_allow_ips="*",
            access_log=False,
            server_header=False,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
