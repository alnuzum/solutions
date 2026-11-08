"""Outbound requests to an address a user typed in (self-hosted Odoo): public hosts only.

The address is checked twice: when the connection is saved (``check_public_url``) and on every
TCP connect (``guarded_transport``), which resolves the host itself, refuses if any address is
not public, and connects to the address it checked. A DNS answer that changes between the check
and the connect (DNS rebinding) therefore cannot reach an internal address. TLS still verifies
the certificate against the host name.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx


class BlockedAddress(ValueError):
    """The address is not public, or not an https URL."""


def is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BlockedAddress(f"{host} does not resolve") from exc
    ips = sorted({str(info[4][0]) for info in infos})
    if not ips or not all(is_public(ip) for ip in ips):
        raise BlockedAddress(f"{host} resolves to a non-public address")
    return ips


def split_https_url(url: str) -> tuple[str, int, str]:
    """(host, 443, normalised base URL) of an https URL on the standard port, without credentials,
    query or fragment."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise BlockedAddress("only https addresses are allowed")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise BlockedAddress("the address must not carry credentials, a query or a fragment")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedAddress("the address has no host")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise BlockedAddress("bad port") from exc
    if port != 443:
        raise BlockedAddress("only the standard https port 443 is allowed")
    netloc = f"[{host}]" if ":" in host else host
    return host, port, f"https://{netloc}{parts.path.rstrip('/')}"


async def check_public_url(url: str) -> str:
    """The normalised URL if it is https and every address of its host is public."""
    host, port, base = split_https_url(url)
    await resolve_public(host, port)
    return base


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - the httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        ips = await resolve_public(host, port)
        return await self._inner.connect_tcp(
            ips[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - the httpcore interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddress("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose every connection goes only to a public address."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        pool = getattr(self, "_pool", None)
        if not isinstance(
            pool, httpcore.AsyncConnectionPool
        ):  # pragma: no cover - fail closed on httpx changes
            raise RuntimeError("httpx transport layout changed: the network guard cannot be installed")
        pool._network_backend = _GuardedBackend()


def guarded_transport(**kw: Any) -> httpx.AsyncBaseTransport:
    return GuardedTransport(**kw)
