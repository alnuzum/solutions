"""Solutions plug into the platform: their pages share its sessions, CSRF, CRM connections and look.

A solution is a package that declares an entry point in the group ``alnuzum.solutions`` pointing at a
``Solution``. The platform gives it a ``Kit`` (the same helpers its own pages use) and mounts its router,
templates and static files; the solution's startup and sweep run with the platform's.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from alnuzum_platform.config import Settings
from alnuzum_platform.store import ConnectionInfo, ConnectionNotFound, ConnectionStore
from alnuzum_platform.web.connectors import Connectors
from alnuzum_platform.web.i18n import texts
from alnuzum_platform.web.routes import PageError
from alnuzum_platform.web.security import Sessions

log = logging.getLogger("alnuzum_platform.solutions")

GROUP = "alnuzum.solutions"


@dataclass
class Kit:
    settings: Settings
    sessions: Sessions
    connectors: Connectors
    templates: Jinja2Templates
    quota: int
    extra_texts: Callable[[str], dict[str, str]] = field(default=lambda lang: {})

    def texts(self, lang: str) -> dict[str, str]:
        return texts(lang) | self.extra_texts(lang)

    def store_of(self, request: Request) -> ConnectionStore:
        found: ConnectionStore | None = request.app.state.store
        if found is None:
            raise PageError(503, "e_unreachable", "e_unreachable")
        return found

    def ensure_session(self, request: Request) -> str:
        if request.state.sid is None:
            request.state.sid = self.sessions.new_sid()
            request.state.issue_cookie = True
        sid: str = request.state.sid
        return sid

    def need_session(self, request: Request, lang: str) -> str:
        if request.state.sid is None:
            raise PageError(401, "e_401_t", "e_401", lang)
        sid: str = request.state.sid
        return sid

    def need_csrf(self, request: Request, form: Mapping[str, Any], lang: str) -> str:
        sid = request.state.sid
        origin = request.headers.get("origin")
        token = form.get("csrf")
        if (
            sid is None
            or (origin is not None and origin != self.settings.origin)
            or not self.sessions.csrf_ok(sid, token if isinstance(token, str) else None)
        ):
            raise PageError(403, "e_403_t", "e_403", lang)
        return str(sid)

    @staticmethod
    def owner(sid: str) -> str:
        return Sessions.owner(sid)

    async def own_connection(self, request: Request, lang: str, conn_id: Any) -> ConnectionInfo:
        """An active or broken connection of this session; anybody else's is a 404."""
        sid = self.need_session(request, lang)
        try:
            return await self.store_of(request).get(conn_id, Sessions.owner(sid))
        except ConnectionNotFound:
            raise PageError(404, "e_404_t", "e_404", lang) from None

    def page(self, request: Request, name: str, lang: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        sid = request.state.sid
        base = {
            "t": self.texts(lang),
            "lang": lang,
            "other": "en" if lang == "ru" else "ru",
            "env": self.settings.app_env,
            "csrf": self.sessions.csrf(sid) if sid else "",
            "quota": self.quota,
            "path": request.url.path,
        }
        return self.templates.TemplateResponse(request, name, base | ctx, status_code=status)


@dataclass
class Solution:
    """What a solution package exposes through its entry point."""

    slug: str  # the path segment of its pages: /{lang}/{slug}
    titles: dict[str, str]  # language -> name on the platform's home page
    templates: Path
    static: Path
    texts: Callable[[str], dict[str, str]]
    router: Callable[[Kit], APIRouter]
    startup: Callable[[FastAPI], Awaitable[None]] | None = None
    sweep: Callable[[FastAPI], Awaitable[None]] | None = None


def discover(names: list[str] | None = None) -> list[Solution]:
    """Installed solutions (``names`` limits them, ``[]`` turns them all off)."""
    found: list[Solution] = []
    for ep in entry_points(group=GROUP):
        if names is not None and ep.name not in names:
            continue
        try:
            item = ep.load()
        except Exception:
            log.exception("solution %s does not load", ep.name)
            continue
        if isinstance(item, Solution):
            found.append(item)
    return sorted(found, key=lambda s: s.slug)
