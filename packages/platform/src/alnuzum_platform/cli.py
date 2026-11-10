"""Command line: migrate, serve; head and downgrade for a rollback to an older image."""

import argparse
import asyncio
import sys

import uvicorn

from alnuzum_platform.config import get_settings
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.db import make_engine, make_sessionmaker
from alnuzum_platform.migrate import downgrade, head, upgrade
from alnuzum_platform.store import ConnectionStore


async def rotate(database_url: str, keys: str) -> int:
    engine = make_engine(database_url)
    try:
        return await ConnectionStore(make_sessionmaker(engine), Keyring.from_env(keys)).rotate_keys()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alnuzum-platform")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("rotate-keys", help="re-encrypt every secret under the first key of SECRET_KEYS")
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
        count = asyncio.run(rotate(settings.database_url, settings.secret_keys.get_secret_value()))
        print(f"re-encrypted: {count}")
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
