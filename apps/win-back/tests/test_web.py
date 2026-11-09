"""The pages end to end against a real database and stand-in CRMs: a scan, the list, the screen of a client, the
task on it; who may do what; quotas that a new cookie does not reset; a demo task that closes itself."""

import re
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from alnuzum_crm.types import CrmKind
from alnuzum_platform.web.security import Sessions
from alnuzum_winback.store import DEMO_TASK_TTL

from .conftest import EVIL, ORIGIN, SESSION_SECRET, Browser, Harness, csrf_of, notice, rows


def signed(sid: str) -> dict[str, str]:
    return {"__Host-an_sid": Sessions(SESSION_SECRET, secure=True).sign(sid)}


def own_browser(h: Harness, sid: str, ip: str = "203.0.113.9") -> Browser:
    b = Browser(h.app, ip)
    b.http.cookies.update(signed(sid))
    return b


async def own_connection(h: Harness, sid: str) -> Any:
    return await h.app.state.store.create(
        Sessions.owner(sid),
        CrmKind.BITRIX24,
        "own",
        {"webhook_url": "https://own.bitrix24.ru/rest/1/ownhook0123456789/"},
        label="own",
        account="own",
    )


def number_of(html: str, name: str) -> int:
    found = re.search(rf'/winback/[0-9a-f-]+/(\d+)"><b>{re.escape(name)}</b>', html)
    assert found, f"{name} is not on the list"
    return int(found.group(1))


async def test_the_start_page_offers_the_demo_crm_in_both_languages(make: Any) -> None:
    h = await make()
    b = Browser(h.app)
    for lang, title in (("ru", "Возврат ушедших клиентов"), ("en", "Win back lapsed clients")):
        page = await b.http.get(f"/{lang}/winback")
        assert page.status_code == 200 and title in page.text
        assert 'value="demo:bitrix24"' in page.text
    assert (await b.http.get("/de/winback")).status_code == 404


async def test_a_demo_scan_lists_who_missed_their_cycle_and_nothing_but_the_demo(make: Any) -> None:
    h = await make()
    b = Browser(h.app)
    scan = await b.scan_id()
    page = await b.http.get(f"/ru/winback/{scan}")
    assert page.status_code == 200
    html = page.text
    assert number_of(html, "Samarqand Tekstil") and number_of(html, "Ромашка")
    assert "Пропустили цикл: 2 · покупают по циклу: 2 · мало истории: 1" in html
    assert "Navruz Stroy · по циклу" in html and "Tashkent City Mall · мало истории" in html
    # the pool of the live tests and a stranger's company are read but never shown
    assert "E2E" not in html and "Чужая" not in html
    # the account is read without products, the names once
    assert h.demo.calls.count("purchases:all:False") == 1 and "names" in h.demo.calls


async def test_the_screen_shows_the_history_beside_the_reasons_that_light_their_orders(make: Any) -> None:
    h = await make()
    b = Browser(h.app)
    scan = await b.scan_id()
    n = number_of((await b.http.get(f"/ru/winback/{scan}")).text, "Ромашка")
    page = await b.http.get(f"/ru/winback/{scan}/{n}")
    assert page.status_code == 200
    html = page.text
    assert (
        "Заказывали раз в ~31 день (10 заказов), последний 15.08.2026 — прошло 87 дней, это 2,8 цикла."
        in html
    )
    assert 'data-r="1"' in html and re.search(r'<li class="r1 [^"]*last">', html)
    assert "Товар EL-AV-001" in html  # products of the opened client only
    assert "срок — 11.11.2026 18:00" in html
    # products are read once for the screen, the second view takes them from the scan
    await b.http.get(f"/ru/winback/{scan}/{n}")
    assert h.demo.calls.count("purchases:1:True") == 1
    css = await b.http.get("/static/winback/winback.css")
    assert '.oc:has(.it[data-r="1"]:hover' in css.text


async def test_a_task_goes_on_the_company_and_a_second_one_is_not_set(make: Any, engine: AsyncEngine) -> None:
    h = await make()
    b = Browser(h.app)
    scan = await b.scan_id()
    n = number_of((await b.http.get(f"/ru/winback/{scan}")).text, "Ромашка")
    assert notice(await b.task(scan, n, "Позвонить Ромашке")) == "task_set"
    [task] = h.demo.open_tasks("1")
    assert task.text == "Позвонить Ромашке" and task.due.isoformat() == "2026-11-11T18:00:00+05:00"
    page = await b.http.get(f"/ru/winback/{scan}/{n}")
    assert "Задача поставлена на компанию, срок — 11.11.2026 18:00" in page.text
    assert 'name="text"' not in page.text  # the open task stands: no button
    # another visitor sees it standing, without the text the first one typed
    other = Browser(h.app, "198.51.100.4")
    scan2 = await other.scan_id()
    page = await other.http.get(f"/ru/winback/{scan2}/{n}")
    assert "уже стоит открытая задача" in page.text and "Позвонить Ромашке" not in page.text
    assert "Через 10 минут здесь можно будет поставить свою" in page.text
    assert [r.status for r in await rows(engine, "SELECT status FROM winback_tasks")] == ["written"]
    writes = await rows(engine, "SELECT action, ok FROM crm_writes")
    assert [(w.action, w.ok) for w in writes] == [("winback_task", True)]


async def test_the_demo_does_not_stick_after_its_time(make: Any) -> None:
    h = await make()
    b, other = Browser(h.app), Browser(h.app, "198.51.100.4")
    scan = await b.scan_id()
    n = number_of((await b.http.get(f"/ru/winback/{scan}")).text, "Ромашка")
    assert notice(await b.task(scan, n)) == "task_set"
    scan2 = await other.scan_id()
    # within its ten minutes the next visitor gets the standing one
    token = csrf_of((await other.http.get("/ru/winback")).text)
    resp = await other.http.post(
        f"/ru/winback/{scan2}/{n}",
        data={"csrf": token, "action": "task", "text": "x"},
        headers={"origin": ORIGIN},
    )
    assert notice(resp) == "standing" and len(h.demo.open_tasks("1")) == 1
    # after them the button is back and the next task closes the old one first
    h.clock.later(DEMO_TASK_TTL.total_seconds() / 60 + 1)
    page = await other.http.get(f"/ru/winback/{scan2}/{n}")
    assert 'name="text"' in page.text
    assert notice(await other.task(scan2, n, "Новая")) == "task_set"
    assert [t.text for t in h.demo.open_tasks("1")] == ["Новая"]
    # a task nobody closed by a visit is closed by the sweep
    h.clock.later(DEMO_TASK_TTL.total_seconds() / 60 + 1)
    assert await h.winback.close_stale(h.app) == 1 and h.demo.open_tasks("1") == []


async def test_a_task_the_demo_did_not_set_does_not_block_it(make: Any) -> None:
    h = await make()
    b = Browser(h.app)
    scan = await b.scan_id()
    n = number_of((await b.http.get(f"/ru/winback/{scan}")).text, "Samarqand Tekstil")
    from alnuzum_crm.types import TaskIn

    from .conftest import NOW

    await h.demo.add_client_task(TaskIn("left by someone", NOW), company_id="3")
    page = await b.http.get(f"/ru/winback/{scan}/{n}")
    assert 'name="text"' in page.text
    assert notice(await b.task(scan, n)) == "task_set"
    assert [t.text for t in h.demo.open_tasks("3")] != ["left by someone"] and len(
        h.demo.open_tasks("3")
    ) == 1


async def test_every_route_wants_its_own_session_and_csrf(make: Any) -> None:
    h = await make()
    b = Browser(h.app)
    scan = await b.scan_id()
    n = number_of((await b.http.get(f"/ru/winback/{scan}")).text, "Ромашка")
    stranger = Browser(h.app, "198.51.100.8")
    # no session at all
    assert (await stranger.http.get(f"/ru/winback/{scan}")).status_code == 401
    assert (await stranger.http.get(f"/ru/winback/{scan}/{n}")).status_code == 401
    # a session of its own: somebody else's scan is not found, nor is a made-up one
    await stranger.http.get("/ru/winback")
    assert (await stranger.http.get(f"/ru/winback/{scan}")).status_code == 404
    assert (await stranger.http.get(f"/ru/winback/{scan}/{n}")).status_code == 404
    assert (await stranger.http.get("/ru/winback/00000000-0000-4000-8000-000000000000")).status_code == 404
    assert (await stranger.http.get("/ru/winback/not-a-uuid")).status_code == 404
    token = csrf_of((await stranger.http.get("/ru/winback")).text)
    resp = await stranger.http.post(
        f"/ru/winback/{scan}/{n}",
        data={"csrf": token, "action": "task", "text": "x"},
        headers={"origin": ORIGIN},
    )
    assert resp.status_code == 404 and h.demo.open_tasks("1") == []
    # its own session but no CSRF, or another origin
    page = await b.http.get(f"/ru/winback/{scan}/{n}")
    resp = await b.http.post(
        f"/ru/winback/{scan}/{n}", data={"action": "task", "text": "x"}, headers={"origin": ORIGIN}
    )
    assert resp.status_code == 403
    resp = await b.http.post(
        f"/ru/winback/{scan}/{n}",
        data={"csrf": csrf_of(page.text), "action": "task", "text": "x"},
        headers={"origin": "https://evil.test"},
    )
    assert resp.status_code == 403
    # a client number beyond the scan
    assert (await b.http.get(f"/ru/winback/{scan}/99")).status_code == 404
    assert (await b.http.get(f"/ru/winback/{scan}/0")).status_code == 404
    # a scan of an own connection of another session
    sid = "s" * 43
    info = await own_connection(h, sid)
    resp = await stranger.http.post(
        "/ru/winback", data={"csrf": token, "target": f"conn:{info.id}"}, headers={"origin": ORIGIN}
    )
    assert resp.status_code == 404
    assert h.demo.open_tasks("1") == []


async def test_quotas_count_the_address_and_not_only_the_cookie(make: Any) -> None:
    h = await make()
    scan, n, set_ = None, 0, []
    for _ in range(3):  # three cookies from one address
        b = Browser(h.app, "203.0.113.50")
        scan = await b.scan_id()
        html = (await b.http.get(f"/ru/winback/{scan}")).text
        for name in ("Ромашка", "Samarqand Tekstil"):
            n = number_of(html, name)
            set_.append(notice(await b.task(scan, n)))
            h.clock.later(DEMO_TASK_TTL.total_seconds() / 60 + 1)
    # each cookie may set 5 a day, the address 8: a new cookie goes on counting the address
    assert set_ == ["task_set"] * 6
    b = Browser(h.app, "203.0.113.50")
    scan = await b.scan_id()
    html = (await b.http.get(f"/ru/winback/{scan}")).text
    assert notice(await b.task(scan, number_of(html, "Ромашка"))) == "task_set"
    h.clock.later(DEMO_TASK_TTL.total_seconds() / 60 + 1)
    assert notice(await b.task(scan, number_of(html, "Samarqand Tekstil"))) == "task_set"
    h.clock.later(DEMO_TASK_TTL.total_seconds() / 60 + 1)
    fresh = Browser(h.app, "203.0.113.50")
    scan = await fresh.scan_id()
    html = (await fresh.http.get(f"/ru/winback/{scan}")).text
    assert notice(await fresh.task(scan, number_of(html, "Ромашка"))) == "quota"


async def test_scans_of_a_day_are_counted_by_the_address(make: Any) -> None:
    h = await make()
    got = []
    for _ in range(31):
        got.append((await Browser(h.app, "203.0.113.60").scan()).status_code)
    assert got[:30] == [303] * 30 and got[30] == 429
    # the demo answer is shared for ten minutes: the account was read once
    assert h.demo.calls.count("purchases:all:False") == 1
    h.clock.later(11)
    assert (await Browser(h.app, "203.0.113.61").scan()).status_code == 303
    assert h.demo.calls.count("purchases:all:False") == 2


async def test_an_own_crm_is_escaped_named_by_contact_too_and_shows_its_open_task(make: Any) -> None:
    h = await make()
    sid = "s" * 43
    info = await own_connection(h, sid)
    b = own_browser(h, sid)
    scan = await b.scan_id(f"conn:{info.id}")
    html = (await b.http.get(f"/ru/winback/{scan}")).text
    assert EVIL not in html and "&lt;script&gt;alert(1)&lt;/script&gt;&#34; onmouseover=&#34;x" in html
    assert "Алишер Каримов · по циклу" in html
    page = (await b.http.get(f"/ru/winback/{scan}/1")).text
    assert "<b>Кабель</b>" not in page and "&lt;b&gt;Кабель&lt;/b&gt;" in page
    assert "в последнем его нет" in page
    assert notice(await b.task(scan, 1, "Вернуть")) == "task_set"
    page = (await b.http.get(f"/ru/winback/{scan}/1")).text
    assert "Задача поставлена на компанию" in page and 'name="text"' not in page
    assert notice(await b.task(scan, 1, "Ещё одна")) == "standing"
    assert [t.text for t in h.own.open_tasks("500")] == ["Вернуть"]


async def test_an_own_scan_stops_at_its_ceiling_and_five_a_day(make: Any) -> None:
    h = await make(winback_cap=4)
    sid = "s" * 43
    info = await own_connection(h, sid)
    b = own_browser(h, sid)
    scan = await b.scan_id(f"conn:{info.id}")
    html = (await b.http.get(f"/ru/winback/{scan}")).text
    assert "Прочитаны первые 4 сделки в порядке CRM" in html
    for _ in range(4):
        assert (await b.scan(f"conn:{info.id}")).status_code == 303
    resp = await b.scan(f"conn:{info.id}")
    assert resp.status_code == 429 and "Сканы на сегодня исчерпаны" in resp.text


async def test_a_crm_that_fails_is_said_and_the_scan_is_rejected(make: Any, engine: AsyncEngine) -> None:
    h = await make()
    h.demo.fail_on = "purchases"
    resp = await Browser(h.app).scan()
    assert resp.status_code == 422 and "CRM не ответила" in resp.text
    assert [(r.status, r.error) for r in await rows(engine, "SELECT status, error FROM winback_scans")] == [
        ("rejected", "unavailable")
    ]


async def test_a_task_the_crm_refuses_is_counted_and_said(make: Any, engine: AsyncEngine) -> None:
    h = await make()
    b = Browser(h.app)
    scan = await b.scan_id()
    n = number_of((await b.http.get(f"/ru/winback/{scan}")).text, "Ромашка")
    h.demo.fail_on = "task"
    assert notice(await b.task(scan, n)) == "unavailable"
    assert notice(await b.task(scan, n, "   ")) == "text"
    assert [(r.status, r.error) for r in await rows(engine, "SELECT status, error FROM winback_tasks")] == [
        ("failed", "unavailable")
    ]


async def test_the_sweep_seeds_only_where_told_and_once_a_day(make: Any, monkeypatch: Any) -> None:
    from alnuzum_winback import demo as plan

    seeded: list[str] = []

    async def seed(crm: Any, kind: str, day: Any) -> int:
        seeded.append(kind)
        return 1

    monkeypatch.setattr(plan, "seed", seed)
    h = await make()
    assert not h.winback.seeds(h.app) and await h.winback.seed(h.app) == {}
    h = await make(winback_seed=True)
    assert await h.winback.seed(h.app) == {"bitrix24": 1}
    assert await h.winback.seed(h.app) == {} and seeded == ["bitrix24"]


async def test_english_pages(make: Any) -> None:
    h = await make()
    b = Browser(h.app)
    resp = await b.scan("demo:bitrix24", lang="en")
    scan = resp.headers["location"].rsplit("/", 1)[-1]
    page = await b.http.get(f"/en/winback/{scan}")
    assert "Missed their cycle: 2" in page.text
    n = number_of(page.text, "Ромашка")
    page = await b.http.get(f"/en/winback/{scan}/{n}")
    assert "Ordered every ~31 days" in page.text and "Set the task" in page.text
    assert isinstance(page, httpx.Response)


async def test_zoho_reads_the_products_of_the_newest_purchases_only(make: Any) -> None:
    h = await make()
    sid = "s" * 43
    info = await h.app.state.store.create(
        Sessions.owner(sid),
        CrmKind.ZOHO,
        "https://www.zohoapis.com",
        {
            "access_token": "a",
            "refresh_token": "r",
            "accounts_server": "https://accounts.zoho.com",
            "expires_at": "2030-01-01T00:00:00+00:00",
        },
        label="z",
        account="z",
    )
    h.own.kind = CrmKind.ZOHO
    b = own_browser(h, sid)
    scan = await b.scan_id(f"conn:{info.id}")
    page = (await b.http.get(f"/ru/winback/{scan}/1")).text
    assert h.own.calls.count("purchases:500:False") == 1 and h.own.calls.count("purchases:500:True") == 1
    assert "в последнем его нет" in page and "Розетка" in page


async def test_rare_branches_give_a_page_and_not_a_500(make: Any, monkeypatch: Any) -> None:
    from alnuzum_platform.store import ConnectionNotFound
    from alnuzum_winback.service import WinbackError

    h = await make()
    b = Browser(h.app)
    scan = await b.scan_id()
    # the demo connection is gone between the list of demos and the scan
    real_get = h.app.state.store.get

    async def gone(conn_id: Any, owner: str) -> Any:
        if owner == "demo":
            raise ConnectionNotFound(str(conn_id))
        return await real_get(conn_id, owner)

    monkeypatch.setattr(h.app.state.store, "get", gone)
    resp = await b.scan()
    assert resp.status_code == 404 and "На главную" in resp.text
    monkeypatch.setattr(h.app.state.store, "get", real_get)

    # the solution's store cannot be built (no keys): the list, the screen and the task say so
    def broken(app: Any) -> Any:
        raise WinbackError("e_unreachable")

    page = await b.http.get(f"/ru/winback/{scan}")
    n = number_of(page.text, "Ромашка")
    token = csrf_of((await b.http.get("/ru/winback")).text)
    monkeypatch.setattr(h.winback, "store", broken)
    for resp in (
        await b.http.get(f"/ru/winback/{scan}"),
        await b.http.get(f"/ru/winback/{scan}/{n}"),
        await b.http.post(
            f"/ru/winback/{scan}/{n}",
            data={"csrf": token, "action": "task", "text": "x"},
            headers={"origin": ORIGIN},
        ),
    ):
        assert resp.status_code == 503 and "Попробуйте позже" in resp.text


async def test_five_addresses_do_not_fill_the_demo_of_a_crm(make: Any) -> None:
    h = await make()
    limits = h.winback.settings.limits()
    assert limits.demo_tasks_per_ip == 8 and limits.demo_tasks_per_crm >= 50 * limits.demo_tasks_per_ip


async def test_a_failed_top_up_is_tried_again_by_the_next_sweep(make: Any, monkeypatch: Any) -> None:
    from alnuzum_crm.errors import CrmUnavailable
    from alnuzum_winback import demo as plan

    tries: list[str] = []

    async def seed(crm: Any, kind: str, day: Any) -> int:
        tries.append(kind)
        if len(tries) == 1:
            raise CrmUnavailable("down", crm=kind)
        return 2

    monkeypatch.setattr(plan, "seed", seed)
    h = await make(winback_seed=True)
    assert await h.winback.seed(h.app) == {}
    assert await h.winback.seed(h.app) == {"bitrix24": 2}
    assert await h.winback.seed(h.app) == {} and tries == ["bitrix24", "bitrix24"]


async def test_an_open_task_of_an_own_crm_stands_with_its_text_and_a_date_only_due(make: Any) -> None:
    from datetime import UTC, datetime

    from alnuzum_crm.types import TaskIn

    h = await make()
    sid = "s" * 43
    info = await own_connection(h, sid)
    b = own_browser(h, sid)
    scan = await b.scan_id(f"conn:{info.id}")
    await h.own.add_client_task(
        TaskIn("Позвонить <срочно>", datetime(2026, 11, 12, tzinfo=UTC)), company_id="500"
    )
    page = (await b.http.get(f"/ru/winback/{scan}/1")).text
    assert "Задача: «Позвонить &lt;срочно&gt;»" in page and "срок 12.11.2026<" in page
    assert 'name="text"' not in page
