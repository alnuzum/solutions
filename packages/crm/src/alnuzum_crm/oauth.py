"""OAuth 2.0 of amoCRM, Kommo and Zoho CRM: consent URL, code exchange, refresh.

Every host that receives a secret is checked against the CRM's own domains first: the account
domain and the accounts server come from the browser redirect, which the user controls.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from alnuzum_crm.errors import CrmAuthError, CrmError, CrmUnavailable
from alnuzum_crm.tokens import TokenSet
from alnuzum_crm.types import CrmKind

AMO_SUFFIXES = (".amocrm.ru", ".amocrm.com")
KOMMO_SUFFIXES = (".kommo.com",)
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

ZOHO_ACCOUNTS_HOSTS = frozenset(
    {
        "accounts.zoho.com",
        "accounts.zoho.eu",
        "accounts.zoho.in",
        "accounts.zoho.com.au",
        "accounts.zoho.jp",
        "accounts.zoho.com.cn",
        "accounts.zoho.sa",
        "accounts.zoho.uk",
        "accounts.zohocloud.ca",
    }
)
ZOHO_API_HOSTS = frozenset(
    {
        "www.zohoapis.com",
        "www.zohoapis.eu",
        "www.zohoapis.in",
        "www.zohoapis.com.au",
        "www.zohoapis.jp",
        "www.zohoapis.com.cn",
        "www.zohoapis.sa",
        "www.zohoapis.uk",
        "www.zohoapis.ca",
    }
)


def normalize_amo_domain(raw: str, kind: CrmKind = CrmKind.AMOCRM) -> str:
    """ "sub", "sub.amocrm.ru", "https://sub.amocrm.ru/leads" → "sub.amocrm.ru"; anything outside
    the CRM's domains is refused, since the domain is where the server sends the token."""
    suffixes = KOMMO_SUFFIXES if kind is CrmKind.KOMMO else AMO_SUFFIXES
    value = raw.strip().lower()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    value = value.split("/")[0].rstrip(".")
    if value and "." not in value:
        value += suffixes[0]
    for suffix in suffixes:
        if value.endswith(suffix) and _LABEL.match(value[: -len(suffix)]):
            return value
    raise ValueError(f"not a {kind.value} account domain")


def zoho_host(raw: str, allowed: frozenset[str]) -> str:
    """ "https://accounts.zoho.eu" or "accounts.zoho.eu" → "accounts.zoho.eu", only from the list."""
    value = raw.strip().lower()
    parts = urlsplit(value if "://" in value else f"https://{value}")
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https" or parts.port not in (None, 443) or host not in allowed:
        raise ValueError("not a Zoho data-centre host")
    return host


def amo_authorize_url(kind: CrmKind, client_id: str, state: str) -> str:
    host = "www.kommo.com" if kind is CrmKind.KOMMO else "www.amocrm.ru"
    return f"https://{host}/oauth?" + urlencode(
        {"client_id": client_id, "state": state, "mode": "post_message"}
    )


def zoho_authorize_url(client_id: str, redirect_uri: str, state: str, scopes: str) -> str:
    query = {
        "client_id": client_id,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
    }
    return "https://accounts.zoho.com/oauth/v2/auth?" + urlencode(query)


def _error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.reason_phrase or "error"
    if isinstance(data, dict):
        for key in ("hint", "detail", "error_description", "error", "title"):
            if data.get(key):
                return str(data[key])[:300]
    return resp.reason_phrase or "error"


async def _post(
    crm: str, url: str, *, transport: httpx.AsyncBaseTransport | None, **kw: Any
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0, transport=transport, follow_redirects=False) as http:
        try:
            resp = await http.post(url, **kw)
        except httpx.TransportError as exc:
            raise CrmUnavailable(f"token endpoint unreachable ({type(exc).__name__})", crm=crm) from None
    if resp.status_code in (400, 401, 403):
        raise CrmAuthError(f"authorisation refused: {_error(resp)}", crm=crm, status=resp.status_code)
    if resp.status_code != 200:
        raise CrmError(f"token endpoint answered {resp.status_code}", crm=crm, status=resp.status_code)
    data = resp.json()
    if not isinstance(data, dict) or "access_token" not in data:
        # Zoho answers 200 with {"error": "invalid_code"} for a used or expired code
        raise CrmAuthError(f"authorisation refused: {_error(resp)}", crm=crm, status=resp.status_code)
    return data


def _expires(data: dict[str, Any], default: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=int(data.get("expires_in", default)))


async def amo_exchange_code(
    kind: CrmKind,
    domain: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenSet:
    domain = normalize_amo_domain(domain, kind)
    body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    data = await _post(kind.value, f"https://{domain}/oauth2/access_token", transport=transport, json=body)
    return TokenSet(data["access_token"], data["refresh_token"], _expires(data, 86400))


async def amo_refresh(
    kind: CrmKind,
    domain: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    tokens: TokenSet,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenSet:
    """amoCRM refresh tokens are single-use: the answer carries the next one, store it."""
    domain = normalize_amo_domain(domain, kind)
    body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "redirect_uri": redirect_uri,
    }
    data = await _post(kind.value, f"https://{domain}/oauth2/access_token", transport=transport, json=body)
    return TokenSet(data["access_token"], data["refresh_token"], _expires(data, 86400))


async def zoho_exchange_code(
    accounts_server: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[TokenSet, str]:
    """Tokens and the API domain of the user's data centre."""
    host = zoho_host(accounts_server, ZOHO_ACCOUNTS_HOSTS)
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    data = await _post("zoho", f"https://{host}/oauth/v2/token", transport=transport, data=form)
    api = zoho_host(str(data.get("api_domain", "")), ZOHO_API_HOSTS)
    return TokenSet(data["access_token"], data["refresh_token"], _expires(data, 3600)), api


async def zoho_refresh(
    accounts_server: str,
    client_id: str,
    client_secret: str,
    tokens: TokenSet,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenSet:
    """Zoho keeps the refresh token; only the access token (one hour) is new. Zoho allows ten
    new access tokens per ten minutes, so callers keep the token until it expires."""
    host = zoho_host(accounts_server, ZOHO_ACCOUNTS_HOSTS)
    form = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens.refresh_token,
    }
    data = await _post("zoho", f"https://{host}/oauth/v2/token", transport=transport, data=form)
    return TokenSet(data["access_token"], tokens.refresh_token, _expires(data, 3600))
