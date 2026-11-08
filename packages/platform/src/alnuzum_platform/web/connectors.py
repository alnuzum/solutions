"""What it takes to connect each CRM from the browser, and how to reach a stored connection.

- amoCRM: external integration. The window carries only our metadata; amoCRM creates an integration
  in the customer's account and posts its keys to ``/hooks/amocrm/secrets`` before the browser
  returns. Stored: tokens and the keys of that integration.
- Kommo: OAuth with our integration (one per environment). Stored: tokens.
- Zoho CRM: OAuth with our server-based client, the code is exchanged at the data centre named in the
  redirect. Stored: tokens and the accounts server.
- Bitrix24: an inbound webhook pasted by the user. Stored: the webhook address.
- Odoo: address, database, login and API key. Stored: database, login, key.

Every address that came from the browser (amoCRM ``referer``, Zoho ``accounts-server``, the Bitrix24
webhook, the Odoo address) is checked against the CRM's own domains or for a public https address
before anything secret is sent to it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

import httpx

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.base import CRM
from alnuzum_crm.bitrix24 import Bitrix24, parse_webhook
from alnuzum_crm.errors import CrmAuthError, CrmError, CrmPermissionError, CrmValidationError
from alnuzum_crm.http import RetryPolicy
from alnuzum_crm.netguard import check_public_url
from alnuzum_crm.oauth import (
    ZOHO_ACCOUNTS_HOSTS,
    amo_exchange_code,
    amo_refresh,
    normalize_amo_domain,
    zoho_authorize_url,
    zoho_exchange_code,
    zoho_host,
    zoho_refresh,
)
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.tokens import StaticToken, TokenSet, TokenSource
from alnuzum_crm.types import AccountInfo, CrmKind
from alnuzum_crm.zoho import ZohoCRM
from alnuzum_platform.config import Settings

if TYPE_CHECKING:
    from alnuzum_platform.store import ConnectionInfo, ConnectionStore

OAUTH_CRMS = (CrmKind.AMOCRM, CrmKind.KOMMO, CrmKind.ZOHO)
FORM_CRMS = (CrmKind.BITRIX24, CrmKind.ODOO)
ZOHO_SCOPES = "ZohoCRM.modules.ALL,ZohoCRM.settings.ALL,ZohoCRM.org.READ,ZohoCRM.users.READ"
AMO_NAME = "Al-Nuzum"
AMO_DESCRIPTION = "Открытые решения Al-Nuzum: запись сделок, контактов и задач из ваших документов"
QUICK = RetryPolicy(max_retries=1, backoff=0.5)


class LinkError(Exception):
    """A connection could not be made; ``code`` names the message for the user (``i18n`` key)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Linked:
    crm: CrmKind
    endpoint: str
    account: str
    label: str
    secrets: dict[str, str]
    meta: dict[str, str]


def _expires(tokens: TokenSet) -> str:
    return tokens.expires_at.astimezone(UTC).isoformat()


class Connectors:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """``transport`` replaces the network in tests; in service it stays None, and Odoo then goes
        through the SSRF guard."""
        self.settings = settings
        self._transport = transport

    # --- OAuth ---

    def redirect_uri(self, crm: CrmKind) -> str:
        return f"{self.settings.origin}/oauth/{crm.value}/callback"

    def configured(self, crm: CrmKind) -> bool:
        s = self.settings
        if crm is CrmKind.KOMMO:
            return bool(s.kommo_client_id and s.kommo_client_secret.get_secret_value())
        if crm is CrmKind.ZOHO:
            return bool(s.zoho_client_id and s.zoho_client_secret.get_secret_value())
        return crm in (CrmKind.AMOCRM, CrmKind.BITRIX24, CrmKind.ODOO)

    def authorize_url(self, crm: CrmKind, state: str) -> str:
        if crm is CrmKind.AMOCRM:
            # the address amoCRM's own button.min.js builds: slash before "?", scopes[] with literal brackets
            query = urlencode(
                {
                    "state": state,
                    "mode": "popup",
                    "origin": self.settings.origin,
                    "name": AMO_NAME,
                    "description": AMO_DESCRIPTION,
                    "redirect_uri": self.redirect_uri(crm),
                    "secrets_uri": f"{self.settings.origin}/hooks/amocrm/secrets",
                    "logo": f"{self.settings.origin}/static/amocrm-logo.png",
                },
                quote_via=quote,
            )
            return f"https://www.amocrm.ru/oauth/?{query}&scopes[]=crm&scopes[]=notifications"
        if crm is CrmKind.KOMMO:
            query = urlencode({"client_id": self.settings.kommo_client_id, "state": state, "mode": "popup"})
            return f"https://www.kommo.com/oauth?{query}"
        if crm is CrmKind.ZOHO:
            return zoho_authorize_url(
                self.settings.zoho_client_id, self.redirect_uri(crm), state, ZOHO_SCOPES
            )
        raise ValueError(f"{crm.value} does not connect through OAuth")

    async def finish(
        self, crm: CrmKind, params: Mapping[str, str], client: Mapping[str, str] | None
    ) -> Linked:
        """Exchange the code from the redirect and find out which account it reaches."""
        code = params.get("code", "")
        if not code:
            raise LinkError("e_oauth")
        try:
            if crm is CrmKind.ZOHO:
                return await self._finish_zoho(code, params)
            return await self._finish_amo(crm, code, params, client)
        except (CrmError, ValueError):
            raise LinkError("e_oauth") from None

    async def _finish_amo(
        self, crm: CrmKind, code: str, params: Mapping[str, str], client: Mapping[str, str] | None
    ) -> Linked:
        domain = normalize_amo_domain(params.get("referer", ""), crm)
        if crm is CrmKind.AMOCRM:
            # the keys came to /hooks/amocrm/secrets for this very window, and the redirect names them
            if not client or params.get("client_id") != client.get("client_id"):
                raise LinkError("e_oauth")
            client_id, client_secret = client["client_id"], client["client_secret"]
        else:
            client_id = self.settings.kommo_client_id
            client_secret = self.settings.kommo_client_secret.get_secret_value()
        tokens = await amo_exchange_code(
            crm, domain, client_id, client_secret, self.redirect_uri(crm), code, transport=self._transport
        )
        cls = Kommo if crm is CrmKind.KOMMO else AmoCRM
        async with cls(
            domain, StaticToken(tokens.access_token, crm=crm.value), transport=self._transport, retry=QUICK
        ) as adapter:
            account = await adapter.account()
        secrets = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": _expires(tokens),
        }
        if crm is CrmKind.AMOCRM:
            secrets |= {"client_id": client_id, "client_secret": client_secret}
        return Linked(crm, domain, account.id, account.name or domain, secrets, {"web": account.web})

    async def _finish_zoho(self, code: str, params: Mapping[str, str]) -> Linked:
        server = zoho_host(params.get("accounts-server") or "accounts.zoho.com", ZOHO_ACCOUNTS_HOSTS)
        tokens, api = await zoho_exchange_code(
            server,
            self.settings.zoho_client_id,
            self.settings.zoho_client_secret.get_secret_value(),
            self.redirect_uri(CrmKind.ZOHO),
            code,
            transport=self._transport,
        )
        async with ZohoCRM(
            api, StaticToken(tokens.access_token, crm="zoho"), transport=self._transport, retry=QUICK
        ) as adapter:
            account = await adapter.account()
        secrets = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": _expires(tokens),
            "accounts_server": server,
        }
        return Linked(CrmKind.ZOHO, api, account.id, account.name or api, secrets, {"web": account.web})

    # --- forms ---

    async def link_bitrix24(self, webhook_url: str) -> Linked:
        url = webhook_url.strip()
        try:
            host, _ = parse_webhook(url)
        except ValueError:
            raise LinkError("e_webhook") from None
        try:
            async with Bitrix24(url, transport=self._transport, retry=QUICK) as adapter:
                account = await adapter.account()
        except (CrmAuthError, CrmPermissionError, CrmValidationError):
            raise LinkError("e_webhook_refused") from None
        except CrmError:
            raise LinkError("e_unreachable") from None
        return Linked(CrmKind.BITRIX24, host, account.id, host, {"webhook_url": url}, {"web": account.web})

    async def link_odoo(self, url: str, database: str, login: str, api_key: str) -> Linked:
        try:
            clean = (await check_public_url(url.strip())) if self._transport is None else url.strip()
            async with Odoo(
                clean,
                database.strip(),
                login.strip(),
                api_key.strip(),
                transport=self._transport,
                retry=QUICK,
            ) as adapter:
                account = await adapter.account()
        except CrmAuthError:
            raise LinkError("e_odoo_refused") from None
        except CrmError:
            raise LinkError("e_unreachable") from None
        except (ValueError, OSError):
            # not https, a private address, or a name that does not resolve
            raise LinkError("e_odoo_url") from None
        secrets = {"database": database.strip(), "login": login.strip(), "api_key": api_key.strip()}
        return Linked(
            CrmKind.ODOO,
            clean,
            account.id,
            f"{database.strip()} · {login.strip()}",
            secrets,
            {"web": account.web},
        )

    # --- stored connections ---

    def adapter(self, info: "ConnectionInfo", secrets: Mapping[str, str], store: "ConnectionStore") -> CRM:
        crm = info.crm
        if crm in (CrmKind.AMOCRM, CrmKind.KOMMO):
            tokens: TokenSource = StaticToken(secrets["access_token"], crm=crm.value)
            if secrets.get("refresh_token"):
                if crm is CrmKind.AMOCRM:
                    cid, csecret = secrets.get("client_id", ""), secrets.get("client_secret", "")
                else:
                    cid = self.settings.kommo_client_id
                    csecret = self.settings.kommo_client_secret.get_secret_value()
                redirect = self.redirect_uri(crm)

                async def amo_refresher(current: TokenSet) -> TokenSet:
                    return await amo_refresh(
                        crm, info.endpoint, cid, csecret, redirect, current, transport=self._transport
                    )

                tokens = store.token_source(info.id, amo_refresher)
            cls = Kommo if crm is CrmKind.KOMMO else AmoCRM
            return cls(info.endpoint, tokens, transport=self._transport)
        if crm is CrmKind.ZOHO:
            server = secrets["accounts_server"]

            async def zoho_refresher(current: TokenSet) -> TokenSet:
                return await zoho_refresh(
                    server,
                    self.settings.zoho_client_id,
                    self.settings.zoho_client_secret.get_secret_value(),
                    current,
                    transport=self._transport,
                )

            return ZohoCRM(
                info.endpoint, store.token_source(info.id, zoho_refresher), transport=self._transport
            )
        if crm is CrmKind.BITRIX24:
            return Bitrix24(secrets["webhook_url"], transport=self._transport)
        return Odoo(
            info.endpoint,
            secrets["database"],
            secrets["login"],
            secrets["api_key"],
            transport=self._transport,
        )

    @staticmethod
    def account_of(info: "ConnectionInfo") -> AccountInfo:
        return AccountInfo(info.account, info.label, str(info.meta.get("web", "")))

    async def revoke(self, crm: CrmKind, secrets: Mapping[str, str]) -> bool:
        """Give the token back where the CRM allows it (Zoho: a user has at most 20 refresh tokens)."""
        if crm is not CrmKind.ZOHO or not secrets.get("refresh_token"):
            return False
        try:
            server = zoho_host(secrets.get("accounts_server", ""), ZOHO_ACCOUNTS_HOSTS)
            async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as http:
                resp = await http.post(
                    f"https://{server}/oauth/v2/token/revoke", params={"token": secrets["refresh_token"]}
                )
        except (httpx.HTTPError, ValueError):
            return False
        return resp.status_code == 200


def check_deal_title(lang: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    if lang == "ru":
        return f"Al-Nuzum: проверка подключения · e2e · {stamp}"
    return f"Al-Nuzum: connection check · e2e · {stamp}"
