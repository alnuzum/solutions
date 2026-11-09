"""Company lookup by requisites, users and where a new deal starts: the requests each CRM gets (as checked live
on 09.11) and how its answer is read."""

import json
import re
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.base import core_name, host, lookup_keys, mail_domain, same_company_name
from alnuzum_crm.bitrix24 import Bitrix24, php_query
from alnuzum_crm.errors import CrmRateLimited, CrmValidationError
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.tokens import StaticToken
from alnuzum_crm.types import CompanyHit, DealIn, User
from alnuzum_crm.zoho import ZohoCRM

from .odoo_fake import FakeOdoo


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ООО «Ромашка»", "Ромашка"),
        ("Romashka MChJ", "romashka"),
        ('"Navruz Stroy" LLC', "Navruz Stroy"),
        ("Общество с ограниченной ответственностью «Ёлка»", "елка"),
        ("Bahor Market Ltd.", "BAHOR MARKET"),
    ],
)
def test_names_compare_without_legal_forms_quotes_and_case(a: str, b: str) -> None:
    assert same_company_name(a, b)


def test_a_legal_form_alone_is_no_name() -> None:
    assert core_name("ООО") == ""
    assert not same_company_name("ООО", "ООО")
    assert not same_company_name("Ромашка", "Ромашка-2")


def test_hosts_and_mail_domains() -> None:
    assert host("https://www.Romashka-Demo.uz/contacts?x=1") == "romashka-demo.uz"
    assert host("romashka-demo.uz") == "romashka-demo.uz"
    assert mail_domain("A.Karimov@Romashka-Demo.UZ") == "romashka-demo.uz"
    assert mail_domain("not an address") == ""


def test_lookup_keys_leave_out_what_cannot_be_searched() -> None:
    keys = lookup_keys(
        inn="305 123 456",
        domain="https://romashka-demo.uz",
        phone="+998 (71) 200-11-01",
        name="ООО «Ромашка»",
    )
    assert keys == {
        "inn": "305123456",
        "domain": "romashka-demo.uz",
        "phone": "998712001101",
        "name": "Ромашка",
    }
    assert lookup_keys(inn="123", domain="no host", email="x@y", phone="12-34", name="ООО") == {}


# --- amoCRM and Kommo ---


def _amo(handler: Any) -> AmoCRM:
    return AmoCRM(
        "example.amocrm.ru", StaticToken("T", crm="amocrm"), rate=1000, transport=httpx.MockTransport(handler)
    )


async def test_amocrm_asks_each_key_and_reads_the_values_back() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/companies/custom_fields":
            return httpx.Response(
                200, json={"_embedded": {"custom_fields": [{"id": 77, "code": "INN", "name": "ИНН"}]}}
            )
        assert request.url.path == "/api/v4/companies"
        asked.append(request.url.params["query"])
        company = {
            "id": 37846273,
            "name": "Ромашка",
            "custom_fields_values": [
                {"field_id": 77, "values": [{"value": "305123456"}]},
                {"field_code": "WEB", "values": [{"value": "https://romashka-demo.uz"}]},
                {"field_code": "PHONE", "values": [{"value": "+998 71 200 11 01"}]},
                {"field_code": "EMAIL", "values": [{"value": "info@romashka-demo.uz"}]},
            ],
        }
        return httpx.Response(200, json={"_embedded": {"companies": [company]}})

    async with _amo(handler) as crm:
        hits = await crm.find_companies(
            inn="305123456",
            domain="romashka-demo.uz",
            email="a@romashka-demo.uz",
            phone="+998712001101",
            name="ООО «Ромашка»",
        )
    assert asked == ["305123456", "romashka-demo.uz", "a@romashka-demo.uz", "712001101", "Ромашка"]
    assert hits == [
        CompanyHit(
            "37846273",
            "Ромашка",
            inn="305123456",
            phones=("+998 71 200 11 01",),
            emails=("info@romashka-demo.uz",),
            web=("https://romashka-demo.uz",),
        )
    ]


async def test_amocrm_users_are_the_active_ones_and_a_deal_starts_where_routed() -> None:
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/users":
            users = [
                {"id": 1, "name": "Анна", "rights": {"is_active": True}},
                {"id": 2, "name": "Уволен", "rights": {"is_active": False}},
            ]
            return httpx.Response(200, json={"_embedded": {"users": users}})
        if request.url.path == "/api/v4/account":
            return httpx.Response(200, json={"id": 1, "currency": "UZS"})
        assert request.method == "POST" and request.url.path == "/api/v4/leads"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"_embedded": {"leads": [{"id": 9}]}})

    async with _amo(handler) as crm:
        assert await crm.users() == [User("1", "Анна")]
        made = await crm.create_deal(DealIn("Письмо", stage_id="7:142", responsible_id="1"))
        with pytest.raises(ValueError, match="digits"):
            await crm.create_deal(DealIn("Письмо", stage_id="7:../x"))
    assert made.id == "9"
    assert sent[0][0] | {"_embedded": None} == {
        "name": "Письмо",
        "price": 0,
        "_embedded": None,
        "pipeline_id": 7,
        "status_id": 142,
        "responsible_user_id": 1,
    }


# --- Bitrix24 ---


def _nested(query: str) -> dict[str, Any]:
    """A command of ``batch`` read back as PHP reads it: ``a[b][0]=x`` → ``{"a": {"b": ["x"]}}``."""
    out: dict[str, Any] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        path = re.findall(r"[^\[\]]+", key)
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value

    def lists(node: Any) -> Any:
        if isinstance(node, dict):
            if node and all(k.isdigit() for k in node):
                return [lists(node[k]) for k in sorted(node, key=int)]
            return {k: lists(v) for k, v in node.items()}
        return node

    return dict(lists(out))


def _b24(results: dict[str, Any], calls: list[tuple[str, Any]], posts: list[str] | None = None) -> Bitrix24:
    """``calls``: every method asked, batched or not; ``posts``: the requests that went to the portal."""

    def answer(method: str, params: dict[str, Any]) -> Any:
        calls.append((method, params))
        got = results[method]
        return got(params) if callable(got) else got

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        params = json.loads(request.content or b"{}")
        if posts is not None:
            posts.append(method)
        if method == "batch":
            done = {}
            for key, cmd in params["cmd"].items():
                name, _, query = cmd.partition("?")
                done[key] = answer(name, _nested(query))
            return httpx.Response(200, json={"result": {"result": done, "result_error": []}})
        return httpx.Response(200, json={"result": answer(method, params)})

    return Bitrix24(
        "https://example.bitrix24.ru/rest/1/WEBHOOKTOKEN/", rate=1000, transport=httpx.MockTransport(handler)
    )


async def test_bitrix24_finds_by_requisites_site_mail_phone_and_title_then_reads_them() -> None:
    calls: list[tuple[str, Any]] = []

    def companies(params: dict[str, Any]) -> list[dict[str, Any]]:
        flt = params["filter"]
        if "ID" in flt:
            return [
                {
                    "ID": "63",
                    "TITLE": "Ромашка",
                    "WEB": [{"VALUE": "https://romashka-demo.uz"}],
                    "EMAIL": [{"VALUE": "info@romashka-demo.uz"}],
                    "PHONE": [{"VALUE": "+998 71 200 11 01"}],
                }
            ]
        if "WEB" in flt:  # the portal may ignore the filter: only the site asked for counts
            return [{"ID": "63", "WEB": [{"VALUE": "https://romashka-demo.uz"}]}, {"ID": "70", "WEB": []}]
        return [{"ID": "63"}] if "%TITLE" in flt else []

    def requisites(params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"ENTITY_ID": "63", "RQ_INN": "305123456"}]

    results = {
        "crm.requisite.list": requisites,
        "crm.company.list": companies,
        "crm.duplicate.findbycomm": {"COMPANY": [63]},
        "user.get": [{"ID": "1", "NAME": "Анна", "LAST_NAME": "Ли"}],
        "crm.deal.add": 501,
    }
    posts: list[str] = []
    async with _b24(results, calls, posts) as crm:
        hits = await crm.find_companies(
            inn="305123456",
            domain="romashka-demo.uz",
            email="info@romashka-demo.uz",
            phone="+998 71 200 11 01",
            name="«Ромашка»",
        )
        assert posts == ["batch", "batch"]  # five searches in one request, the reading in another
        assert await crm.users() == [User("1", "Анна Ли")]
        await crm.create_deal(DealIn("Письмо", currency="UZS", stage_id="C3:NEW", responsible_id="1"))
        with pytest.raises(CrmValidationError):
            await crm.create_deal(DealIn("Письмо", currency="UZS", stage_id="bad stage"))
    assert hits == [
        CompanyHit(
            "63",
            "Ромашка",
            inn="305123456",
            phones=("+998 71 200 11 01",),
            emails=("info@romashka-demo.uz",),
            web=("https://romashka-demo.uz",),
        )
    ]
    sites = next(p["filter"]["WEB"] for m, p in calls if m == "crm.company.list" and "WEB" in p["filter"])
    assert {"https://romashka-demo.uz", "www.romashka-demo.uz"} <= set(sites)
    assert not any("%WEB" in p.get("filter", {}) or "%PHONE" in p.get("filter", {}) for _, p in calls)
    dup = [p for m, p in calls if m == "crm.duplicate.findbycomm"]
    assert [(p["type"], p["values"]) for p in dup] == [
        ("EMAIL", ["info@romashka-demo.uz"]),
        ("PHONE", ["998712001101"]),
    ]
    added = next(p["fields"] for m, p in calls if m == "crm.deal.add")
    assert (added["STAGE_ID"], added["CATEGORY_ID"], added["ASSIGNED_BY_ID"]) == ("C3:NEW", 3, 1)


async def test_bitrix24_asks_nothing_more_when_nothing_is_found() -> None:
    calls: list[tuple[str, Any]] = []
    results: dict[str, Any] = {
        "crm.requisite.list": [],
        "crm.company.list": [],
        "crm.duplicate.findbycomm": [],
    }
    posts: list[str] = []
    async with _b24(results, calls, posts) as crm:
        assert await crm.find_companies(inn="305123456", name="Нет такой") == []
    assert [m for m, _ in calls] == ["crm.requisite.list", "crm.company.list"]
    assert posts == ["batch"]


async def test_bitrix24_a_failed_command_of_a_batch_raises_as_alone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = {"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many requests"}
        return httpx.Response(200, json={"result": {"result": [], "result_error": {"inn": error}}})

    crm = Bitrix24(
        "https://example.bitrix24.ru/rest/1/WEBHOOKTOKEN/", rate=1000, transport=httpx.MockTransport(handler)
    )
    async with crm:
        with pytest.raises(CrmRateLimited):
            await crm.find_companies(inn="305123456")


def test_a_batch_command_is_a_php_query() -> None:
    query = php_query({"filter": {"%TITLE": "Ромашка", "WEB": ["https://x.uz", "x.uz"]}, "select": ["ID"]})
    assert _nested(query) == {
        "filter": {"%TITLE": "Ромашка", "WEB": ["https://x.uz", "x.uz"]},
        "select": ["ID"],
    }


# --- Zoho ---


async def test_zoho_asks_words_and_a_phone_and_reads_its_inn_field() -> None:
    asked: list[str] = []
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/crm/v8")
        if path == "/settings/fields":
            fields = [{"api_name": "INN", "field_label": "INN", "custom_field": True}]
            return httpx.Response(200, json={"fields": fields})
        if path == "/Accounts/search":
            asked.append(request.url.query.decode())
            row = {
                "id": "4607570000000315007",
                "Account_Name": "Ромашка",
                "INN": "305123456",
                "Website": "https://romashka-demo.uz",
                "Phone": "+998 71 200 11 01",
            }
            return httpx.Response(200, json={"data": [row]})
        if path == "/users":
            return httpx.Response(200, json={"users": [{"id": "945982939", "full_name": "Dev"}]})
        if path == "/Deals" and request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(201, json={"data": [{"code": "SUCCESS", "details": {"id": "1"}}]})
        raise AssertionError(f"{request.method} {path}")

    async with ZohoCRM(
        "https://www.zohoapis.com",
        StaticToken("T", crm="zoho"),
        rate=1000,
        transport=httpx.MockTransport(handler),
    ) as crm:
        hits = await crm.find_companies(
            inn="305123456",
            domain="romashka-demo.uz",
            email="a@romashka-demo.uz",
            phone="+998712001101",
            name="Ромашка",
        )
        assert await crm.users() == [User("945982939", "Dev")]

        async def stages() -> dict[str, str]:
            return {"Qualification": "Open", "Needs Analysis": "Open"}

        crm._stage_types = stages  # type: ignore[method-assign]
        await crm.create_deal(DealIn("Письмо", stage_id="Needs Analysis", responsible_id="945982939"))
        with pytest.raises(CrmValidationError):
            await crm.create_deal(DealIn("Письмо", stage_id="Nowhere"))
    assert asked == [
        "word=305123456",
        "word=romashka-demo.uz",
        "word=%D0%A0%D0%BE%D0%BC%D0%B0%D1%88%D0%BA%D0%B0",
        "phone=998712001101",
    ]
    assert hits == [
        CompanyHit(
            "4607570000000315007",
            "Ромашка",
            inn="305123456",
            phones=("+998 71 200 11 01",),
            web=("https://romashka-demo.uz",),
        )
    ]
    record = sent[0]["data"][0]
    assert (record["Stage"], record["Owner"]) == ("Needs Analysis", {"id": "945982939"})


# --- Odoo ---


class CreatingOdoo(FakeOdoo):
    """The fake, with ``create`` answering a new id as Odoo does."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)["params"]
        if params["method"] != "authenticate" and params["args"][4] == "create":
            self.calls.append((params["args"][3], "create", params["args"][5]))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 41})
        return super().handler(request)


async def test_odoo_looks_among_companies_only_and_routes_a_new_deal() -> None:
    fake = CreatingOdoo(
        {
            "res.partner": [
                {
                    "id": 16,
                    "name": "Ромашка",
                    "is_company": True,
                    "vat": "305123456",
                    "email": "info@romashka-demo.uz",
                    "phone": "+998 71 200 11 01",
                    "website": "https://romashka-demo.uz",
                },
                {
                    "id": 17,
                    "name": "Алишер Каримов",
                    "is_company": False,
                    "vat": "305123456",
                    "email": False,
                    "phone": False,
                    "website": False,
                },
            ],
            "res.users": [{"id": 2, "name": "Admin", "share": False}],
            "crm.lead": [],
        }
    )
    async with Odoo(
        "https://odoo.example.com", "db", "u", "k", trusted_url=True, rate=1000, transport=fake.transport()
    ) as crm:
        hits = await crm.find_companies(inn="305 123 456")
        assert await crm.users() == [User("2", "Admin")]
        assert await crm.find_companies() == []
        await crm.find_companies(
            inn="305123456", domain="romashka-demo.uz", phone="+998712001101", name="Ромашка"
        )
        await crm.create_deal(DealIn("Письмо", stage_id="3", responsible_id="2"))
        await crm.find_companies(phone="+7 900 123 45 67")  # a tail «00…» Odoo would anchor at the start
    assert hits == [
        CompanyHit(
            "16",
            "Ромашка",
            inn="305123456",
            phones=("+998 71 200 11 01",),
            emails=("info@romashka-demo.uz",),
            web=("https://romashka-demo.uz",),
        )
    ]
    domains = [
        args[0] for model, method, args in fake.calls if model == "res.partner" and method == "search_read"
    ]
    assert domains[-2][:2] == ["&", ["is_company", "=", True]]
    assert domains[-2].count("|") == 4 and ["phone_mobile_search", "ilike", "712001101"] in domains[-2]
    assert ["phone_mobile_search", "ilike", "1234567"] in domains[-1]
    created = next(
        args[0] for model, method, args in fake.calls if model == "crm.lead" and method == "create"
    )
    assert (created["stage_id"], created["user_id"]) == (3, 2)
