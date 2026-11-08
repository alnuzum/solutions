"""Command line: migrate, serve."""

import argparse
import sys

import uvicorn

from alnuzum_platform.config import get_settings
from alnuzum_platform.migrate import upgrade


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alnuzum-platform")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    serve = sub.add_parser("serve", help="migrate, then run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    settings = get_settings()
    upgrade(settings.database_url)
    if args.cmd == "serve":
        uvicorn.run("alnuzum_platform.api:create_app", factory=True, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
