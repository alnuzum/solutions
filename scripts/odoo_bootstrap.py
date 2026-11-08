"""Prepare a fresh Odoo Community for the live E2E: database, admin password, CRM and Sales.

    python scripts/odoo_bootstrap.py --url http://127.0.0.1:8069 --db e2e --password "$ODOO_PASSWORD"

Idempotent: an existing database is reused and installed modules are skipped. Only for a local
or CI test Odoo (it uses the default master password of the official image).
"""

import argparse
import json
import sys
import time
import urllib.request
from typing import Any

MODULES = ["crm", "sale_management", "sale_crm"]


def rpc(url: str, service: str, method: str, *args: Any) -> Any:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": list(args)},
            "id": 1,
        }
    ).encode()
    if not url.startswith(("http://", "https://")):
        raise ValueError("--url must be http(s)")
    req = urllib.request.Request(f"{url}/jsonrpc", data=body, headers={"Content-Type": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 - the URL is the operator's test Odoo
        data = json.loads(resp.read())
    if data.get("error"):
        raise RuntimeError(str(data["error"].get("data", {}).get("message") or data["error"])[:300])
    return data["result"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8069")
    ap.add_argument("--db", default="e2e")
    ap.add_argument("--login", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--master", default="admin", help="master password of the test Odoo")
    ap.add_argument("--wait", type=int, default=180, help="seconds to wait for Odoo to start")
    args = ap.parse_args()
    url = args.url.rstrip("/")

    deadline = time.monotonic() + args.wait
    while True:
        try:
            version = rpc(url, "common", "version")["server_version"]
            break
        except OSError:
            if time.monotonic() > deadline:
                print("Odoo did not start", file=sys.stderr)
                return 1
            time.sleep(2)
    print(f"Odoo {version}")
    if args.db not in rpc(url, "db", "list"):
        rpc(
            url,
            "db",
            "create_database",
            args.master,
            args.db,
            False,
            "en_US",
            args.password,
            args.login,
            "UZ",
            "",
        )
        print(f"database {args.db} created")
    uid = rpc(url, "common", "authenticate", args.db, args.login, args.password, {})
    if not uid:
        print("admin login refused", file=sys.stderr)
        return 1

    def kw(model: str, method: str, *a: Any) -> Any:
        return rpc(url, "object", "execute_kw", args.db, uid, args.password, model, method, list(a), {})

    mods = kw("ir.module.module", "search_read", [["name", "in", MODULES]], ["name", "state"])
    todo = [m["id"] for m in mods if m["state"] != "installed"]
    if todo:
        kw("ir.module.module", "button_immediate_install", todo)
    print("modules:", ", ".join(sorted(m["name"] for m in mods)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
