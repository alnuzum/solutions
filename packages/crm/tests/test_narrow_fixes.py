"""Probes of the narrow review (I1-I5, M1, M4): each was red before its fix."""

import asyncio
import json
import socket
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpcore
import httpx
import pytest

from alnuzum_crm import netguard
from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.errors import CrmUnsupported
from alnuzum_crm.http import RetryPolicy
from alnuzum_crm.tokens import StaticToken
from alnuzum_crm.types import CompanyIn, ContactIn, DealIn, DealLine, FileIn, Ref, TaskIn
from alnuzum_crm.zoho import ZohoCRM

from .odoo_fake import FakeOdoo
from .test_odoo_matching import _odoo

NO_RETRY = RetryPolicy(max_retries=0)


class Router:
    """Answers by (method, path) or by Bitrix24 REST method; records every request."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for key, answer in self.routes.items():
            method, _, prefix = key.partition(" ")
            if request.method == method and (path == prefix or path.endswith(prefix)):
                value = answer(request) if callable(answer) else answer
                if value is None:
                    return httpx.Response(204)
                return httpx.Response(200, json=value)
        raise AssertionError(f"unexpected {request.method} {request.url}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def sent(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


def query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


# --- I1: every vetted address is tried, IPv4 first ---


def _dns(monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
    loop = asyncio.get_running_loop()

    async def fake(host: Any, port: Any, *a: Any, **kw: Any) -> list[Any]:
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in ips
        ]

    monkeypatch.setattr(loop, "getaddrinfo", fake)


class FakeInner:
    def __init__(self, reachable: set[str]) -> None:
        self.reachable = reachable
        self.tried: list[str] = []

    async def connect_tcp(self, host: str, port: int, **kw: Any) -> str:
        self.tried.append(host)
        if host not in self.reachable:
            raise httpcore.ConnectError("[Errno 101] Network is unreachable")
        return f"stream:{host}"


async def test_guard_falls_back_to_the_next_vetted_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, ["2a01:4f9:3051:52aa::2", "65.109.84.172", "65.109.84.173"])
    backend = netguard._GuardedBackend()
    inner = FakeInner({"65.109.84.173"})
    backend._inner = inner  # type: ignore[assignment]
    stream: object = await backend.connect_tcp("odoo.example.com", 443)
    assert stream == "stream:65.109.84.173"
    assert inner.tried == ["65.109.84.172", "65.109.84.173"]


async def test_guard_reports_failure_after_every_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _dns(monkeypatch, ["2a01:4f9:3051:52aa::2", "65.109.84.172"])
    backend = netguard._GuardedBackend()
    inner = FakeInner(set())
    backend._inner = inner  # type: ignore[assignment]
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("odoo.example.com", 443)
    assert inner.tried == ["65.109.84.172", "2a01:4f9:3051:52aa::2"]


# --- I2: record ids that go into a URL path are digits only ---

BAD_IDS = ["1/../../settings/users?type=AdminUsers&x=", "1?x=1", "abc", "", "1 2", "-1", "١٢"]


@pytest.mark.parametrize("bad", BAD_IDS)
async def test_zoho_refuses_non_numeric_ids_before_any_request(bad: str) -> None:
    router = Router({})
    crm = ZohoCRM(
        "www.zohoapis.com",
        StaticToken("t", crm="zoho"),
        rate=1000,
        retry=NO_RETRY,
        transport=router.transport(),
    )
    async with crm:
        for make in (
            lambda: crm.get_deal(bad),
            lambda: crm.attach_file(bad, FileIn("a.txt", b"x")),
            lambda: crm.add_note(bad, "x"),
            lambda: crm.add_task(bad, TaskIn("x", datetime.now(UTC))),
            lambda: crm.deal_history(company_id=bad),
            lambda: crm.deal_history(contact_id=bad),
        ):
            with pytest.raises(ValueError, match=r"record id|exactly one"):
                await make()
    assert router.requests == []


# --- I3: a name match with another INN is another legal entity ---

AMO_FIELDS = {"_embedded": {"custom_fields": [{"id": 9, "code": "INN", "name": "ИНН"}]}}


def amo(routes: dict[str, Any]) -> tuple[AmoCRM, Router]:
    router = Router(routes)
    crm = AmoCRM(
        "acme", StaticToken("t", crm="amocrm"), rate=1000, retry=NO_RETRY, transport=router.transport()
    )
    return crm, router


def amo_company(cid: int, name: str, inn: str | None) -> dict[str, Any]:
    fields = [{"field_id": 9, "values": [{"value": inn}]}] if inn else None
    return {"id": cid, "name": name, "custom_fields_values": fields}


def amo_search(rows_by_query: dict[str, list[dict[str, Any]]]) -> Any:
    def answer(request: httpx.Request) -> Any:
        rows = rows_by_query.get(query(request)["query"][0], [])
        return {"_embedded": {"companies": rows}} if rows else None

    return answer


async def test_amo_name_match_with_another_inn_is_not_reused() -> None:
    crm, _ = amo(
        {
            "GET /api/v4/companies/custom_fields": AMO_FIELDS,
            "GET /api/v4/companies": amo_search(
                {"ООО Ромашка": [amo_company(100, "ООО Ромашка", "7701111111")]}
            ),
        }
    )
    async with crm:
        assert await crm.find_company(CompanyIn("ООО Ромашка", inn="7702222222")) is None
        assert await crm.find_company(CompanyIn("ООО Ромашка")) == "100"


async def test_amo_name_match_without_inn_is_reused() -> None:
    crm, _ = amo(
        {
            "GET /api/v4/companies/custom_fields": AMO_FIELDS,
            "GET /api/v4/companies": amo_search({"ООО Ромашка": [amo_company(101, "ООО Ромашка", None)]}),
        }
    )
    async with crm:
        assert await crm.find_company(CompanyIn("ООО Ромашка", inn="7702222222")) == "101"


def bitrix(handler: Any) -> Bitrix24:
    return Bitrix24(
        "https://acme.bitrix24.ru/rest/1/abcdef123456/",
        rate=1000,
        retry=NO_RETRY,
        transport=httpx.MockTransport(handler),
    )


def b24_method(request: httpx.Request) -> tuple[str, dict[str, Any]]:
    return request.url.path.rsplit("/", 1)[1].removesuffix(".json"), json.loads(request.content or b"{}")


async def test_bitrix_name_match_with_another_inn_is_not_reused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method, body = b24_method(request)
        if method == "crm.requisite.list" and "RQ_INN" in body["filter"]:
            return httpx.Response(200, json={"result": []})
        if method == "crm.requisite.list":
            return httpx.Response(200, json={"result": [{"ENTITY_ID": "1", "RQ_INN": "7701111111"}]})
        if method == "crm.company.list":
            return httpx.Response(200, json={"result": [{"ID": "1", "TITLE": "ООО Ромашка"}]})
        raise AssertionError(method)

    async with bitrix(handler) as crm:
        assert await crm.find_company(CompanyIn("ООО Ромашка", inn="7702222222")) is None


async def test_zoho_name_match_with_another_inn_is_not_reused() -> None:
    def search(request: httpx.Request) -> Any:
        crit = query(request)["criteria"][0]
        return (
            None
            if crit.startswith("(INN")
            else {"data": [{"id": "10", "Account_Name": "Acme", "INN": "7701111111"}]}
        )

    router = Router(
        {
            "GET /settings/fields": {
                "fields": [{"api_name": "INN", "field_label": "INN", "custom_field": True}]
            },
            "GET /Accounts/search": search,
        }
    )
    crm = ZohoCRM(
        "www.zohoapis.com",
        StaticToken("t", crm="zoho"),
        rate=1000,
        retry=NO_RETRY,
        transport=router.transport(),
    )
    async with crm:
        assert await crm.find_company(CompanyIn("Acme", inn="7702222222")) is None


async def test_odoo_name_match_with_another_inn_is_not_reused_and_inn_is_exact() -> None:
    fake = FakeOdoo(
        {
            "res.partner": [
                {"id": 5, "name": "Acme Co", "is_company": True, "vat": "7701111111", "email": False},
                {"id": 7, "name": "Beta", "is_company": True, "vat": "RU7701234567", "email": False},
            ]
        }
    )
    async with _odoo(fake) as crm:
        assert await crm.find_company(CompanyIn("Acme Co", inn="7702222222")) is None
        assert await crm.find_company(CompanyIn("Acme Co", inn="7701111111")) == "5"
        assert await crm.find_company(CompanyIn("Nobody", inn="01234567")) is None  # M1: no suffix match
        assert await crm.find_company(CompanyIn("Nobody", inn="7701234567")) == "7"


# --- I4: amoCRM keeps one price per product and one currency per account ---


def amo_catalog(price: int = 100) -> dict[str, Any]:
    return {
        "GET /api/v4/catalogs": {"_embedded": {"catalogs": [{"id": 5, "type": "products"}]}},
        "GET /api/v4/catalogs/5/custom_fields": {
            "_embedded": {"custom_fields": [{"id": 77, "code": "PRICE"}]}
        },
        "GET /api/v4/catalogs/5/elements": {
            "_embedded": {
                "elements": [
                    {
                        "id": 42,
                        "name": "Widget",
                        "custom_fields_values": [{"field_code": "PRICE", "values": [{"value": price}]}],
                    }
                ]
            }
        },
        "GET /api/v4/account": {"currency": "UZS", "drive_url": "https://drive-b.amocrm.ru"},
        "POST /api/v4/leads": {"_embedded": {"leads": [{"id": 1}]}},
        "POST /api/v4/leads/1/link": {"_embedded": {"links": []}},
        "PATCH /api/v4/leads/1": {"id": 1},
    }


@pytest.mark.parametrize(
    ("deal", "error"),
    [
        (DealIn("d", (DealLine("Widget", Decimal(2), Decimal(80), product_id="42"),)), "catalog price"),
        (
            DealIn(
                "d",
                (
                    DealLine("Widget", Decimal(1), Decimal(100), product_id="42"),
                    DealLine("Widget", Decimal(2), Decimal(100), product_id="42"),
                ),
            ),
            "once",
        ),
        (
            DealIn("d", (DealLine("Widget", Decimal(1), Decimal(100), product_id="42"),), currency="USD"),
            "currency",
        ),
    ],
)
async def test_amo_refuses_what_it_cannot_keep(deal: DealIn, error: str) -> None:
    crm, router = amo(amo_catalog())
    async with crm:
        with pytest.raises(CrmUnsupported, match=error):
            await crm.create_deal(deal)
    assert router.sent("POST", "/api/v4/leads") == []


async def test_amo_line_at_catalog_price_in_account_currency_passes() -> None:
    crm, router = amo(amo_catalog())
    async with crm:
        created = await crm.create_deal(
            DealIn("d", (DealLine("Widget", Decimal(2), Decimal(100), product_id="42"),), currency="UZS")
        )
    assert created.id == "1"
    assert (
        json.loads(router.sent("POST", "/api/v4/leads/1/link")[0].content)[0]["metadata"]["quantity"] == 2.0
    )


async def test_amo_budget_is_the_given_total_while_lines_keep_catalog_prices() -> None:
    """Discounts amoCRM cannot keep on a line live in the budget: lines at the catalog price, the
    budget is the total after the company's rules."""
    crm, router = amo(amo_catalog())
    line = DealLine("Widget", Decimal(3), Decimal(100), product_id="42")
    async with crm:
        await crm.create_deal(DealIn("d", (line,), total=Decimal("271.50")))
    assert json.loads(router.sent("POST", "/api/v4/leads")[0].content)[0]["price"] == 272
    # the link recounts the budget at catalog prices, so the total is put back after it
    assert json.loads(router.sent("PATCH", "/api/v4/leads/1")[0].content) == {"price": 272}
    assert router.requests[-1].method == "PATCH"


async def test_odoo_currency_is_applied_or_refused() -> None:
    fake = FakeOdoo(
        {
            "ir.model.fields": [{"id": 1, "model": "sale.order", "name": "opportunity_id"}],
            "res.currency": [{"id": 1, "name": "USD"}, {"id": 2, "name": "EUR"}],
            "res.company": [{"id": 1, "currency_id": [1, "USD"]}],
            "product.pricelist": [],
        }
    )
    line = DealLine("x", Decimal(1), Decimal(1), product_id="3")
    async with _odoo(fake) as crm:
        with pytest.raises(CrmUnsupported, match="EUR"):
            await crm.create_deal(DealIn("d", (line,), company_id="1", currency="EUR"))
        with pytest.raises(CrmUnsupported, match="XXX"):
            await crm.create_deal(DealIn("d", (line,), company_id="1", currency="XXX"))
    assert not [c for c in fake.calls if c[1] == "create"]


# --- I5: every page is read ---


async def test_bitrix_products_cursor_does_not_skip_rows() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method, body = b24_method(request)
        if method == "catalog.catalog.list":
            return httpx.Response(
                200, json={"result": {"catalogs": [{"iblockId": 25, "productIblockId": None}]}}
            )
        if method == "catalog.product.list":
            return httpx.Response(200, json={"result": {"products": []}})
        start = int(body["start"])
        starts.append(start)
        rows = [{"ID": str(i), "NAME": f"P{i}"} for i in range(start, min(start + 50, 120))]
        nxt = start + 50 if start + 50 < 120 else None
        return httpx.Response(200, json={"result": rows, **({"next": nxt} if nxt else {})})

    seen: list[str] = []
    async with bitrix(handler) as crm:
        cursor: str | None = None
        while True:
            page = await crm.list_products(limit=10, cursor=cursor)
            seen += [p.id for p in page.items]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    assert seen == [str(i) for i in range(120)]


async def test_amo_company_found_beyond_the_first_page() -> None:
    page1 = {
        "_embedded": {"companies": [amo_company(i, f"Alfa {i}", None) for i in range(1, 251)]},
        "_links": {"next": {"href": "x"}},
    }
    page2 = {"_embedded": {"companies": [amo_company(999, "Alfa", None)]}}

    def answer(request: httpx.Request) -> Any:
        return page2 if query(request).get("page") == ["2"] else page1

    crm, _ = amo({"GET /api/v4/companies": answer})
    async with crm:
        assert await crm.find_company(CompanyIn("Alfa")) == "999"


async def test_bitrix_company_found_beyond_the_first_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _, body = b24_method(request)
        if int(body.get("start", 0)) == 0:
            return httpx.Response(
                200, json={"result": [{"ID": str(i), "TITLE": f"Alfa {i}"} for i in range(1, 51)], "next": 50}
            )
        return httpx.Response(200, json={"result": [{"ID": "999", "TITLE": "Alfa"}]})

    async with bitrix(handler) as crm:
        assert await crm.find_company(CompanyIn("Alfa")) == "999"


# --- M4: a found contact is linked to the company when it has none ---


async def test_amo_found_contact_is_linked_to_the_company() -> None:
    contact = {"id": 5, "custom_fields_values": [{"field_code": "EMAIL", "values": [{"value": "a@b.com"}]}]}
    crm, router = amo(
        {
            "GET /api/v4/contacts": {"_embedded": {"contacts": [contact]}},
            "GET /api/v4/contacts/5": {"id": 5, "_embedded": {"companies": []}},
            "POST /api/v4/contacts/5/link": {"_embedded": {"links": []}},
        }
    )
    async with crm:
        assert await crm.find_or_create_contact(ContactIn("x", email="a@b.com"), company_id="7") == Ref(
            "5", False
        )
    link = router.sent("POST", "/api/v4/contacts/5/link")
    assert json.loads(link[0].content) == [{"to_entity_id": 7, "to_entity_type": "companies"}]


async def test_odoo_found_contact_is_linked_only_when_it_has_no_company() -> None:
    fake = FakeOdoo(
        {
            "res.partner": [
                {"id": 6, "name": "Jane", "is_company": False, "email": "jane@corp.com", "parent_id": False},
                {
                    "id": 8,
                    "name": "Joe",
                    "is_company": False,
                    "email": "joe@corp.com",
                    "parent_id": [3, "Other"],
                },
            ]
        }
    )
    async with _odoo(fake) as crm:
        await crm.find_or_create_contact(ContactIn("x", email="jane@corp.com"), company_id="5")
        await crm.find_or_create_contact(ContactIn("x", email="joe@corp.com"), company_id="5")
    writes = [args for model, method, args in fake.calls if method == "write"]
    assert writes == [[[6], {"parent_id": 5}]]
