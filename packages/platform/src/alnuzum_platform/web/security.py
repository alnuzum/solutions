"""Sessions without registration, CSRF, rate limits and response headers.

A session is a random id in a signed cookie. The database never sees the id itself: a connection's
``owner`` is a hash of it, so a leaked database row cannot be turned back into a cookie.
"""

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

SESSION_DAYS = 30
_SID_BYTES = 32


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mac(secret: bytes, purpose: str, sid: str) -> str:
    return _b64(hmac.new(secret, f"{purpose}:{sid}".encode(), hashlib.sha256).digest())


class Sessions:
    def __init__(self, secret: str, *, secure: bool) -> None:
        self._secret = secret.encode()
        self.secure = secure
        # __Host- binds the cookie to this exact host over https: the stand cannot set it for prod
        self.cookie = "__Host-an_sid" if secure else "an_sid"

    @staticmethod
    def new_sid() -> str:
        return secrets.token_urlsafe(_SID_BYTES)

    def sign(self, sid: str) -> str:
        return f"{sid}.{_mac(self._secret, 'sid', sid)}"

    def verify(self, value: str | None) -> str | None:
        """The session id of a cookie value, or None for a missing, forged or malformed one."""
        if not value or value.count(".") != 1:
            return None
        sid, sig = value.split(".")
        if len(sid) != 43 or not sid.isascii():
            return None
        expected = _mac(self._secret, "sid", sid)
        return sid if hmac.compare_digest(sig.encode("utf-8", "replace"), expected.encode()) else None

    @staticmethod
    def owner(sid: str) -> str:
        return "s:" + hashlib.sha256(sid.encode()).hexdigest()[:48]

    def csrf(self, sid: str) -> str:
        return _mac(self._secret, "csrf", sid)

    def csrf_ok(self, sid: str, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(
            (token or "").encode("utf-8", "replace"), self.csrf(sid).encode()
        )

    def set_cookie(self, response: Response, sid: str) -> None:
        response.set_cookie(
            self.cookie,
            self.sign(sid),
            max_age=SESSION_DAYS * 86400,
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )


class RateLimiter:
    """Fixed one-minute windows per client address, in the memory of this process."""

    def __init__(self, per_minute: int, post_per_minute: int) -> None:
        self.per_minute = per_minute
        self.post_per_minute = post_per_minute
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._window = 0

    @staticmethod
    def client(ip: str) -> str:
        """An IPv6 client usually holds a whole /64, so the /64 is one client."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        if isinstance(addr, ipaddress.IPv6Address):
            if addr.ipv4_mapped is not None:
                return str(addr.ipv4_mapped)
            return str(ipaddress.ip_network(f"{addr}/64", strict=False))
        return str(addr)

    def allow(self, ip: str, method: str, now: float | None = None) -> bool:
        ip = self.client(ip)
        window = int((now if now is not None else time.time()) // 60)
        if window != self._window:
            self._counts.clear()
            self._window = window
        self._counts[(ip, "all", window)] += 1
        if self._counts[(ip, "all", window)] > self.per_minute:
            return False
        if method == "POST":
            self._counts[(ip, "post", window)] += 1
            if self._counts[(ip, "post", window)] > self.post_per_minute:
                return False
        return True


def content_security_policy() -> str:
    # no script at all: every page works with plain links and forms
    return "; ".join(
        (
            "default-src 'none'",
            "style-src 'self'",
            "font-src 'self'",
            "img-src 'self' data:",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        )
    )


SECURITY_HEADERS = {
    "Content-Security-Policy": content_security_policy(),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}

Handler = Callable[[Request], Awaitable[Response]]


def make_middleware(
    sessions_of: Callable[[Request], Sessions | None], limiter: RateLimiter
) -> Callable[[Request, Handler], Awaitable[Response]]:
    async def middleware(request: Request, call_next: Handler) -> Response:
        path = request.url.path
        if path != "/health" and not path.startswith("/static/"):
            ip = request.client.host if request.client else "?"
            if not limiter.allow(ip, request.method):
                return PlainTextResponse("Too many requests", status_code=429, headers={"Retry-After": "60"})
        sessions = sessions_of(request)
        request.state.sid = None
        request.state.issue_cookie = False
        if sessions is not None:
            request.state.sid = sessions.verify(request.cookies.get(sessions.cookie))
        response = await call_next(request)
        # a new session gets its cookie; a known one has it renewed on every page, so it lasts
        # SESSION_DAYS from the last visit, not from the first
        is_page = response.headers.get("content-type", "").startswith("text/html")
        if sessions is not None and request.state.sid and (request.state.issue_cookie or is_page):
            sessions.set_cookie(response, request.state.sid)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return middleware
