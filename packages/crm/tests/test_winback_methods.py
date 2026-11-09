"""Win-back: the purchases of a client or of a whole account, a won deal at a past date and a task on the client
itself — the requests each CRM gets (as checked live on 10.11) and how its answer is read."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.base import at_most_one, in_period
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.tokens import StaticToken
from alnuzum_crm.types import ClientTask, DealIn, DealLine, Purchase, TaskIn
from alnuzum_crm.zoho import ZohoCRM

from .test_lookup_methods import _b24

DUE = datetime(2026, 11, 12, 13, 0, tzinfo=UTC)
SINCE, UNTIL = datetime(2025, 11, 10, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC)


def test_a_period_is_half_open_and_naive_moments_are_utc() -> None:
    assert in_period(datetime(2025, 11, 10), SINCE, UNTIL)
    assert not in_period(UNTIL, SINCE, UNTIL)
    assert in_period(datetime(2001, 1, 1, tzinfo=UTC), None, None)
    assert not in_period(None, None, None)
    with pytest.raises(ValueError, match="not both"):
        at_most_one("1", "2", "x")


# --- amoCRM ---

LEAD_WON = {
    "id": 501,
    "name": "Покупка 1",
    "price": 250,
    "status_id": 142,
    "closed_at": int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()),
    "_embedded": {
        "catalog_elements": [
            {"id": 7, "metadata": {"quantity": 2.5, "catalog_id": 11}},
            {"id": 8, "metadata": {"quantity": 1, "catalog_id": 99}},  # another catalog: not a product
            {"id": 9, "metadata": {"quantity": 1, "catalog_id": 11}},  # gone from the catalog
        ],
        "companies": [{"id": 37}],
        "contacts": [{"id": 40, "is_main": False}, {"id": 41, "is_main": True}],
    },
}
LEAD_LOST = {"id": 502, "name": "x", "status_id": 143, "closed_at": LEAD_WON["closed_at"]}
LEAD_OLD = {**LEAD_WON, "id": 503, "closed_at": int(datetime(2026, 3, 1, tzinfo=UTC).timestamp())}
ELEMENT = {
    "id": 7,
    "name": "Кабель",
    "custom_fields_values": [
        {"field_code": "PRICE", "values": [{"value": "100"}]},
        {"field_code": "SKU", "values": [{"value": "K-1"}]},
    ],
}


def _amo(handler: Any) -> AmoCRM:
    return AmoCRM(
        "example.amocrm.ru",
        StaticToken("T", crm="amocrm"),
        catalog_id=11,
        rate=1000,
        transport=httpx.MockTransport(handler),
    )


def _amo_common(request: httpx.Request) -> httpx.Response | None:
    path = request.url.path
    if path == "/api/v4/account":
        return httpx.Response(200, json={"id": 1, "currency": "UZS"})
    if path == "/api/v4/leads/pipelines":
        return httpx.Response(200, json={"_embedded": {"pipelines": [{"id": 5}, {"id": 6, "is_main": True}]}})
    if path == "/api/v4/catalogs/11/elements":
        assert request.url.params.get_list("filter[id][]") == ["7", "9"]
        return httpx.Response(200, json={"_embedded": {"elements": [ELEMENT]}})
    return None


async def test_amocrm_reads_the_won_deals_of_the_account_page_by_page() -> None:
    asked: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        common = _amo_common(request)
        if common is not None:
            return common
        assert request.url.path == "/api/v4/leads"
        asked.append(request.url.params)
        links = {"next": {"href": "x"}} if request.url.params["page"] == "1" else {}
        return httpx.Response(200, json={"_embedded": {"leads": [LEAD_WON, LEAD_LOST]}, "_links": links})

    async with _amo(handler) as crm:
        first = await crm.purchases(since=SINCE, until=UNTIL, limit=500)
        second = await crm.purchases(since=SINCE, until=UNTIL, cursor=first.next_cursor)
    params = asked[0]
    assert (
        params["filter[statuses][0][pipeline_id]"] == "5"
        and params["filter[statuses][1][pipeline_id]"] == "6"
    )
    assert params["filter[statuses][0][status_id]"] == params["filter[statuses][1][status_id]"] == "142"
    assert params["filter[closed_at][from]"] == str(int(SINCE.timestamp()))
    assert params["filter[closed_at][to]"] == str(int(UNTIL.timestamp()))
    assert params["limit"] == "250" and params["with"] == "catalog_elements,contacts"
    assert first.next_cursor == "2" and second.next_cursor is None and asked[1]["page"] == "2"
    assert first.items == (
        Purchase(
            "501",
            "Покупка 1",
            datetime(2026, 9, 1, tzinfo=UTC),
            Decimal(250),
            "UZS",
            company_id="37",
            contact_id="41",
            lines=(DealLine("Кабель", Decimal("2.5"), Decimal(100), product_id="7", sku="K-1"),),
        ),
    )


async def test_amocrm_reads_a_company_newest_first_and_without_products_when_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        common = _amo_common(request)
        if common is not None:
            return common
        if request.url.path == "/api/v4/contacts/41":
            return httpx.Response(204)
        if request.url.path == "/api/v4/companies/37":
            assert request.url.params["with"] == "leads"
            ids = [{"id": i} for i in (503, 501, 502)]
            return httpx.Response(200, json={"id": 37, "_embedded": {"leads": ids}})
        assert request.url.params["with"] == "contacts"
        assert request.url.params.get_list("filter[id][]") == ["503", "501", "502"]
        return httpx.Response(200, json={"_embedded": {"leads": [LEAD_OLD, LEAD_WON, LEAD_LOST]}})

    async with _amo(handler) as crm:
        page = await crm.purchases(company_id="37", lines=False)
        newest = await crm.purchases(company_id="37", lines=False, since=datetime(2026, 6, 1, tzinfo=UTC))
        with pytest.raises(Exception, match="client not found"):
            await crm.purchases(contact_id="41")
    assert [(p.deal_id, p.lines) for p in page.items] == [("501", ()), ("503", ())]
    assert page.next_cursor is None and [p.deal_id for p in newest.items] == ["501"]


async def test_amocrm_creates_a_won_deal_closed_in_the_past_and_tasks_a_company() -> None:
    sent: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        common = _amo_common(request)
        if common is not None:
            return common
        body = json.loads(request.content) if request.content else None
        sent.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == "/api/v4/leads":
            return httpx.Response(200, json={"_embedded": {"leads": [{"id": 600}]}})
        if request.method == "POST" and request.url.path == "/api/v4/tasks":
            return httpx.Response(200, json={"_embedded": {"tasks": [{"id": 70}]}})
        if request.method == "GET" and request.url.path == "/api/v4/tasks":
            assert request.url.params["filter[entity_type]"] == "contacts"
            assert request.url.params["filter[entity_id][]"] == "41"
            assert request.url.params["filter[is_completed]"] == "0"
            tasks = [
                {"id": 70, "text": "Позвонить", "complete_till": int(DUE.timestamp()), "is_completed": False},
                {"id": 71, "text": "Готово", "is_completed": True},
            ]
            return httpx.Response(200, json={"_embedded": {"tasks": tasks}})
        return httpx.Response(200, json={})

    closed = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    async with _amo(handler) as crm:
        made = await crm.create_won_deal(DealIn("Покупка", company_id="37"), closed)
        task = await crm.add_client_task(TaskIn("Позвонить", DUE, responsible_id="3"), company_id="37")
        listed = await crm.client_tasks(contact_id="41")
        await crm.discard_task(task)
        with pytest.raises(ValueError, match="exactly one"):
            await crm.client_tasks()
    assert made.id == "600"
    lead = sent[0][2][0]
    assert (lead["pipeline_id"], lead["status_id"]) == (6, 142), "created won in the main pipeline"
    assert sent[1] == ("PATCH", "/api/v4/leads/600", {"closed_at": int(closed.timestamp())})
    assert sent[2][2] == [
        {
            "text": "Позвонить",
            "complete_till": int(DUE.timestamp()),
            "entity_id": 37,
            "entity_type": "companies",
            "task_type_id": 1,
            "responsible_user_id": 3,
        }
    ]
    assert task == "70" and listed == [ClientTask("70", "Позвонить", DUE)]
    assert sent[-1] == ("PATCH", "/api/v4/tasks/70", {"is_completed": True, "result": {"text": "e2e"}})


# --- Bitrix24 ---


async def test_bitrix24_reads_won_deals_with_their_rows_and_tasks_a_company() -> None:
    calls: list[tuple[str, Any]] = []
    row = {
        "ID": "255",
        "TITLE": "Покупка",
        "OPPORTUNITY": "300.00",
        "CURRENCY_ID": "RUB",
        "STAGE_SEMANTIC_ID": "S",
        "CLOSED": "Y",
        "CLOSEDATE": "2026-08-01T03:00:00+03:00",
        "COMPANY_ID": "185",
        "CONTACT_ID": None,
    }
    old = {**row, "ID": "254", "CLOSEDATE": "2024-01-01T03:00:00+03:00"}
    results: dict[str, Any] = {
        "crm.deal.list": [row, old],
        "crm.deal.productrows.get": [
            {"PRODUCT_ID": 51, "PRODUCT_NAME": "Кабель", "PRICE": 100, "QUANTITY": 3},
            {"PRODUCT_ID": 0, "PRODUCT_NAME": "Доставка", "PRICE": 10, "QUANTITY": 1},
        ],
        "crm.activity.todo.add": {"id": 259},
        "crm.activity.list": [{"ID": "259", "SUBJECT": "Позвонить", "DEADLINE": "2026-11-12T16:00:00+03:00"}],
        "crm.activity.delete": True,
    }
    crm = _b24(results, calls)
    async with crm:
        page = await crm.purchases(since=SINCE, until=UNTIL)
        mine = await crm.purchases(contact_id="9", lines=False)
        task = await crm.add_client_task(TaskIn("Позвонить", DUE, responsible_id="1"), company_id="185")
        listed = await crm.client_tasks(company_id="185")
        await crm.discard_task(task)
    first = calls[0][1]
    assert first["filter"]["STAGE_SEMANTIC_ID"] == "S" and first["start"] == 0
    assert first["filter"][">=CLOSEDATE"] == "2025-11-10T00:00:00+00:00"
    assert first["filter"]["<CLOSEDATE"] == "2026-11-10T00:00:00+00:00"
    assert first["order"] == {"CLOSEDATE": "DESC", "ID": "DESC"}
    assert page.items == (
        Purchase(
            "255",
            "Покупка",
            datetime.fromisoformat("2026-08-01T03:00:00+03:00"),
            Decimal("300.00"),
            "RUB",
            company_id="185",
            lines=(
                DealLine("Кабель", Decimal(3), Decimal(100), product_id="51"),
                DealLine("Доставка", Decimal(1), Decimal(10)),
            ),
        ),
    )
    assert page.next_cursor is None
    assert [p.deal_id for p in mine.items] == ["255", "254"] and mine.items[0].lines == ()
    assert ("crm.deal.list", calls[2][1]) == calls[2] and calls[2][1]["filter"]["CONTACT_ID"] == 9
    added = next(p for m, p in calls if m == "crm.activity.todo.add")
    assert (added["ownerTypeId"], added["ownerId"], added["responsibleId"]) == (4, 185, 1)
    assert task == "259" and listed == [
        ClientTask("259", "Позвонить", datetime.fromisoformat("2026-11-12T16:00:00+03:00"))
    ]
    flt = next(p for m, p in calls if m == "crm.activity.list")["filter"]
    assert flt == {"OWNER_TYPE_ID": 4, "OWNER_ID": 185, "COMPLETED": "N"}
    assert calls[-1] == ("crm.activity.delete", {"id": 259})


async def test_bitrix24_a_won_deal_gets_its_closing_date_after_it_is_won() -> None:
    calls: list[tuple[str, Any]] = []
    results: dict[str, Any] = {
        "crm.category.list": {"categories": [{"id": 0, "name": "Общая"}]},
        "crm.status.list": [
            {"STATUS_ID": "NEW", "NAME": "Новая", "SEMANTICS": None},
            {"STATUS_ID": "WON", "NAME": "Успех", "SEMANTICS": "S"},
        ],
        "crm.currency.base.get": "RUB",
        "crm.deal.add": 300,
        "crm.deal.update": True,
    }
    crm = _b24(results, calls)
    async with crm:
        made = await crm.create_won_deal(DealIn("Покупка", currency="RUB"), datetime(2026, 8, 1, tzinfo=UTC))
    assert made.id == "300"
    assert next(p for m, p in calls if m == "crm.deal.add")["fields"]["STAGE_ID"] == "WON"
    assert calls[-1] == ("crm.deal.update", {"id": 300, "fields": {"CLOSEDATE": "2026-08-01"}})


# --- Zoho ---


def _zoho(handler: Any) -> ZohoCRM:
    return ZohoCRM(
        "https://www.zohoapis.com",
        StaticToken("T", crm="zoho"),
        rate=1000,
        transport=httpx.MockTransport(handler),
    )


STAGE_FIELD = {
    "fields": [
        {
            "api_name": "Stage",
            "pick_list_values": [
                {"actual_value": "Qualification", "forecast_type": "Open"},
                {"actual_value": "Closed Won", "forecast_type": "Closed Won"},
                {"actual_value": "Closed (Lost)", "forecast_type": "Closed Lost"},
            ],
        }
    ]
}
ZOHO_DEAL = {
    "id": "4001",
    "Deal_Name": "Покупка",
    "Amount": 300,
    "Stage": "Closed Won",
    "Closing_Date": "2026-09-01",
    "Account_Name": {"id": "3001", "name": "Ромашка"},
    "Contact_Name": None,
}


async def test_zoho_reads_the_account_by_search_and_a_company_by_its_deals() -> None:
    asked: list[tuple[str, httpx.QueryParams]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/crm/v8")
        asked.append((path, request.url.params))
        if path == "/settings/fields":
            return httpx.Response(200, json=STAGE_FIELD)
        if path == "/Deals/search":
            more = request.url.params["page"] == "1"
            return httpx.Response(200, json={"data": [ZOHO_DEAL], "info": {"more_records": more}})
        if path == "/Accounts/3001/Deals":
            more = request.url.params["page"] == "1"
            rows = [ZOHO_DEAL, {**ZOHO_DEAL, "id": "4002", "Stage": "Qualification"}] if more else []
            rows += [{**ZOHO_DEAL, "id": "4003", "Closing_Date": "2026-10-01"}] if not more else []
            return httpx.Response(200, json={"data": rows, "info": {"more_records": more}})
        if path == "/Deals/4001/Quotes":
            return httpx.Response(200, json={"data": [{"id": "5001"}]})
        if path == "/Quotes/5001":
            item = {"Product_Name": {"id": "6001", "name": "Кабель"}, "Quantity": 3, "List_Price": 100}
            return httpx.Response(200, json={"data": [{"Quoted_Items": [item]}]})
        raise AssertionError(path)

    async with _zoho(handler) as crm:
        page = await crm.purchases(since=SINCE, until=UNTIL, limit=500)
        mine = await crm.purchases(company_id="3001", lines=False)
    search = next(p for path, p in asked if path == "/Deals/search")
    assert search["criteria"] == (
        "(((Stage:equals:Closed Won))and(Closing_Date:greater_equal:2025-11-10)"
        "and(Closing_Date:less_equal:2026-11-10))"
    )
    assert search["per_page"] == "200" and page.next_cursor == "2"
    assert page.items == (
        Purchase(
            "4001",
            "Покупка",
            datetime(2026, 9, 1, tzinfo=UTC),
            Decimal(300),
            None,
            company_id="3001",
            lines=(DealLine("Кабель", Decimal(3), Decimal(100), product_id="6001"),),
        ),
    )
    assert [p.deal_id for p in mine.items] == ["4003", "4001"] and mine.next_cursor is None


async def test_zoho_tasks_an_account_or_a_contact_and_lists_the_open_ones() -> None:
    sent: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/crm/v8")
        sent.append((request.method, path, json.loads(request.content) if request.content else None))
        if request.method == "POST":
            return httpx.Response(201, json={"data": [{"code": "SUCCESS", "details": {"id": "7001"}}]})
        if path == "/Accounts/3001/Tasks":
            rows = [
                {"id": "7001", "Subject": "Позвонить", "Status": "Not Started", "Due_Date": "2026-11-12"},
                {"id": "7000", "Subject": "Старое", "Status": "Completed", "Due_Date": "2026-10-01"},
            ]
            return httpx.Response(200, json={"data": rows})
        if path == "/Contacts/3002/Tasks":
            return httpx.Response(204)
        return httpx.Response(200, json={"data": [{"code": "SUCCESS"}]})

    async with _zoho(handler) as crm:
        on_account = await crm.add_client_task(TaskIn("Позвонить", DUE), company_id="3001")
        await crm.add_client_task(TaskIn("Позвонить", DUE, responsible_id="5"), contact_id="3002")
        listed = await crm.client_tasks(company_id="3001")
        assert await crm.client_tasks(contact_id="3002") == []
        await crm.discard_task(on_account)
    assert sent[0][2]["data"][0] == {
        "Subject": "Позвонить",
        "Description": "Позвонить",
        "Due_Date": "2026-11-12",
        "What_Id": {"id": "3001"},
        "$se_module": "Accounts",
    }
    assert sent[1][2]["data"][0]["Who_Id"] == {"id": "3002"} and sent[1][2]["data"][0]["Owner"] == {"id": "5"}
    assert listed == [ClientTask("7001", "Позвонить", datetime(2026, 11, 12, tzinfo=UTC))]
    assert sent[-1][:2] == ("DELETE", "/Tasks")


async def test_zoho_a_won_deal_is_created_won_then_dated() -> None:
    sent: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/crm/v8")
        if path == "/settings/fields":
            return httpx.Response(200, json=STAGE_FIELD)
        sent.append((request.method, path, json.loads(request.content)))
        return httpx.Response(201, json={"data": [{"code": "SUCCESS", "details": {"id": "4009"}}]})

    async with _zoho(handler) as crm:
        await crm.create_won_deal(DealIn("Покупка"), datetime(2026, 8, 1, tzinfo=UTC))
    assert sent[0][2]["data"][0]["Stage"] == "Closed Won"
    assert sent[1] == ("PUT", "/Deals/4009", {"data": [{"Closing_Date": "2026-08-01"}]})


# --- Odoo ---


def _odoo(answers: dict[tuple[str, str], Any], calls: list[tuple[str, str, Any, Any]]) -> Odoo:
    def handler(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)["params"]
        if params["method"] in ("authenticate", "version"):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 2})
        _db, _uid, _key, model, method, args, kwargs = params["args"]
        calls.append((model, method, args, kwargs))
        got = answers.get((model, method), [])
        result = got(args, kwargs) if callable(got) else got
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return Odoo(
        "https://odoo.example.com",
        "db",
        "u",
        "k",
        trusted_url=True,
        rate=1000,
        transport=httpx.MockTransport(handler),
    )


async def test_odoo_reads_won_opportunities_with_their_quotation_lines_and_the_client() -> None:
    calls: list[tuple[str, str, Any, Any]] = []

    def fields(args: Any, _kw: Any) -> list[int]:
        return [1] if args[0][1][2] in ("won_status", "opportunity_id") else []

    lead = {
        "id": 14,
        "name": "Покупка",
        "expected_revenue": 300,
        "won_status": "won",
        "date_closed": "2026-08-01 07:00:00",
        "company_currency": [1, "USD"],
        "stage_id": [4, "Won"],
        "partner_id": [27, "Алишер"],
    }
    answers: dict[tuple[str, str], Any] = {
        ("ir.model.fields", "search"): fields,
        ("crm.lead", "search_read"): [lead, {**lead, "id": 15, "partner_id": [26, "Ромашка"]}],
        ("res.partner", "search_read"): [
            {"id": 27, "is_company": False, "commercial_partner_id": [26, "Ромашка"]},
            {"id": 26, "is_company": True, "commercial_partner_id": [26, "Ромашка"]},
        ],
        ("sale.order", "search_read"): [{"id": 3, "opportunity_id": [14, "Покупка"]}],
        ("sale.order.line", "search_read"): [
            {
                "order_id": [3, "S3"],
                "name": "Кабель",
                "product_id": [51, "Кабель"],
                "product_uom_qty": 3,
                "price_unit": 100,
                "display_type": False,
            },
            {"order_id": [3, "S3"], "name": "Раздел", "product_id": False, "display_type": "line_section"},
        ],
    }
    async with _odoo(answers, calls) as crm:
        page = await crm.purchases(since=SINCE, until=UNTIL, limit=2, cursor="4")
        mine = await crm.purchases(company_id="26", lines=False)
    lead_call = next(c for c in calls if c[:2] == ("crm.lead", "search_read"))
    assert lead_call[2][0] == [
        ["won_status", "=", "won"],
        ["date_closed", ">=", "2025-11-10 00:00:00"],
        ["date_closed", "<", "2026-11-10 00:00:00"],
    ]
    assert (lead_call[3]["offset"], lead_call[3]["limit"], lead_call[3]["order"]) == (
        4,
        2,
        "date_closed desc, id desc",
    )
    assert page.next_cursor == "6"
    first, second = page.items
    assert (first.company_id, first.contact_id, second.company_id, second.contact_id) == (
        "26",
        "27",
        "26",
        None,
    )
    assert first.closed_at == datetime(2026, 8, 1, 7, tzinfo=UTC) and first.currency == "USD"
    assert (
        first.lines == (DealLine("Кабель", Decimal(3), Decimal(100), product_id="51"),) and second.lines == ()
    )
    owner_call = [c for c in calls if c[:2] == ("crm.lead", "search_read")][-1]
    assert owner_call[2][0][-1] == ["partner_id", "child_of", 26] and owner_call[3]["offset"] == 0
    assert mine.next_cursor is None and all(p.lines == () for p in mine.items)


async def test_odoo_dates_a_won_opportunity_and_puts_an_activity_on_the_partner() -> None:
    calls: list[tuple[str, str, Any, Any]] = []
    answers: dict[tuple[str, str], Any] = {
        ("crm.stage", "search_read"): [{"id": 1, "name": "New"}, {"id": 4, "name": "Won", "is_won": True}],
        ("ir.model.fields", "search"): [],
        ("crm.lead", "create"): 14,
        ("ir.model", "search"): [85],
        ("ir.model.data", "search_read"): [{"res_id": 4}],
        ("mail.activity", "create"): 8,
        ("mail.activity", "search_read"): [{"id": 8, "summary": "Позвонить", "date_deadline": "2026-11-12"}],
        ("mail.activity", "search"): lambda args, _kw: [8] if args[0][0][2] == 8 else [],
    }
    async with _odoo(answers, calls) as crm:
        made = await crm.create_won_deal(
            DealIn("Покупка", company_id="26"), datetime(2026, 8, 1, 7, tzinfo=UTC)
        )
        task = await crm.add_client_task(TaskIn("Позвонить", DUE), contact_id="27")
        listed = await crm.client_tasks(company_id="26")
        await crm.discard_task(task)
        await crm.discard_task("9")
    assert made.id == "14"
    assert next(c for c in calls if c[:2] == ("crm.lead", "create"))[2][0]["stage_id"] == 4
    assert next(c for c in calls if c[:2] == ("crm.lead", "write"))[2] == [
        [14],
        {"date_closed": "2026-08-01 07:00:00"},
    ]
    vals = next(c for c in calls if c[:2] == ("mail.activity", "create"))[2][0]
    assert (vals["res_model_id"], vals["res_id"], vals["date_deadline"], vals["user_id"]) == (
        85,
        27,
        "2026-11-12",
        2,
    )
    assert listed == [ClientTask("8", "Позвонить", datetime(2026, 11, 12, tzinfo=UTC))]
    unlinks = [c for c in calls if c[:2] == ("mail.activity", "unlink")]
    assert [c[2] for c in unlinks] == [[[8]]], "a missing activity is not unlinked"


# --- names of the clients a scan shows ---


async def test_amocrm_names_companies_and_contacts_fifty_a_request() -> None:
    asked: list[tuple[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params.get_list("filter[id][]")
        key = request.url.path.rsplit("/", 1)[-1]
        asked.append((key, ids))
        rows = [{"id": int(i), "name": f"{key} {i}"} for i in ids if i != "77"]
        return httpx.Response(200, json={"_embedded": {key: rows}})

    async with _amo(handler) as crm:
        got = await crm.client_names([str(i) for i in range(1, 61)] + ["77", "5"])
        assert len(got) == 60 and got["5"] == "companies 5" and "77" not in got
        assert [len(ids) for _, ids in asked] == [50, 11]
        assert await crm.client_names(["9"], contacts=True) == {"9": "contacts 9"}
        assert await crm.client_names([]) == {}
        with pytest.raises(ValueError, match="id"):
            await crm.client_names(["1/../x"])


async def test_bitrix24_names_by_title_or_by_name_and_last_name() -> None:
    calls: list[tuple[str, Any]] = []
    results = {
        "crm.company.list": lambda p: [{"ID": i, "TITLE": f"Co {i}"} for i in p["filter"]["ID"]],
        "crm.contact.list": [{"ID": "4", "NAME": "Алишер", "LAST_NAME": "Каримов"}],
    }
    async with _b24(results, calls) as crm:
        assert await crm.client_names(["3", "1"]) == {"1": "Co 1", "3": "Co 3"}
        assert await crm.client_names(["4"], contacts=True) == {"4": "Алишер Каримов"}
    assert calls[0] == ("crm.company.list", calls[0][1]) and calls[0][1]["filter"] == {"ID": ["1", "3"]}


async def test_zoho_names_records_by_ids_and_skips_the_missing() -> None:
    asked: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/Contacts"):
            return httpx.Response(204)
        rows = [{"id": i, "Account_Name": f"A{i}"} for i in request.url.params["ids"].split(",") if i != "9"]
        return httpx.Response(200, json={"data": rows})

    async with _zoho(handler) as crm:
        assert await crm.client_names(["3001", "9"]) == {"3001": "A3001"}
        assert await crm.client_names(["5"], contacts=True) == {}
    assert asked[0][1] == {"ids": "3001,9", "fields": "Account_Name"}
    assert asked[1][1]["fields"] == "Full_Name"


async def test_odoo_names_partners_archived_ones_too() -> None:
    calls: list[tuple[str, str, Any, Any]] = []
    answers = {("res.partner", "search_read"): [{"id": 7, "name": "Ромашка"}]}
    async with _odoo(answers, calls) as crm:
        assert await crm.client_names(["7", "7"]) == {"7": "Ромашка"}
        assert await crm.client_names([]) == {}
    assert calls == [
        (
            "res.partner",
            "search_read",
            [[["id", "in", [7]]]],
            {"fields": ["name"], "context": {"active_test": False}},
        )
    ]


# --- the newest purchases of an account first: a scan stopped at its ceiling keeps them ---


async def test_amocrm_reads_the_account_newest_first_by_id() -> None:
    asked: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (common := _amo_common(request)) is not None:
            return common
        asked.append(request.url.params)
        return httpx.Response(
            200, json={"_embedded": {"leads": [LEAD_WON, LEAD_LOST]}, "_links": {"next": {"href": "n"}}}
        )

    async with _amo(handler) as crm:
        page = await crm.recent_purchases(since=SINCE, until=UNTIL, limit=999)
    assert [p.deal_id for p in page.items] == ["501"] and page.next_cursor == "2"
    assert page.items[0].lines == () and page.items[0].company_id == "37"
    got = asked[0]
    assert got["order[id]"] == "desc" and got["limit"] == "250" and got["with"] == "contacts"
    assert got["filter[statuses][0][status_id]"] == "142" and got["filter[statuses][1][pipeline_id]"] == "6"


async def test_bitrix24_and_odoo_read_the_account_newest_first_already() -> None:
    calls: list[tuple[str, Any]] = []
    async with _b24({"crm.deal.list": []}, calls) as crm:
        await crm.recent_purchases(since=SINCE, until=UNTIL, limit=50)
    assert calls[0][1]["order"] == {"CLOSEDATE": "DESC", "ID": "DESC"}
    odoo_calls: list[tuple[str, str, Any, Any]] = []
    answers = {("ir.model.fields", "search_read"): [{"name": "won_status"}]}
    async with _odoo(answers, odoo_calls) as crm:
        await crm.recent_purchases(since=SINCE, until=UNTIL, limit=100)
    lead = next(c for c in odoo_calls if c[0] == "crm.lead")
    assert lead[3]["order"] == "date_closed desc, id desc"
