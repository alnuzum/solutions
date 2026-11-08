"""``account()`` (the liveness probe of a connection) and ``deal_link()`` of every adapter."""

import json

import httpx
import pytest

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.base import CRM
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.errors import CrmAuthError, CrmUnsupported
from alnuzum_crm.http import RetryPolicy
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.tokens import StaticToken
from alnuzum_crm.types import AccountInfo
from alnuzum_crm.zoho import ZohoCRM

NO_RETRY = RetryPolicy(max_retries=0)


def transport(answer: httpx.Response, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer

    return httpx.MockTransport(handle)


async def test_amocrm_and_kommo_account() -> None:
    for cls, domain in ((AmoCRM, "acme.amocrm.ru"), (Kommo, "acme.kommo.com")):
        seen: list[httpx.Request] = []
        crm = cls(
            domain,
            StaticToken("t", crm="x"),
            transport=transport(httpx.Response(200, json={"id": 7, "name": "Acme"}), seen),
        )
        first = await crm.account()
        await crm.account()  # asks again: a probe must not answer from cache
        assert first == AccountInfo("7", "Acme", f"https://{domain}")
        assert [r.url.path for r in seen] == ["/api/v4/account", "/api/v4/account"]
        assert crm.deal_link(first, "55") == f"https://{domain}/leads/detail/55"
        with pytest.raises(ValueError, match="digits"):
            crm.deal_link(first, "55/../../settings")


async def test_amocrm_refused_token_is_an_auth_error() -> None:
    crm = AmoCRM(
        "acme.amocrm.ru",
        StaticToken("t", crm="amocrm"),
        transport=transport(httpx.Response(401, json={"title": "Unauthorized"}), []),
    )
    with pytest.raises(CrmAuthError):
        await crm.account()


async def test_bitrix24_account_proves_the_crm_scope() -> None:
    seen: list[httpx.Request] = []
    crm = Bitrix24(
        "https://acme.bitrix24.ru/rest/1/abcdefghijklmnop/",
        transport=transport(httpx.Response(200, json={"result": [], "total": 0}), seen),
        retry=NO_RETRY,
    )
    info = await crm.account()
    assert info == AccountInfo("acme.bitrix24.ru", "acme.bitrix24.ru", "https://acme.bitrix24.ru")
    assert seen[0].url.path.endswith("/crm.deal.list.json")
    assert json.loads(seen[0].content)["filter"] == {"ID": 0}
    assert crm.deal_link(info, "12") == "https://acme.bitrix24.ru/crm/deal/details/12/"
    denied = Bitrix24(
        "https://acme.bitrix24.ru/rest/1/abcdefghijklmnop/",
        transport=transport(
            httpx.Response(
                401, json={"error": "INVALID_CREDENTIALS", "error_description": "Invalid request credentials"}
            ),
            [],
        ),
        retry=NO_RETRY,
    )
    with pytest.raises(CrmAuthError):
        await denied.account()


@pytest.mark.parametrize(
    ("api", "web"),
    [
        ("www.zohoapis.com", "crm.zoho.com"),
        ("www.zohoapis.eu", "crm.zoho.eu"),
        ("www.zohoapis.ca", "crm.zohocloud.ca"),
    ],
)
async def test_zoho_account_and_link_per_data_centre(api: str, web: str) -> None:
    seen: list[httpx.Request] = []
    org = {"org": [{"id": "9001", "domain_name": "org9001", "company_name": "Acme"}]}
    crm = ZohoCRM(api, StaticToken("t", crm="zoho"), transport=transport(httpx.Response(200, json=org), seen))
    info = await crm.account()
    assert info == AccountInfo("9001", "Acme", f"https://{web}/crm/org9001")
    assert seen[0].url.path == "/crm/v8/org"
    assert crm.deal_link(info, "123") == f"https://{web}/crm/org9001/tab/Potentials/123"


async def test_zoho_account_survives_an_odd_answer() -> None:
    crm = ZohoCRM(
        "www.zohoapis.com",
        StaticToken("t", crm="zoho"),
        transport=transport(httpx.Response(200, json={"org": []}), []),
    )
    assert (await crm.account()).id == ""


async def test_odoo_account_authenticates_every_time() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": 2})

    crm = Odoo("https://odoo.example.com", "my db", "me", "key", transport=httpx.MockTransport(handle))
    info = await crm.account()
    await crm.account()
    assert info == AccountInfo("https://odoo.example.com|my db|2", "my db", "https://odoo.example.com")
    assert len(seen) == 2
    assert (
        crm.deal_link(info, "8")
        == "https://odoo.example.com/web?db=my%20db#id=8&model=crm.lead&view_type=form"
    )


async def test_odoo_refused_key() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": False}
        )

    crm = Odoo("https://odoo.example.com", "db", "me", "bad", transport=httpx.MockTransport(handle))
    with pytest.raises(CrmAuthError):
        await crm.account()


async def test_base_adapter_without_account_support() -> None:
    class Bare(Bitrix24):
        pass

    with pytest.raises(CrmUnsupported):
        await CRM.account(Bare("https://acme.bitrix24.ru/rest/1/abcdefghijklmnop/"))
    with pytest.raises(CrmUnsupported):
        CRM.deal_link(
            Bare("https://acme.bitrix24.ru/rest/1/abcdefghijklmnop/"), AccountInfo("a", "b", "c"), "1"
        )
