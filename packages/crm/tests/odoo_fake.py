"""A tiny in-memory Odoo for unit tests: JSON-RPC over httpx.MockTransport with SQL LIKE rules."""

import json
import re
from typing import Any

import httpx


def like(pattern: str, value: str, *, ci: bool) -> bool:
    """SQL LIKE: % any run, _ one character, backslash escapes."""
    out, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out += re.escape(pattern[i + 1])
            i += 2
            continue
        out += ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
        i += 1
    return re.fullmatch(out, value, re.S | (re.I if ci else 0)) is not None


def matches(row: dict[str, Any], term: list[Any]) -> bool:
    field, op, value = term
    have = row.get(field)
    if op == "=":
        return bool(have == value)
    if op == "in":
        return have in value
    if op in ("=ilike", "=like"):
        return isinstance(have, str) and like(str(value), have, ci=op == "=ilike")
    if op == "ilike":
        return isinstance(have, str) and like(f"%{value}%", have, ci=True)
    raise NotImplementedError(op)


class FakeOdoo:
    def __init__(self, records: dict[str, list[dict[str, Any]]]) -> None:
        self.records = records
        self.calls: list[tuple[str, str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)["params"]
        if params["method"] == "authenticate":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 2})
        _db, _uid, _key, model, method, args, kwargs = params["args"]
        self.calls.append((model, method, args))
        rows = [
            r
            for r in self.records.get(model, [])
            if all(matches(r, t) for t in args[0] if isinstance(t, list))
        ]
        result: Any = [r["id"] for r in rows] if method == "search" else rows
        if kwargs.get("limit"):
            result = result[: kwargs["limit"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
