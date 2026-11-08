"""Pages of the platform: pick a CRM, connect it, check it, disconnect it.

Who may do what:
- pages that show connections need the session cookie (401 without it) and show only the session's own
  connections (404 for anybody else's);
- every POST from a page needs the session's CSRF token and, when the browser sends one, our Origin (403);
- the CRM hooks (``/hooks/...``) carry no cookie: amoCRM's key delivery is bound to a secret one-time
  state, Kommo's uninstall hook to an HMAC signature with our client secret.
"""

import hashlib
import hmac
import json
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from alnuzum_crm.errors import (
    CrmAuthError,
    CrmError,
    CrmRateLimited,
    CrmUnavailable,
)
from alnuzum_crm.types import CrmKind, DealIn
from alnuzum_platform.store import (
    ConnectionDisabled,
    ConnectionInfo,
    ConnectionNotFound,
    ConnectionStore,
    QuotaExceeded,
    StateInvalid,
)
from alnuzum_platform.web.connectors import FORM_CRMS, OAUTH_CRMS, Connectors, LinkError, check_deal_title
from alnuzum_platform.web.i18n import LANGS, pick_lang, texts
from alnuzum_platform.web.security import Sessions

log = logging.getLogger("alnuzum_platform.web")

NOTICES = ("quota", "auth", "unavailable", "rejected")

# Where a page may send the browser: one of our own pages, or the consent window of a CRM.
_OWN_PAGE = re.compile(r"/(?:ru|en)(?:/[A-Za-z0-9_-]+)*(?:\?[A-Za-z0-9_=&%.-]*)?")
CONSENT_HOSTS = frozenset({"www.amocrm.ru", "www.kommo.com", "accounts.zoho.com"})
# the language as our own constant, never the string that came in the address
_LANG_OF = {code: code for code in LANGS}


HOOK_BODY_LIMIT = 64 * 1024


class HookRefused(Exception):
    """A CRM hook got a body that no CRM sends: too large or not parseable."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class PageError(Exception):
    def __init__(self, status: int, title: str, text: str, lang: str = "ru", **fmt: str) -> None:
        super().__init__(title)
        self.status, self.title, self.text, self.lang, self.fmt = status, title, text, lang, fmt


def _crm(value: str, allowed: tuple[CrmKind, ...] = tuple(CrmKind)) -> CrmKind:
    try:
        crm = CrmKind(value)
    except ValueError:
        raise PageError(404, "e_404_t", "e_404") from None
    if crm not in allowed:
        raise PageError(404, "e_404_t", "e_404")
    return crm


def _lang(value: str) -> str:
    try:
        return _LANG_OF[value]
    except KeyError:
        raise PageError(404, "e_404_t", "e_404") from None


def redirect(location: str) -> RedirectResponse:
    """303 to one of our own pages or to a CRM's consent window. Anything else is refused, so no value
    that reaches a ``Location`` can send the browser to another site."""
    if not _OWN_PAGE.fullmatch(location):
        parts = urlsplit(location)
        if parts.scheme != "https" or parts.netloc != parts.hostname or parts.hostname not in CONSENT_HOSTS:
            raise PageError(400, "e_404_t", "e_404")
    return RedirectResponse(location, status_code=303)


def _conn_id(value: str, lang: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise PageError(404, "e_404_t", "e_404", lang) from None


def _error_code(exc: CrmError) -> str:
    if isinstance(exc, CrmAuthError):
        return "auth"
    if isinstance(exc, CrmUnavailable | CrmRateLimited):
        return "unavailable"
    # a short machine code, never the CRM's message (it may quote the customer's field values)
    return (exc.code or type(exc).__name__)[:40]


async def _params(request: Request) -> dict[str, str]:
    """Query, form or JSON: CRM hooks arrive in any of them."""
    out = {k: v for k, v in request.query_params.items()}
    if request.method == "POST":
        kind = request.headers.get("content-type", "")
        declared = request.headers.get("content-length", "0")
        if not declared.isdigit() or int(declared) > HOOK_BODY_LIMIT:
            raise HookRefused(413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > HOOK_BODY_LIMIT:
                raise HookRefused(413)
        # junk from anybody is a 400, never a 500: 5xx wake the alert
        try:
            if kind.startswith("application/json"):
                data = json.loads(body or b"{}")
                if isinstance(data, dict):
                    out |= {str(k): str(v) for k, v in data.items() if isinstance(v, str | int)}
            else:
                out |= {k: v[-1] for k, v in parse_qs(body.decode(), max_num_fields=50).items()}
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HookRefused(400) from None
    return out


def build_router(
    settings_quota: int, sessions: Sessions, connectors: Connectors, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter()
    s = connectors.settings

    def store_of(request: Request) -> ConnectionStore:
        found: ConnectionStore | None = request.app.state.store
        if found is None:
            raise PageError(503, "e_unreachable", "e_unreachable")
        return found

    def ensure_session(request: Request) -> str:
        if request.state.sid is None:
            request.state.sid = sessions.new_sid()
            request.state.issue_cookie = True
        sid: str = request.state.sid
        return sid

    def need_session(request: Request, lang: str) -> str:
        if request.state.sid is None:
            raise PageError(401, "e_401_t", "e_401", lang)
        sid: str = request.state.sid
        return sid

    def need_csrf(request: Request, form: Mapping[str, Any], lang: str) -> str:
        sid = request.state.sid
        origin = request.headers.get("origin")
        if (
            sid is None
            or (origin is not None and origin != s.origin)
            or not sessions.csrf_ok(sid, form.get("csrf") if isinstance(form.get("csrf"), str) else None)
        ):
            raise PageError(403, "e_403_t", "e_403", lang)
        return str(sid)

    def page(request: Request, name: str, lang: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        sid = request.state.sid
        base = {
            "t": texts(lang),
            "lang": lang,
            "other": "en" if lang == "ru" else "ru",
            "env": s.app_env,
            "csrf": sessions.csrf(sid) if sid else "",
            "quota": settings_quota,
            "path": request.url.path,
        }
        return templates.TemplateResponse(request, name, base | ctx, status_code=status)

    async def home(
        request: Request, lang: str, crm: CrmKind, *, status: int = 200, **ctx: Any
    ) -> HTMLResponse:
        sid = ensure_session(request)
        await store_of(request).touch(Sessions.owner(sid))
        conns = [c for c in await store_of(request).list(Sessions.owner(sid)) if c.status != "disabled"]
        return page(
            request,
            "home.html",
            lang,
            status,
            crm=crm.value,
            crms=[k.value for k in CrmKind],
            oauth=crm in OAUTH_CRMS,
            configured=connectors.configured(crm),
            connections=conns,
            **ctx,
        )

    @router.get("/")
    async def root(request: Request) -> RedirectResponse:
        return redirect(f"/{pick_lang(request.headers.get('accept-language'))}")

    @router.get("/{lang}")
    async def index(request: Request, lang: str) -> HTMLResponse:
        return await home(request, _lang(lang), CrmKind.AMOCRM)

    @router.get("/{lang}/connect/{crm}")
    async def connect_page(request: Request, lang: str, crm: str) -> HTMLResponse:
        return await home(request, _lang(lang), _crm(crm))

    @router.get("/{lang}/connect/{crm}/start")
    async def oauth_start(request: Request, lang: str, crm: str) -> RedirectResponse:
        """A link, not a form: the state it creates belongs to this browser's own session, so a page
        elsewhere that sends somebody here can only show them the CRM's consent window."""
        lang, kind = _lang(lang), _crm(crm, OAUTH_CRMS)
        if not connectors.configured(kind):
            raise PageError(503, "e_404_t", "e_not_configured", lang, crm=texts(lang)[f"crm_{kind.value}"])
        sid = ensure_session(request)
        state = await store_of(request).create_state(Sessions.owner(sid), kind, lang)
        return redirect(connectors.authorize_url(kind, state))

    @router.post("/{lang}/connect/{crm}")
    async def connect_form(request: Request, lang: str, crm: str) -> Response:
        lang, kind = _lang(lang), _crm(crm, FORM_CRMS)
        form = await request.form()
        sid = need_csrf(request, form, lang)
        fields = {k: v.strip() for k, v in form.items() if isinstance(v, str)}
        try:
            if kind is CrmKind.BITRIX24:
                if not fields.get("webhook_url"):
                    raise LinkError("e_fill")
                linked = await connectors.link_bitrix24(fields["webhook_url"])
            else:
                need = ("url", "database", "login", "api_key")
                if not all(fields.get(k) for k in need):
                    raise LinkError("e_fill")
                linked = await connectors.link_odoo(*(fields[k] for k in need))
        except LinkError as exc:
            # what was typed comes back, except the webhook address and the key: they are secrets
            keep = {k: fields.get(k, "") for k in ("url", "database", "login")}
            return await home(request, lang, kind, status=400, form_error=exc.code, typed=keep)
        info = await store_of(request).create(
            Sessions.owner(sid),
            linked.crm,
            linked.endpoint,
            linked.secrets,
            label=linked.label,
            account=linked.account,
            meta=linked.meta,
        )
        log.info("connected %s %s", info.crm.value, info.id)
        return redirect(f"/{lang}/connections/{info.id}")

    @router.get("/oauth/{crm}/callback")
    async def oauth_callback(request: Request, crm: str) -> Response:
        kind = _crm(crm, OAUTH_CRMS)
        params = dict(request.query_params)
        lang = "ru"
        sid = request.state.sid
        if sid is None or not params.get("state"):
            raise PageError(400, "e_state_t", "e_state", lang)
        try:
            lang, client = await store_of(request).consume_state(params["state"], Sessions.owner(sid), kind)
        except StateInvalid:
            raise PageError(400, "e_state_t", "e_state", lang) from None
        name = texts(lang)[f"crm_{kind.value}"]
        if params.get("error"):
            raise PageError(400, "e_state_t", "e_denied", lang)
        try:
            linked = await connectors.finish(kind, params, client)
        except LinkError:
            raise PageError(400, "e_state_t", "e_oauth", lang, crm=name) from None
        info = await store_of(request).create(
            Sessions.owner(sid),
            linked.crm,
            linked.endpoint,
            linked.secrets,
            label=linked.label,
            account=linked.account,
            meta=linked.meta,
        )
        log.info("connected %s %s", info.crm.value, info.id)
        return redirect(f"/{lang}/connections/{info.id}")

    @router.post("/hooks/amocrm/secrets")
    async def amocrm_secrets(request: Request) -> PlainTextResponse:
        """amoCRM posts the keys of the integration it has just created; always 200, or it retries."""
        p = await _params(request)
        if p.get("state") and p.get("client_id") and p.get("client_secret"):
            stored = await store_of(request).attach_client(p["state"], p["client_id"], p["client_secret"])
            log.info("amocrm keys for a window: %s", "stored" if stored else "ignored")
        return PlainTextResponse("ok")

    @router.api_route("/hooks/kommo/uninstall", methods=["GET", "POST"])
    async def kommo_uninstall(request: Request) -> PlainTextResponse:
        """Kommo calls this when the integration is removed in an account: signature =
        HMAC-SHA256("{client_uuid}|{account_id}", our client secret). Always 200; a bad one is ignored."""
        p = await _params(request)
        uuid_, account, signature = p.get("client_uuid", ""), p.get("account_id", ""), p.get("signature", "")
        secret = s.kommo_client_secret.get_secret_value()
        if secret and uuid_ == s.kommo_client_id and account.isdigit():
            expected = hmac.new(secret.encode(), f"{uuid_}|{account}".encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature.encode("utf-8", "replace"), expected.encode()):
                done = await store_of(request).disable_account(CrmKind.KOMMO, account, "crm_uninstalled")
                log.info("kommo uninstall hook: %d connection(s) disabled", done)
        return PlainTextResponse("ok")

    async def own(request: Request, lang: str, conn_id: str) -> tuple[str, ConnectionInfo]:
        sid = need_session(request, lang)
        try:
            return sid, await store_of(request).get(_conn_id(conn_id, lang), Sessions.owner(sid))
        except ConnectionNotFound:
            raise PageError(404, "e_404_t", "e_404", lang) from None

    @router.get("/{lang}/connections/{conn_id}")
    async def connection(request: Request, lang: str, conn_id: str) -> HTMLResponse:
        lang = _lang(lang)
        sid, info = await own(request, lang, conn_id)
        await store_of(request).touch(Sessions.owner(sid))
        writes = await store_of(request).writes(info.id, Sessions.owner(sid), 10)
        notice = request.query_params.get("n")
        return page(
            request,
            "connection.html",
            lang,
            c=info,
            writes=writes,
            last=next((w for w in writes if w.ok), None),
            notice=notice if notice in NOTICES else None,
            notice_code=request.query_params.get("code", "")[:40] if notice == "rejected" else "",
        )

    @router.post("/{lang}/connections/{conn_id}/check")
    async def check(request: Request, lang: str, conn_id: str) -> RedirectResponse:
        lang = _lang(lang)
        form = await request.form()
        need_csrf(request, form, lang)
        sid, info = await own(request, lang, conn_id)
        st = store_of(request)
        back = f"/{lang}/connections/{info.id}"
        try:
            write_id = await st.start_write(info.id, Sessions.owner(sid), "test_deal", settings_quota)
        except QuotaExceeded:
            return redirect(f"{back}?n=quota")
        except ConnectionDisabled:
            return redirect(back)
        notice = ""
        try:
            secrets = await st.secrets(info.id)
            async with connectors.adapter(info, secrets, st) as crm:
                deal = await crm.create_deal(DealIn(check_deal_title(lang), tags=("e2e",)))
                url = crm.deal_link(connectors.account_of(info), deal.id)
            await st.finish_write(write_id, ok=True, record_id=deal.id, record_url=url)
            await st.mark(info.id)
        except (CrmError, ConnectionDisabled) as exc:
            code = _error_code(exc) if isinstance(exc, CrmError) else "auth"
            await st.finish_write(write_id, ok=False, error=code)
            if code == "auth":
                await st.mark(info.id, status="reauth_required", error="auth")
                notice = "?n=auth"
            elif code == "unavailable":
                notice = "?n=unavailable"
            else:
                notice = "?" + urlencode({"n": "rejected", "code": code})
            log.info("check of %s failed: %s", info.id, code)
        return redirect(back + notice)

    @router.post("/{lang}/connections/{conn_id}/disable")
    async def disable(request: Request, lang: str, conn_id: str) -> RedirectResponse:
        lang = _lang(lang)
        form = await request.form()
        need_csrf(request, form, lang)
        sid, info = await own(request, lang, conn_id)
        held = await store_of(request).disable(info.id, Sessions.owner(sid))
        if held:
            await connectors.revoke(info.crm, held)
        log.info("disconnected %s %s", info.crm.value, info.id)
        return redirect(f"/{lang}/connections/{info.id}")

    return router


def render_error(templates: Jinja2Templates, request: Request, exc: PageError, env: str) -> HTMLResponse:
    t = texts(exc.lang if exc.lang in LANGS else "ru")
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "t": t,
            "lang": exc.lang,
            "other": "en" if exc.lang == "ru" else "ru",
            "env": env,
            "title": t.get(exc.title, exc.title),
            "text": t.get(exc.text, exc.text).format(**exc.fmt) if exc.fmt else t.get(exc.text, exc.text),
            "path": f"/{exc.lang}",
        },
        status_code=exc.status,
    )
