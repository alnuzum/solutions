"""Answers of every CRM map to the same error types, and a refused token is refreshed once."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from alnuzum_crm import errors as e
from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.http import RetryPolicy
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.tokens import StaticToken
from alnuzum_crm.types import CompanyIn, DealIn, DealLine, FileIn
from alnuzum_crm.zoho import ZohoCRM

NO_RETRY = RetryPolicy(max_retries=0)
Answer = httpx.Response | Callable[[httpx.Request], httpx.Response]


class Script:
    def __init__(self, *answers: Answer) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.answers.pop(0)
        return item(request) if callable(item) else item

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class Rotating:
    """A token source whose refresh hands out the next token."""

    def __init__(self) -> None:
        self.current = "t1"
        self.refreshes = 0

    async def token(self) -> str:
        return self.current

    async def refresh(self, stale: str) -> str:
        self.refreshes += 1
        self.current = f"t{self.refreshes + 1}"
        return self.current


def amo(script: Script, tokens: Any = None) -> AmoCRM:
    return AmoCRM(
        "acme",
        tokens or StaticToken("t", crm="amocrm"),
        rate=1000,
        retry=NO_RETRY,
        transport=script.transport(),
    )


async def test_amo_refreshes_once_after_401() -> None:
    tokens = Rotating()
    script = Script(
        httpx.Response(401, json={"title": "Unauthorized"}),
        httpx.Response(200, json={"_embedded": {"companies": []}}),
    )
    async with amo(script, tokens) as crm:
        assert await crm.find_company(CompanyIn("Acme")) is None
    assert tokens.refreshes == 1
    assert script.requests[1].headers["authorization"] == "Bearer t2"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, e.CrmAuthError),
        (402, e.CrmPermissionError),
        (403, e.CrmPermissionError),
        (404, e.CrmNotFound),
        (405, e.CrmUnsupported),
        (429, e.CrmRateLimited),
        (502, e.CrmUnavailable),
        (400, e.CrmValidationError),
    ],
)
async def test_amo_status_mapping(status: int, error: type[e.CrmError]) -> None:
    body = {"title": "Bad", "detail": "why", "validation-errors": [{"path": "name"}]}
    async with amo(Script(httpx.Response(status, json=body), httpx.Response(status, json=body))) as crm:
        with pytest.raises(error) as err:
            await crm.find_company(CompanyIn("Acme"))
    assert err.value.status == status
    assert err.value.crm == "amocrm"


@pytest.mark.parametrize(
    "detail",
    [
        "Код ошибки 205. В случае повторного возникновения ошибки, обращайтесь в нашу техническую поддержку",
        "Error code 205. If error appears again, you can write to our support team",
        "Payment Required",
    ],
)
async def test_amo_account_over_its_limit_is_a_permission_error(detail: str) -> None:
    body = {"title": "Bad Request", "status": 400, "detail": detail}
    async with amo(Script(httpx.Response(400, json=body))) as crm:
        with pytest.raises(e.CrmPermissionError) as err:
            await crm.create_company(CompanyIn("Acme"))
    assert err.value.code == "account_limit"
    assert err.value.status == 400


async def test_amo_error_text_without_json_and_missing_records() -> None:
    async with amo(
        Script(httpx.Response(400, text="<html>"), httpx.Response(204), httpx.Response(204))
    ) as crm:
        with pytest.raises(e.CrmValidationError):
            await crm.find_company(CompanyIn("Acme"))
        with pytest.raises(e.CrmNotFound):
            await crm.get_deal("5")
        with pytest.raises(e.CrmNotFound):
            await crm.deal_history(company_id="5")
        with pytest.raises(ValueError, match="exactly one"):
            await crm.deal_history(company_id="1", contact_id="2")


async def test_amo_without_catalog_and_with_a_foreign_drive() -> None:
    no_catalog = Script(httpx.Response(200, json={"_embedded": {"catalogs": [{"id": 1, "type": "regular"}]}}))
    async with amo(no_catalog) as crm:
        with pytest.raises(e.CrmUnsupported, match="catalog"):
            await crm.list_products()
    foreign_drive = Script(httpx.Response(200, json={"drive_url": "https://drive.evil.example"}))
    async with amo(foreign_drive) as crm:
        with pytest.raises(e.CrmUnsupported, match="file storage"):
            await crm.attach_file("1", FileIn("a.txt", b"x"))
    foreign_upload = Script(
        httpx.Response(200, json={"drive_url": "https://drive-b.amocrm.ru"}),
        httpx.Response(200, json={"upload_url": "https://evil.example/upload/x", "max_part_size": 10}),
    )
    async with amo(foreign_upload) as crm:
        with pytest.raises(e.CrmError, match="upload address"):
            await crm.attach_file("1", FileIn("a.txt", b"x"))
    assert foreign_upload.requests[-1].url.host == "drive-b.amocrm.ru"


HOOK = "https://acme.bitrix24.ru/rest/1/supersecret123/"


def b24(script: Script) -> Bitrix24:
    return Bitrix24(
        HOOK, rate=1000, retry=RetryPolicy(max_retries=1, backoff=0.001), transport=script.transport()
    )


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (
            401,
            {"error": "INVALID_CREDENTIALS", "error_description": "Invalid request credentials"},
            e.CrmAuthError,
        ),
        (401, {"error": "expired_token"}, e.CrmAuthError),
        (403, {"error": "insufficient_scope"}, e.CrmPermissionError),
        (401, {"error": "insufficient_scope"}, e.CrmPermissionError),
        (404, {"error": "ERROR_METHOD_NOT_FOUND"}, e.CrmUnsupported),
        (400, {"error": "", "error_description": "Not found"}, e.CrmNotFound),
        (400, {"error": "ERROR_CORE", "error_description": "bad field"}, e.CrmValidationError),
        (500, {"error": "INTERNAL_SERVER_ERROR"}, e.CrmUnavailable),
        (200, {"error": "OPERATION_TIME_LIMIT"}, e.CrmValidationError),
    ],
)
async def test_bitrix_error_mapping_never_shows_the_webhook(
    status: int, body: dict[str, str], error: type[e.CrmError]
) -> None:
    async with b24(Script(httpx.Response(status, json=body), httpx.Response(status, json=body))) as crm:
        with pytest.raises(error) as err:
            await crm.get_deal("1")
    assert "supersecret123" not in str(err.value)
    assert "supersecret123" not in repr(crm)


async def test_bitrix_limit_is_retried_then_reported() -> None:
    limit = httpx.Response(
        503, json={"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many requests"}
    )
    ok = httpx.Response(
        200,
        json={"result": [{"ID": "1", "NAME": "P", "PRICE": "5", "CURRENCY_ID": "RUB", "XML_ID": "SKU-1"}]},
    )
    catalogs = httpx.Response(200, json={"result": {"catalogs": [{"iblockId": 25, "productIblockId": None}]}})
    stock = httpx.Response(200, json={"result": {"products": [{"id": 1, "quantity": 4}]}})
    async with b24(Script(limit, ok, catalogs, stock)) as crm:
        page = await crm.list_products()
    assert page.items[0].stock == Decimal(4)
    assert page.items[0].sku == "SKU-1"
    assert page.items[0].price == Decimal(5)
    async with b24(Script(limit, limit)) as crm:
        with pytest.raises(e.CrmRateLimited):
            await crm.list_products()


async def test_bitrix_redirect_non_json_and_bad_method() -> None:
    async with b24(
        Script(
            httpx.Response(302, headers={"Location": "https://x"}),
            httpx.Response(502, text="gw"),
            httpx.Response(502, text="gw"),
            httpx.Response(200, text="<html>"),
        )
    ) as crm:
        with pytest.raises(e.CrmError, match="redirect"):
            await crm.call("profile")
        with pytest.raises(e.CrmUnavailable):
            await crm.call("profile")
        with pytest.raises(e.CrmError, match="without JSON"):
            await crm.call("profile")
        with pytest.raises(ValueError, match="method"):
            await crm.call("../profile")


def zoho(script: Script, tokens: Any = None) -> ZohoCRM:
    return ZohoCRM(
        "www.zohoapis.com",
        tokens or StaticToken("t", crm="zoho"),
        rate=1000,
        retry=NO_RETRY,
        transport=script.transport(),
    )


async def test_zoho_refreshes_invalid_token_once() -> None:
    tokens = Rotating()
    script = Script(httpx.Response(401, json={"code": "INVALID_TOKEN"}), httpx.Response(204))
    async with zoho(script, tokens) as crm:
        with pytest.raises(e.CrmNotFound):
            await crm.get_deal("1")
    assert script.requests[1].headers["authorization"] == "Zoho-oauthtoken t2"


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"code": "OAUTH_SCOPE_MISMATCH", "message": "invalid oauth scope"}, e.CrmPermissionError),
        (401, {"code": "INVALID_TOKEN"}, e.CrmAuthError),
        (403, {"code": "NO_PERMISSION"}, e.CrmPermissionError),
        (404, {"code": "INVALID_URL_PATTERN"}, e.CrmNotFound),
        (429, {"code": "TOO_MANY_REQUESTS"}, e.CrmRateLimited),
        (500, {"code": "INTERNAL_ERROR"}, e.CrmUnavailable),
        (400, {"code": "INVALID_DATA"}, e.CrmValidationError),
    ],
)
async def test_zoho_error_mapping(status: int, body: dict[str, str], error: type[e.CrmError]) -> None:
    async with zoho(Script(httpx.Response(status, json=body))) as crm:
        with pytest.raises(error):
            await crm.add_note("1", "x")


async def test_zoho_record_level_error_and_non_json() -> None:
    bad = {
        "data": [
            {
                "status": "error",
                "code": "MANDATORY_NOT_FOUND",
                "message": "required field not found",
                "details": {"api_name": "Last_Name"},
            }
        ]
    }
    async with zoho(Script(httpx.Response(201, json=bad), httpx.Response(500, text="oops"))) as crm:
        with pytest.raises(e.CrmValidationError, match="Last_Name"):
            await crm.add_note("1", "x")
        with pytest.raises(e.CrmUnavailable):
            await crm.add_note("1", "x")


def rpc_error(name: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": 200,
                "message": "Odoo Server Error",
                "data": {"name": name, "message": "why", "debug": "Traceback secret"},
            },
        },
    )


def odoo(script: Script) -> Odoo:
    return Odoo(
        "https://odoo.example.com",
        "db",
        "u",
        "key",
        trusted_url=True,
        rate=1000,
        retry=NO_RETRY,
        transport=script.transport(),
    )


AUTH_OK = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 2})


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (rpc_error("odoo.exceptions.AccessError"), e.CrmPermissionError),
        (rpc_error("odoo.exceptions.MissingError"), e.CrmNotFound),
        (rpc_error("odoo.exceptions.ValidationError"), e.CrmValidationError),
        (rpc_error("odoo.exceptions.UserError"), e.CrmValidationError),
        (rpc_error("builtins.KeyError"), e.CrmError),
        (httpx.Response(404), e.CrmUnsupported),
        (httpx.Response(503), e.CrmUnavailable),
        (httpx.Response(303), e.CrmError),
        (httpx.Response(200, text="<html>"), e.CrmError),
    ],
)
async def test_odoo_error_mapping(answer: httpx.Response, error: type[e.CrmError]) -> None:
    async with odoo(Script(AUTH_OK, answer)) as crm:
        with pytest.raises(error) as err:
            await crm.add_note("1", "x")
    assert "Traceback" not in str(err.value)


async def test_odoo_login_refused() -> None:
    async with odoo(Script(httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": False}))) as crm:
        with pytest.raises(e.CrmAuthError):
            await crm.add_note("1", "x")
    async with odoo(Script(rpc_error("odoo.exceptions.AccessDenied"))) as crm:
        with pytest.raises(e.CrmAuthError):
            await crm.add_note("1", "x")
    assert "key" not in repr(crm)


async def test_odoo_lines_need_sale_crm_and_a_customer() -> None:
    no_field = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})
    has_field = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [7]})
    line = DealLine("x", Decimal(1), Decimal(1))
    async with odoo(Script(AUTH_OK, no_field)) as crm:
        with pytest.raises(e.CrmUnsupported, match="sale_crm"):
            await crm.create_deal(DealIn("d", (line,), company_id="1"))
    async with odoo(Script(AUTH_OK, has_field)) as crm:
        with pytest.raises(e.CrmValidationError, match="customer"):
            await crm.create_deal(DealIn("d", (line,)))
