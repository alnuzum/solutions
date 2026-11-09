"""The pages: who may do what, the whole way from an order to a deal, the quotas and their races."""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from alnuzum_crm.errors import CrmAuthError, CrmUnavailable
from alnuzum_crm.types import CrmKind
from alnuzum_orders import intake
from alnuzum_orders.demo_catalog import DEMO_CATALOG
from alnuzum_orders.pricing import money
from alnuzum_orders.web import MAX_BODY, link_css
from alnuzum_platform.web.security import Sessions

from .conftest import (
    DEMO_AMO_TOKEN,
    DEMO_HOOK,
    DEMO_ZOHO_REFRESH,
    ORIGIN,
    Browser,
    Harness,
    choose_first,
    extract_lines,
    questions,
    rows,
    run_id,
)


def choose_none(request: dict[str, Any]) -> dict[str, Any]:
    return {"answers": [{"line": k, "choice": None, "confidence": 0.9} for k, _ in questions(request)]}


TEXT_ORDER = "Кабель NYM 3x2,5 (Севкабель) - 100 м\nСтабилизатор напряжения 5 кВт - 1 шт"


def no_style_no_script(html: str) -> None:
    assert "<script" not in html
    assert "style=" not in html


async def written_run(h: Harness, b: Browser, target: str = "demo:bitrix24") -> str:
    rid = run_id(await b.order(target, sample="order-ru.xlsx"))
    resp = await b.decide(rid, {"action": "write"})
    assert resp.status_code == 303 and resp.headers["location"].endswith("?n=written"), resp.headers
    return rid


# --- who may do what ---


@pytest.mark.parametrize(
    ("method", "path", "code"),
    [
        ("GET", "/ru/orders/{id}", 401),
        ("GET", "/ru/orders/rules/demo:bitrix24", 401),
        ("POST", "/ru/orders", 403),
        ("POST", "/ru/orders/{id}", 403),
        ("POST", "/ru/orders/rules/demo:bitrix24", 403),
        ("POST", "/en/orders/rules/demo:bitrix24", 403),
    ],
)
async def test_routes_without_a_session_cookie(make: Any, method: str, path: str, code: int) -> None:
    h = await make(choose_first())
    rid = run_id(await Browser(h.app).order(sample="order-ru.xlsx"))
    stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=ORIGIN)
    data = {"csrf": "x", "target": "demo:bitrix24", "sample": "order-ru.xlsx", "action": "write"}
    resp = await stranger.request(method, path.format(id=rid), data=data)
    assert resp.status_code == code
    forged = {"__Host-an_sid": Sessions("not-our-secret-0123456789abcdefghijkl", secure=True).sign("a" * 43)}
    stranger.cookies.update(forged)
    resp = await stranger.request(method, path.format(id=rid), data=data)
    assert resp.status_code == code
    assert h.crm.deals == {}


async def test_another_session_gets_404_on_a_foreign_run_and_connection(
    make: Any, engine: AsyncEngine
) -> None:
    h = await make(choose_first())
    owner, stranger = Browser(h.app), Browser(h.app)
    rid = run_id(await owner.order(sample="order-ru.xlsx"))
    st = h.app.state.store
    conn = await st.create(owner.owner(), CrmKind.BITRIX24, "", {"webhook_url": DEMO_HOOK}, label="mine")
    token = await stranger.csrf()
    assert (await stranger.http.get(f"/ru/orders/{rid}")).status_code == 404
    resp = await stranger.http.post(f"/ru/orders/{rid}", data={"csrf": token, "action": "write"})
    assert resp.status_code == 404
    for target in (f"conn:{conn.id}", f"conn:{uuid.uuid4()}", "conn:junk", "demo:nope", "elsewhere"):
        resp = await stranger.order(target, sample="order-ru.xlsx", csrf=token)
        assert resp.status_code == 404, target
        assert (await stranger.http.get(f"/ru/orders/rules/{target}")).status_code == 404, target
    for bad in ("not-a-uuid", str(uuid.uuid4())):
        assert (await owner.http.get(f"/ru/orders/{bad}")).status_code == 404
    assert (await owner.http.get("/de/orders")).status_code == 404
    assert h.crm.deals == {}
    assert await rows(engine, "SELECT 1 FROM orders_runs WHERE owner = :o", o=stranger.owner()) == []


async def test_csrf_and_origin_are_required(make: Any) -> None:
    h = await make(choose_first())
    b = Browser(h.app)
    rid = run_id(await b.order(sample="order-ru.xlsx"))
    assert (await b.order(sample="order-ru.xlsx", csrf="forged")).status_code == 403
    token = await b.csrf()
    resp = await b.http.post(
        f"/ru/orders/{rid}",
        data={"csrf": token, "action": "write"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert h.crm.deals == {}


# --- the way from an order to a deal ---


async def test_demo_order_to_deal_with_products_note_task_and_readback(
    make: Any, engine: AsyncEngine
) -> None:
    h = await make(choose_first())
    b = Browser(h.app)
    index = await b.http.get("/ru/orders")
    assert index.status_code == 200
    no_style_no_script(index.text)
    assert re.findall(r'value="demo:(\w+)"', index.text) == ["amocrm", "bitrix24", "zoho"]
    assert "Разборов на сегодня осталось: 10" in index.text
    rid = run_id(await b.order(sample="order-ru.xlsx"))
    page = await b.http.get(f"/ru/orders/{rid}")
    assert page.status_code == 200
    no_style_no_script(page.text)
    assert "Стабилизатор" in page.text and 'name="c16" value="skip"' in page.text
    assert h.crm.list_calls == 1  # 199 products, one page of 200
    # the person leaves the unknown line out and fixes the coil of the heat shrink, then writes
    resp = await b.decide(rid, {"c16": "skip", "q10": "20", "action": "recalc"})
    assert resp.headers["location"] == f"/ru/orders/{rid}"
    resp = await b.decide(rid, {"action": "write"})
    assert resp.headers["location"] == f"/ru/orders/{rid}?n=written"
    ((deal_id, deal),) = h.crm.deals.items()
    assert {line.sku[:3] for line in deal.lines if line.sku} == {"EL-"}
    assert deal.tags == ("al-nuzum", "demo")
    assert deal.total is None  # Bitrix24 keeps line prices
    assert h.crm.notes[0][0] == deal_id
    assert "Итого" in h.crm.notes[0][1]
    shown = await b.http.get(f"/ru/orders/{rid}?n=written")
    no_style_no_script(shown.text)
    assert f"Сделка № {deal_id}" in shown.text and "Сделка записана" in shown.text
    # a second click writes nothing more
    await b.decide(rid, {"action": "write"})
    assert len(h.crm.deals) == 1
    (journal,) = await rows(engine, "SELECT action, ok, record_id FROM crm_writes")
    assert tuple(journal) == ("order_deal", True, deal_id)


async def test_own_crm_order_written_with_a_link_and_rules_of_its_own(make: Any) -> None:
    h = await make(extract_lines, choose_none)
    b = Browser(h.app)
    await b.http.get("/en/orders")
    conn = await h.app.state.store.create(
        b.owner(), CrmKind.BITRIX24, "", {"webhook_url": DEMO_HOOK}, label="mine"
    )
    target = f"conn:{conn.id}"
    token = await b.csrf("/en/orders")
    saved = await b.http.post(
        f"/en/orders/rules/{target}",
        data={"csrf": token, "cat0": "Кабель и провод", "pct0": "10", "round": "1", "default": "0"},
    )
    assert saved.headers["location"] == f"/en/orders/rules/{target}?n=saved"
    rid = run_id(await b.order(target, text=TEXT_ORDER, lang="en"))
    page = await b.http.get(f"/en/orders/{rid}")
    no_style_no_script(page.text)
    resp = await b.decide(rid, {"action": "write"}, lang="en")
    assert resp.headers["location"].endswith("?n=written")
    (deal,) = h.crm.deals.values()
    assert deal.tags == ("al-nuzum",)
    base = next(i.price_rub for i in DEMO_CATALOG if i.sku == "EL-CB-012")
    assert [(x.sku, x.unit_price) for x in deal.lines] == [
        ("EL-CB-012", money(base * Decimal("1.1"), Decimal(1)))
    ]
    assert "https://crm.example/deal/" in (await b.http.get(f"/en/orders/{rid}")).text
    assert "Check 1 line(s)" in h.crm.tasks[0][1].text


async def test_rules_page_refuses_bad_rules_and_returns_to_the_run(make: Any, engine: AsyncEngine) -> None:
    h = await make(choose_first())
    b = Browser(h.app)
    rid = run_id(await b.order(sample="order-ru.xlsx"))
    page = await b.http.get(f"/ru/orders/rules/demo:bitrix24?back={rid}")
    assert page.status_code == 200 and 'value="30000"' in page.text
    no_style_no_script(page.text)
    token = await b.csrf()
    bad = await b.http.post(
        "/ru/orders/rules/demo:bitrix24", data={"csrf": token, "cat0": "A", "pct0": "500"}
    )
    assert bad.status_code == 400 and 'value="500"' in bad.text
    ok = await b.http.post(
        "/ru/orders/rules/demo:bitrix24",
        data={"csrf": token, "default": "50", "from0": "1000000", "vol0": "2", "round": "0.01", "back": rid},
    )
    assert ok.headers["location"] == f"/ru/orders/{rid}"
    assert "+50 %" in (await b.http.get(f"/ru/orders/{rid}")).text
    assert 'value="1000000"' in (await b.http.get("/ru/orders/rules/demo:bitrix24")).text
    (stored,) = await rows(engine, "SELECT rules FROM orders_rules WHERE owner = :o", o=b.owner())
    assert stored[0]["default"] == "50"


async def test_refusals_of_the_order_form(make: Any, engine: AsyncEngine) -> None:
    h = await make()
    b = Browser(h.app)
    token = await b.csrf()
    big = await b.http.post(
        "/ru/orders",
        content=b"x" * 10,
        headers={"content-length": str(MAX_BODY + 1), "content-type": "text/plain"},
    )
    assert big.status_code == 413
    resp = await b.order(file=("order.pdf", b"MZ\x90\x00 exe"), csrf=token)
    assert resp.status_code == 400
    assert (await b.order(csrf=token)).status_code == 400
    assert (await b.order(sample="../../etc/passwd", csrf=token)).status_code == 400
    # what is refused before reading costs nothing; a file that was read and refused is counted
    assert await rows(engine, "SELECT status, error FROM orders_runs") == [("rejected", "e_type")]


async def test_without_a_model_only_tables_with_a_header_are_read(make: Any) -> None:
    h = await make(model=False)
    b = Browser(h.app)
    assert (await b.order(text=TEXT_ORDER)).status_code == 503
    run_id(await b.order(sample="order-ru.xlsx"))


async def test_catalog_errors_and_a_failed_write(make: Any, engine: AsyncEngine) -> None:
    h = await make(choose_first(), choose_first())
    b = Browser(h.app)
    h.crm.fail_list = CrmUnavailable("down", crm="bitrix24")
    assert (await b.order(sample="order-ru.xlsx")).status_code == 502
    h.crm.fail_list = None
    h.crm.products = []
    assert (await b.order("demo:amocrm", sample="order-ru.xlsx")).status_code == 409
    # both attempts are counted
    assert len(await rows(engine, "SELECT 1 FROM orders_runs WHERE status = 'rejected'")) == 2


async def test_own_crm_refusing_the_keys_turns_the_connection_to_reconnect(
    make: Any, engine: AsyncEngine
) -> None:
    h = await make(choose_first())
    b = Browser(h.app)
    await b.http.get("/ru/orders")
    conn = await h.app.state.store.create(
        b.owner(), CrmKind.BITRIX24, "", {"webhook_url": DEMO_HOOK}, label="mine"
    )
    rid = run_id(await b.order(f"conn:{conn.id}", sample="order-ru.xlsx"))
    h.crm.fail = CrmAuthError("no", crm="bitrix24")
    resp = await b.decide(rid, {"action": "write"})
    assert resp.headers["location"].endswith("?n=failed")
    (status,) = await rows(engine, "SELECT status FROM crm_connections WHERE id = :i", i=conn.id)
    assert status[0] == "reauth_required"
    (run,) = await rows(engine, "SELECT status, error FROM orders_runs")
    assert tuple(run) == ("failed", "auth")
    # the connection is gone for new orders
    assert (await b.order(f"conn:{conn.id}", sample="order-ru.xlsx")).status_code == 409


async def test_demo_write_that_failed_can_be_tried_again(make: Any) -> None:
    h = await make(choose_first())
    b = Browser(h.app)
    rid = run_id(await b.order(sample="order-ru.xlsx"))
    h.crm.fail = CrmUnavailable("down", crm="bitrix24")
    assert (await b.decide(rid, {"action": "write"})).headers["location"].endswith("?n=failed")
    h.crm.fail = None
    assert (await b.decide(rid, {"action": "write"})).headers["location"].endswith("?n=written")


async def test_static_parts_and_the_sample_photo(make: Any) -> None:
    h = await make()
    b = Browser(h.app)
    css = await b.http.get("/orders/link.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert css.text == link_css()
    assert css.text.count("{background") == 400
    photo = await b.http.get("/orders/sample/photo-list.jpg")
    assert photo.status_code == 200 and photo.content.startswith(b"\xff\xd8\xff")
    for name in ("order-ru.xlsx", "secret.jpg", "..%2Fweb.py"):
        assert (await b.http.get(f"/orders/sample/{name}")).status_code == 404


# --- quotas and their races ---


async def test_runs_per_session_counted_before_reading_parallel_and_failed_ones_too(
    make: Any, engine: AsyncEngine
) -> None:
    """R1: ten parallel orders against a quota of three: exactly three are read; each failed too."""
    h = await make(*(["not json"] * 10), orders_runs_per_session=3, orders_runs_per_ip=100)
    b = Browser(h.app)
    token = await b.csrf()
    answers = await asyncio.gather(*(b.order(text=TEXT_ORDER, csrf=token) for _ in range(10)))
    codes = sorted(r.status_code for r in answers)
    assert codes == [429] * 7 + [503] * 3, codes
    assert len(h.messages.requests) == 3
    assert len(await rows(engine, "SELECT 1 FROM orders_runs WHERE status = 'rejected'")) == 3
    # the failed ones keep counting
    again = await b.order(sample="order-ru.xlsx", csrf=token)
    assert again.status_code == 429
    assert "Разборов на сегодня осталось: 0" in again.text


async def test_runs_per_address_across_sessions(make: Any) -> None:
    h = await make(*([choose_first()] * 6), orders_runs_per_session=10, orders_runs_per_ip=4)
    browsers = [Browser(h.app, ip="2001:db8:1:1::5"), Browser(h.app, ip="2001:db8:1:1::6")]
    tokens = [await x.csrf() for x in browsers]
    answers = await asyncio.gather(
        *(browsers[i % 2].order(sample="order-ru.xlsx", csrf=tokens[i % 2]) for i in range(8))
    )
    assert sorted(r.status_code for r in answers) == [303] * 4 + [429] * 4
    other = Browser(h.app, ip="198.51.100.9")
    assert (await other.order(sample="order-ru.xlsx")).status_code == 303


async def test_demo_writes_per_session_are_counted_with_the_status_change(make: Any) -> None:
    """R2: five runs written in parallel against a demo quota of two: exactly two deals."""
    h = await make(*([choose_first()] * 5), orders_demo_writes_per_session=2)
    b = Browser(h.app)
    runs = [run_id(await b.order(sample="order-ru.xlsx")) for _ in range(5)]
    token = await b.csrf()
    h.crm.gate = asyncio.Event()

    async def write(rid: str) -> httpx.Response:
        return await b.http.post(f"/ru/orders/{rid}", data={"csrf": token, "action": "write"})

    pending = [asyncio.create_task(write(r)) for r in runs]
    await asyncio.sleep(0.3)
    h.crm.gate.set()
    answers = await asyncio.gather(*pending)
    notes = sorted(r.headers["location"].rsplit("?n=", 1)[-1] for r in answers)
    assert notes == ["quota"] * 3 + ["written"] * 2, notes
    assert len(h.crm.deals) == 2
    refused = next(r for r, a in zip(runs, answers, strict=True) if a.headers["location"].endswith("quota"))
    page = await b.http.get(f"/ru/orders/{refused}")
    assert "Записей в демо на сегодня осталось: 0" in page.text


async def test_model_ceiling_of_the_whole_solution(make: Any, engine: AsyncEngine) -> None:
    h = await make(*([extract_lines] * 4), orders_model_calls_per_day=2)
    sessions = [Browser(h.app, ip=f"198.51.100.{i}") for i in range(4)]
    answers = await asyncio.gather(*(x.order(text="Кабель NYM 3x2,5 (Севкабель) - 100 м") for x in sessions))
    assert sorted(r.status_code for r in answers) == [303, 303, 503, 503]
    assert len(h.messages.requests) == 2
    (calls,) = await rows(engine, "SELECT count(*), sum(input_tokens) FROM orders_model_calls")
    assert tuple(calls) == (2, 2000)


async def test_sweep_erases_old_runs_and_calls(make: Any, engine: AsyncEngine) -> None:
    h = await make(extract_lines)
    b = Browser(h.app)
    rid = run_id(await b.order(text="Кабель NYM 3x2,5 (Севкабель) - 100 м"))
    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(text("UPDATE orders_runs SET expires_at = :t"), {"t": datetime.now(UTC)})
        await conn.execute(
            text("UPDATE orders_model_calls SET created_at = :t"),
            {"t": datetime.now(UTC) - timedelta(days=31)},
        )
    await h.orders.sweep(h.app)
    assert await rows(engine, "SELECT 1 FROM orders_runs") == []
    assert await rows(engine, "SELECT 1 FROM orders_model_calls") == []
    assert (await b.http.get(f"/ru/orders/{rid}")).status_code == 404


async def test_demo_secrets_are_kept_encrypted_and_reused_on_restart(make: Any, engine: AsyncEngine) -> None:
    h = await make()
    first = dict(h.app.state.orders_demo)
    assert sorted(first) == ["amocrm", "bitrix24", "zoho"]
    again = await make()
    assert again.app.state.orders_demo == first
    dump = " ".join(str(r) for r in await rows(engine, "SELECT * FROM crm_connections"))
    for secret in (DEMO_HOOK, "demohooksecret123456", DEMO_AMO_TOKEN, DEMO_ZOHO_REFRESH):
        assert secret not in dump
    assert repr(h.orders.reader()).startswith("ClaudeReader(") and "sk-ant" not in repr(h.orders.reader())


async def test_demo_zoho_is_the_developer_edition_and_follows_its_token(make: Any) -> None:
    h = await make()
    first = dict(h.app.state.orders_demo)
    store = h.app.state.store
    assert (await store.get(first["zoho"], "demo")).endpoint == "developer.zohoapis.com"
    # a new refresh token is a new demo connection; the rest stay
    moved = await make(demo_zoho_refresh_token="1000.demo-zoho-refresh.other")
    second = dict(moved.app.state.orders_demo)
    assert second["zoho"] != first["zoho"]
    assert {k: v for k, v in second.items() if k != "zoho"} == {k: v for k, v in first.items() if k != "zoho"}
    assert (await store.secrets(second["zoho"]))["refresh_token"] == "1000.demo-zoho-refresh.other"
    # another environment with the same token is a new connection too
    prod = await make(
        demo_zoho_refresh_token="1000.demo-zoho-refresh.other",
        demo_zoho_api_domain="https://www.zohoapis.com",
    )
    third = prod.app.state.orders_demo["zoho"]
    assert third != second["zoho"]
    assert (await store.get(third, "demo")).endpoint == "www.zohoapis.com"
    # the same token and host again: the connection is reused
    again = await make(
        demo_zoho_refresh_token="1000.demo-zoho-refresh.other",
        demo_zoho_api_domain="https://www.zohoapis.com",
    )
    assert again.app.state.orders_demo["zoho"] == third


@pytest.mark.parametrize(
    "domain",
    ["https://evil.example", "https://developer.zohoapis.com.evil.io", "http://developer.zohoapis.com"],
)
async def test_demo_zoho_outside_zoho_is_not_set_up(make: Any, domain: str) -> None:
    h = await make(demo_zoho_api_domain=domain)
    assert sorted(h.app.state.orders_demo) == ["amocrm", "bitrix24"]


def test_intake_bound_matches_the_body_bound() -> None:
    assert intake.MAX_BYTES < MAX_BODY <= intake.MAX_BYTES + 512 * 1024


# --- I1: reading a file never holds the event loop ---


async def test_health_answers_while_a_file_is_read(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    h = await make(choose_first())
    b = Browser(h.app)
    token = await b.csrf()
    real = intake.read

    def slow(name: str, data: bytes) -> intake.Source:
        time.sleep(1.5)
        return real(name, data)

    monkeypatch.setattr(intake, "read", slow)

    async def health() -> float:
        # timed from before the pause: a loop held by the parse delays the wake-up itself
        began = time.monotonic()
        await asyncio.sleep(0.3)
        resp = await b.http.get("/health")
        assert resp.status_code == 200
        return time.monotonic() - began - 0.3

    order, took = await asyncio.gather(b.order(sample="order-ru.xlsx", csrf=token), health())
    assert order.status_code == 303
    assert took < 1.0, took


async def test_a_file_read_too_long_is_refused_and_counted(
    make: Any, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    h = await make(orders_parse_seconds=0.3, orders_parse_slots=1)
    b = Browser(h.app)
    token = await b.csrf()
    monkeypatch.setattr(intake, "read", lambda name, data: time.sleep(1.0))
    first, second = await asyncio.gather(
        b.order(sample="order-ru.xlsx", csrf=token), b.order(sample="order-ru.xlsx", csrf=token)
    )
    assert sorted((first.status_code, second.status_code)) == [400, 503]
    assert "разбор не уложился" in (first.text + second.text)
    assert "слишком много файлов" in (first.text + second.text)
    assert h.orders.parsing.busy == 1  # the late thread keeps its slot until it ends
    await asyncio.sleep(1.0)
    assert h.orders.parsing.busy == 0
    got = sorted(e for (e,) in await rows(engine, "SELECT error FROM orders_runs"))
    assert got == ["e_busy", "e_slow"]


# --- I2: the ceiling is dollars, each call booked at its worst cost ---


SHORT = "Кабель NYM 3x2,5 (Севкабель) - 100 м"
# within the form's 64 KB a field and the reader's 40 000 characters
LONG = "\n".join(f"Cable NYM 3x2.5 line {n:03}: " + "x" * 100 for n in range(300))


async def test_model_dollars_are_booked_at_the_worst_cost_before_the_call(
    make: Any, engine: AsyncEngine
) -> None:
    from alnuzum_orders.model import EXTRACT_MAX_TOKENS, cost

    h = await make(extract_lines, extract_lines, orders_model_usd_per_day=Decimal("0.30"))
    assert cost(0, EXTRACT_MAX_TOKENS) < Decimal("0.30") < cost(len(LONG), EXTRACT_MAX_TOKENS)
    b = Browser(h.app)
    # a short order fits, twice: the booking is replaced by what the call cost
    run_id(await b.order(text=SHORT))
    run_id(await b.order(text=SHORT))
    (spent,) = await rows(engine, "SELECT sum(usd) FROM orders_model_calls")
    assert spent[0] == cost(1000, 200) * 2
    # the longest document could cost more than the day allows: refused before the model is asked
    refused = await b.order(text=LONG)
    assert refused.status_code == 503
    assert "Суточный лимит системы на все заявки исчерпан" in refused.text
    assert len(h.messages.requests) == 2


async def test_model_dollars_spent_today_stop_the_next_call(make: Any, engine: AsyncEngine) -> None:
    h = await make(extract_lines, orders_model_usd_per_day=Decimal("0.5"), orders_model_calls_per_day=100)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO orders_model_calls (id, created_at, owner, ip_key, kind, input_tokens,"
                " output_tokens, usd, ok) VALUES (:id, now(), 'x', 'x', 'extract', 0, 0, 0.45, true)"
            ),
            {"id": uuid.uuid4()},
        )
    resp = await Browser(h.app).order(text=SHORT)
    assert resp.status_code == 503
    assert "Суточный лимит системы" in resp.text
    assert h.messages.requests == []


async def test_a_call_without_an_answer_keeps_its_booking(make: Any, engine: AsyncEngine) -> None:
    """A broken connection, a rate limit, a 5xx or a time-out may still have cost: the worst-case booking
    stays, and the one retry is booked on its own."""
    import anthropic

    broken = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    h = await make(broken, broken, orders_model_usd_per_day=Decimal("1.5"))
    resp = await Browser(h.app).order(text=SHORT)
    assert resp.status_code == 503
    booked = await rows(engine, "SELECT usd, ok FROM orders_model_calls ORDER BY created_at")
    assert len(booked) == 2
    assert all(usd > Decimal("0.2") and ok is False for usd, ok in booked), booked


# --- I4: the demo accounts are not spent by one visitor for everybody ---


async def test_demo_crm_has_its_own_ceiling_apart_from_the_platform_quota(make: Any) -> None:
    h = await make(
        *([choose_first()] * 5),
        platform={"quota_writes_per_day": 3},
        orders_demo_writes_per_session=5,
        orders_demo_writes_per_crm=4,
    )
    visitors = [Browser(h.app, ip=f"198.51.100.{i}") for i in range(5)]
    for b in visitors[:4]:
        await written_run(h, b)  # the platform's 3 a day no longer stops the fourth
    late = visitors[4]
    rid = run_id(await late.order("demo:bitrix24", sample="order-ru.xlsx"))
    resp = await late.decide(rid, {"action": "write"})
    assert resp.headers["location"].endswith("?n=full")
    page = await late.http.get(f"/ru/orders/{rid}")
    assert "Демо Битрикс24 на сегодня заполнено" in page.text
    assert "Демо других CRM открыто: amoCRM, Zoho CRM." in page.text
    assert re.search(r'value="write" disabled', page.text)
    start = await late.http.get("/ru/orders")
    assert re.search(r'value="demo:bitrix24" required disabled>', start.text)
    assert "на сегодня заполнено" in start.text
    assert len(h.crm.deals) == 4


async def test_demo_writes_per_address_across_sessions(make: Any) -> None:
    h = await make(*([choose_first()] * 4), orders_demo_writes_per_session=5, orders_demo_writes_per_ip=2)
    a, b = Browser(h.app, ip="2001:db8:7:7::1"), Browser(h.app, ip="2001:db8:7:7::2")
    await written_run(h, a)
    await written_run(h, b)
    rid = run_id(await a.order("demo:bitrix24", sample="order-ru.xlsx"))
    assert (await a.decide(rid, {"action": "write"})).headers["location"].endswith("?n=quota")
    other = Browser(h.app, ip="198.51.100.20")
    await written_run(h, other)


async def test_kommo_demo_is_bounded_in_all_by_the_journal(make: Any, engine: AsyncEngine) -> None:
    h = await make(
        *([choose_first()] * 3),
        demo_kommo_domain="acme.kommo.com",
        demo_kommo_token="AT-demo-kommo-secret",
        orders_demo_kommo_total=2,
    )
    conn = h.app.state.orders_demo["kommo"]
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO crm_writes (id, connection_id, owner, crm, action, ok, created_at)"
                " VALUES (:id, :conn, 'demo', 'kommo', 'order_deal', true, now() - interval '3 days')"
            ),
            {"id": uuid.uuid4(), "conn": conn},
        )
    b = Browser(h.app)
    await written_run(h, b, "demo:kommo")
    rid = run_id(await b.order("demo:kommo", sample="order-ru.xlsx"))
    assert (await b.decide(rid, {"action": "write"})).headers["location"].endswith("?n=full_total")
    page = await b.http.get(f"/ru/orders/{rid}")
    assert "Демо Kommo заполнено: тестовый аккаунт больше не принимает сделок." in page.text


async def test_demo_writes_per_address_count_the_address_that_writes(make: Any) -> None:
    """Uploaded from A, written from B: B's writes are counted for B, never for A."""
    from .conftest import ORIGIN as _origin

    h = await make(*([choose_first()] * 4), orders_demo_writes_per_session=10, orders_demo_writes_per_ip=2)
    a = Browser(h.app, ip="198.51.100.1")
    runs = [run_id(await a.order("demo:bitrix24", sample="order-ru.xlsx")) for _ in range(4)]
    token = await a.csrf(f"/ru/orders/{runs[0]}")
    cookies = dict(a.http.cookies)

    async def write_from(ip: str, rid: str) -> str:
        other = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app, client=(ip, 5555)), base_url=_origin, cookies=cookies
        )
        resp = await other.post(f"/ru/orders/{rid}", data={"csrf": token, "action": "write"})
        return resp.headers["location"].rsplit("?n=", 1)[-1]

    assert await write_from("203.0.113.50", runs[0]) == "written"
    assert await write_from("203.0.113.50", runs[1]) == "written"
    assert await write_from("203.0.113.50", runs[2]) == "quota"  # B has written its two
    # A uploaded all four but wrote none: B's deals are not A's
    resp = await a.decide(runs[3], {"action": "write"})
    assert resp.headers["location"].endswith("?n=written")


# --- I5: a Bitrix24 webhook without «catalog» ---


async def test_a_catalog_without_stock_rights_says_which_right_to_add(make: Any) -> None:
    h = await make(choose_first())
    h.crm.stock_denied = True  # type: ignore[attr-defined]
    b = Browser(h.app)
    rid = run_id(await b.order("demo:bitrix24", sample="order-ru.xlsx"))
    page = await b.http.get(f"/ru/orders/{rid}")
    assert "нет права «Торговый каталог (catalog)»" in page.text


async def test_a_drawing_process_that_hangs_is_killed_and_health_answers(
    make: Any, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import os
    import sys
    import time

    from .test_intake import pdf

    pidfile = tmp_path / "pid"
    hang = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
    monkeypatch.setattr(intake, "RENDER_COMMAND", [sys.executable, "-c", hang, str(pidfile)])
    monkeypatch.setattr(intake, "RENDER_SECONDS", 1.0)
    h = await make()
    b = Browser(h.app)
    token = await b.csrf()

    async def health() -> float:
        began = time.monotonic()
        await asyncio.sleep(0.4)
        assert (await b.http.get("/health")).status_code == 200
        return time.monotonic() - began - 0.4

    began = time.monotonic()
    order, took = await asyncio.gather(b.order(file=("scan.pdf", pdf(1)), csrf=token), health())
    assert order.status_code == 400 and "разбор не уложился" in order.text
    assert time.monotonic() - began < 5
    assert took < 1.0, took
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert await rows(engine, "SELECT status, error FROM orders_runs") == [("rejected", "e_slow")]


async def test_the_accuracy_reads_as_a_reader_writes_it(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The line of the reference set: 100 % in Russian, 100% in English, no stray zero, no dash."""
    from alnuzum_orders import web

    h = await make(choose_first())

    async def pages() -> tuple[str, str]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=ORIGIN) as c:
            return (await c.get("/ru/orders")).text, (await c.get("/en/orders")).text

    ru, en = await pages()
    assert (
        "Точность на эталонном наборе: 100\u00a0% строк (67 строк, 6 заявок в четырёх форматах); ошибок нет."
        in ru
    )
    assert (
        "Accuracy on the reference set: 100% of lines (67 lines, 6 orders in four formats); no mistakes."
        in en
    )
    assert "100.0" not in ru + en and "—" not in ru.split("Точность", 1)[1].split("</p>", 1)[0]
    shown = {
        "accuracy": 0.985,
        "lines": 67,
        "orders": 6,
        "wrong": 2,
        "wrong_flagged": 1,
        "date": "2026-11-09",
    }
    monkeypatch.setattr(web, "quality", lambda: shown)
    ru, en = await pages()
    assert "98,5\u00a0% строк" in ru and "ошибок 2, из них система сама пометила спорными 1." in ru
    assert "98.5% of lines" in en and "mistakes: 2, of which the system itself marked 1 as unsure." in en


# --- the Minor pack 1 ---


async def test_a_write_that_breaks_outside_the_crm_errors_leaves_the_run_failed(
    make: Any, engine: AsyncEngine
) -> None:
    h = await make(choose_first())
    b = Browser(h.app)
    rid = run_id(await b.order(sample="order-ru.xlsx"))
    h.crm.fail = KeyError("a bug of an adapter")
    assert (await b.decide(rid, {"action": "write"})).headers["location"].endswith("?n=failed")
    assert [tuple(r) for r in await rows(engine, "SELECT status, error FROM orders_runs")] == [
        ("failed", "error")
    ]
    assert [tuple(r) for r in await rows(engine, "SELECT ok, error FROM crm_writes")] == [(False, "error")]
    h.crm.fail = None  # the visitor tries again
    assert (await b.decide(rid, {"action": "write"})).headers["location"].endswith("?n=written")


async def test_a_reading_that_breaks_outside_the_known_errors_is_rejected(
    make: Any, engine: AsyncEngine
) -> None:
    h = await make(choose_first())
    b = Browser(h.app)
    h.crm.fail_list = KeyError("a bug of an adapter")
    with pytest.raises(KeyError):
        await b.order(sample="order-ru.xlsx")
    assert [tuple(r) for r in await rows(engine, "SELECT status, error FROM orders_runs")] == [
        ("rejected", "error")
    ]


async def test_runs_a_stopped_process_left_writing_or_reading_are_released_at_start(
    make: Any, engine: AsyncEngine
) -> None:
    h = await make(choose_first(), choose_first(), choose_first())
    b = Browser(h.app)
    ids = old, fresh, read = [run_id(await b.order(sample="order-ru.xlsx")) for _ in range(3)]
    stale = (
        "UPDATE orders_runs SET status = CAST(:s AS varchar), created_at = now() - make_interval(mins => :c), "
        "writing_at = CASE WHEN CAST(:s AS varchar) = 'writing' THEN now() - make_interval(mins => :w) END WHERE id = :i"
    )
    async with engine.begin() as conn:
        for rid, state, age in zip(ids, ("writing", "writing", "reading"), (31, 5, 31), strict=True):
            await conn.execute(text(stale), {"s": state, "c": age + 1, "w": age, "i": uuid.UUID(rid)})
    await h.orders.startup(h.app)
    got = {str(r[0]): (r[1], r[2]) for r in await rows(engine, "SELECT id, status, error FROM orders_runs")}
    assert got == {
        old: ("failed", "interrupted"),
        fresh: ("writing", None),
        read: ("rejected", "interrupted"),
    }
    assert (await b.decide(old, {"action": "write"})).headers["location"].endswith("?n=written")


@pytest.mark.parametrize("breaks", ["add_note", "add_task"])
async def test_a_retry_after_the_deal_was_created_writes_into_that_deal(
    make: Any, engine: AsyncEngine, breaks: str
) -> None:
    """The note or the task failed after the deal was made: the retry adds what is missing to that deal."""
    h = await make(choose_first())
    b = Browser(h.app)
    rid = run_id(await b.order(sample="order-ru.xlsx"))
    real = getattr(h.crm, breaks)

    async def down(*_a: Any, **_k: Any) -> Any:
        raise CrmUnavailable("down", crm="bitrix24")

    setattr(h.crm, breaks, down)
    assert (await b.decide(rid, {"action": "write"})).headers["location"].endswith("?n=failed")
    setattr(h.crm, breaks, real)
    clients = (len(h.crm.companies), len(h.crm.contacts))
    assert (await b.decide(rid, {"action": "write"})).headers["location"].endswith("?n=written")
    assert list(h.crm.deals) == ["500"]
    assert [d for d, _ in h.crm.notes] == ["500"]
    assert [d for d, _ in h.crm.tasks] == ["500"]
    assert (len(h.crm.companies), len(h.crm.contacts)) == clients  # the client is not looked up again
    ((deal, status),) = await rows(engine, "SELECT deal_id, status FROM orders_runs")
    assert (deal, status) == ("500", "written")
