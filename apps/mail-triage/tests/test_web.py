"""The pages end to end over a real Postgres: a letter becomes a reading a person sees beside the letter, a
button makes a lead with its company and contact, or a note and a task in a deal, or nothing; quotas hold and
nobody gets what is not theirs. The CRM and the model are stand-ins at their boundaries (``conftest``)."""

import html
import uuid
from decimal import Decimal
from typing import Any

import pytest
from alnuzum_contracts.store import Seal
from alnuzum_mail import web
from alnuzum_mail.engine import DEMO_NEW
from alnuzum_mail.service import sample_bytes, samples
from alnuzum_mail.store import Limits, MailStore
from sqlalchemy import text

from alnuzum_crm.types import CrmKind
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.web.security import Sessions

from .conftest import (
    KEYS,
    NAVRUZ,
    ORIGIN,
    ROMASHKA,
    SESSION_SECRET,
    Browser,
    FakeCRM,
    Harness,
    answer,
    rows,
    run_id,
)

OWN_HOOK = {"webhook_url": "https://own.bitrix24.ru/rest/1/ownhook0123456789/"}


def signed(sid: str) -> dict[str, str]:
    return {"__Host-an_sid": Sessions(SESSION_SECRET, secure=True).sign(sid)}


def store_of(h: Harness) -> MailStore:
    return MailStore(h.app.state.sessionmaker, Seal(Keyring.from_env(KEYS)))


async def sql(engine: Any, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


def kinds(h: Harness) -> list[str]:
    return [k for k, _, _ in h.crm.writes]


# --- the demo end to end ---


async def test_a_new_request_binds_to_the_company_by_its_inn_and_makes_one_deal(
    make: Any, engine: Any
) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send())
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "Ромашка" in page and "по ИНН" in page
    assert '<mark data-f="inn">ИНН 305123456</mark>' in page  # hovering the field lights its fragment
    assert '<div class="it" data-f="inn"' in page and "/static/mail/mail.css" in page
    assert "<script" not in page
    assert await b.write(run, cls="new", company="1") == "written"
    assert kinds(h) == ["link", "deal", "note", "task"]  # the company and the contact found: no duplicate
    deal = h.crm.writes[1][2]
    assert deal.title.startswith(DEMO_NEW) and deal.company_id == "1" and deal.contact_id == "c1"
    assert deal.tags == ("al-nuzum", "demo") and deal.responsible_id is None
    note = h.crm.writes[2][2]
    assert "305123456" in note and "по ИНН" in note and "&laquo;" not in note
    assert await rows(engine, "SELECT kind FROM mail_demo_records") == [("deal",)]
    assert await rows(engine, "SELECT status, deal_url FROM mail_runs") == [("written", None)]
    done = (await b.http.get(f"/ru/mail/{run}?n=written")).text
    assert "Записано" in done or "записан" in done.lower()


async def test_a_reply_goes_into_the_demo_deal_and_creates_nothing(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("reply"))
    b = Browser(h.app)
    run = run_id(await b.send(sample="reply"))
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "Демо · Ромашка: электрощитовая" in page
    assert "Ромашка: чужая сделка" not in page  # the demo lists only its own deals
    assert "история переписки" in page.lower() or "истори" in page
    assert await b.write(run, cls="reply", deal="13") == "no_choice"  # a deal the page did not offer
    assert await b.write(run, cls="reply", deal="11") == "written"
    assert [(k, d) for k, d, _ in h.crm.writes] == [("note", "11"), ("task", "11")]
    assert await rows(engine, "SELECT count(*) FROM mail_demo_records") == [(0,)]


async def test_junk_closes_the_run_and_the_crm_gets_nothing(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("junk", {}, request="Реклама выставки."))
    b = Browser(h.app)
    run = run_id(await b.send(sample="junk"))
    assert await b.write(run, cls="junk") == "closed"
    assert h.crm.writes == []
    assert await rows(engine, "SELECT status FROM mail_runs") == [("closed",)]
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "disabled" in page


async def test_a_person_overrules_the_class_the_letter_asked_for(make: Any, engine: Any) -> None:
    """The injection sample asks to be junk: the model obeyed, the person did not."""
    bahor = {"company": ("ООО «Bahor Market»", "ООО «Bahor Market»"), "inn": ("308456789", "ИНН 308456789")}
    h: Harness = await make(
        answer("junk", bahor | {"phone": ("+998 71 200 99 99", "тел. +998 71 200 99 99")})
    )
    b = Browser(h.app)
    run = run_id(await b.send(sample="injection"))
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "Мусор" in page or "мусор" in page
    assert await b.write(run, cls="new", company="new") == "written"
    assert kinds(h) == ["company", "contact", "deal", "note", "task"]
    company = h.crm.writes[0][2]
    assert (company.name, company.inn, company.phone) == (
        "ООО «Bahor Market»",
        "308456789",
        None,
    )  # a phone the letter does not hold is dropped


async def test_a_retry_after_a_failure_goes_on_into_the_same_deal(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send())
    h.crm.fail_on = "note"
    assert await b.write(run, cls="new", company="1") == "failed"
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "disabled" in page  # the choice is kept: a retry cannot change it
    assert await b.write(run) == "written"
    assert kinds(h) == ["link", "deal", "note", "task"]  # one deal, the note written once
    steps = (await rows(engine, "SELECT steps FROM mail_runs"))[0][0]
    assert steps == ["company:1", "contact:c1", f"deal:{h.crm.writes[1][2] and 'd1'}", "note", "task"]


async def test_a_new_company_makes_the_company_the_contact_and_the_deal(make: Any, engine: Any) -> None:
    h: Harness = await make(
        answer("new", NAVRUZ | {"company": ("ООО «Navruz Stroy Plus»", "ООО «Navruz Stroy»")})
    )
    h.crm.companies.clear()
    h.crm.contacts.clear()
    b = Browser(h.app)
    run = run_id(await b.send(sample="new-pdf-card"))
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "Карточка предприятия Navruz Stroy.pdf" in page
    assert await b.write(run, cls="new", company="new") == "no_choice"  # «Navruz Stroy Plus» is not written
    h2: Harness = await make(answer("new", NAVRUZ))
    h2.crm.companies.clear()
    h2.crm.contacts.clear()
    b2 = Browser(h2.app, "203.0.113.8")
    run = run_id(await b2.send(sample="new-pdf-card"))
    assert await b2.write(run, cls="new", company="new") == "written"
    assert kinds(h2) == ["company", "contact", "deal", "note", "task"]
    company = h2.crm.writes[0][2]
    assert company.name == "ООО «Navruz Stroy»" and company.inn == "306234567"
    assert sorted(await rows(engine, "SELECT kind FROM mail_demo_records")) == [
        ("company",),
        ("contact",),
        ("deal",),
    ]


async def test_the_demo_stops_creating_records_past_its_budget(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("new", NAVRUZ), mail_demo_new_per_day=2)
    h.crm.companies.clear()
    h.crm.contacts.clear()
    b = Browser(h.app)
    run = run_id(await b.send(sample="new-pdf-card"))
    assert await b.write(run, cls="new", company="new") == "full_new"
    assert h.crm.writes == []
    # a reply creates nothing: it still goes
    h.crm.companies["1"] = FakeCRM().companies["1"]
    h.messages.script.append(answer("reply"))
    run = run_id(await b.send(sample="reply"))
    assert await b.write(run, cls="reply", deal="11") == "written"


async def test_the_demo_stops_creating_records_past_its_budget_in_all(make: Any, engine: Any) -> None:
    h: Harness = await make(mail_demo_new_total=1)
    h.crm.contacts.clear()
    b = Browser(h.app)
    run = run_id(await b.send())
    assert await b.write(run, cls="new", company="1") == "full_new"  # a contact and a deal: two records


AMO = "demo:amocrm"


async def test_amocrm_an_address_makes_one_new_client_a_day(make: Any, engine: Any) -> None:
    """A technical account keeps 100 companies and contacts in all: one address makes at most one new
    company and one new contact a day there; past it the page offers no new company and the deal goes
    without a new contact."""
    h: Harness = await make(answer("new", NAVRUZ))
    h.crm.companies.clear()
    h.crm.contacts.clear()
    b = Browser(h.app)
    run = run_id(await b.send(target=AMO, sample="new-pdf-card"))
    assert await b.write(run, cls="new", company="new") == "written"
    assert kinds(h) == ["company", "contact", "deal", "note", "task"]
    made = await rows(engine, "SELECT kind, ip_key FROM mail_demo_records ORDER BY kind")
    assert made == [("company", "203.0.113.7"), ("contact", "203.0.113.7"), ("deal", "203.0.113.7")]
    h.crm.writes.clear()
    h.messages.script.append(answer("new", NAVRUZ))
    run = run_id(await b.send(target=AMO, sample="new-pdf-card"))
    page = html.unescape((await b.http.get(f"/ru/mail/{run}")).text)
    assert "Новая компания в демо сейчас не создаётся" in page
    assert 'name="company" value="new"' not in page
    assert await b.write(run, cls="new", company="new") == "no_choice"
    assert await b.write(run, cls="new", company="none") == "written"
    assert kinds(h) == ["deal", "note", "task"]  # no new contact either
    # another address still may
    h.messages.script.append(answer("new", NAVRUZ))
    other = Browser(h.app, "203.0.113.50")
    run = run_id(await other.send(target=AMO, sample="new-pdf-card"))
    assert 'name="company" value="new"' in (await other.http.get(f"/ru/mail/{run}")).text


async def test_amocrm_admits_new_clients_by_the_live_count_of_the_account(make: Any, engine: Any) -> None:
    """Not a ceiling of the demo's own journal for ever: the account's own count of companies and contacts
    (taken every ten minutes) with what is being created stays within 70 of its 100."""
    h: Harness = await make(answer("new", NAVRUZ))
    h.crm.companies.clear()
    h.crm.contacts.clear()
    h.crm.clients_count = 69
    b = Browser(h.app)
    run = run_id(await b.send(target=AMO, sample="new-pdf-card"))
    assert 'name="company" value="new"' not in (await b.http.get(f"/ru/mail/{run}")).text
    h2: Harness = await make(answer("new", NAVRUZ), answer("new", NAVRUZ))
    h2.crm.companies.clear()
    h2.crm.contacts.clear()
    h2.crm.clients_count = 67
    b2 = Browser(h2.app, "203.0.113.51")
    run = run_id(await b2.send(target=AMO, sample="new-pdf-card"))
    assert await b2.write(run, cls="new", company="new") == "written"  # 67 + 2 <= 70
    # the count is not taken again for ten minutes: what the demo made since is added to it
    b3 = Browser(h2.app, "203.0.113.52")
    run = run_id(await b3.send(target=AMO, sample="new-pdf-card"))
    assert 'name="company" value="new"' not in (await b3.http.get(f"/ru/mail/{run}")).text  # 67 + 2 + 2 > 70
    assert h2.crm.counted == 1


async def test_amocrm_that_does_not_count_makes_nothing_new(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("new", NAVRUZ))
    h.crm.companies.clear()
    h.crm.contacts.clear()
    h.crm.fail_on = "count"
    b = Browser(h.app)
    run = run_id(await b.send(target=AMO, sample="new-pdf-card"))
    assert 'name="company" value="new"' not in (await b.http.get(f"/ru/mail/{run}")).text
    # it answers again by the write, but the page offered no new company: the deal goes without
    assert await b.write(run, cls="new", company="none") == "written"
    assert kinds(h) == ["deal", "note", "task"]


async def test_a_write_past_the_room_turns_the_page_to_no_new_company(make: Any, engine: Any) -> None:
    """Room at the reading, none by the write: «full_new», and the page no longer offers what it cannot make."""
    h: Harness = await make(answer("new", NAVRUZ))
    h.crm.companies.clear()
    h.crm.contacts.clear()
    b = Browser(h.app)
    run = run_id(await b.send(target=AMO, sample="new-pdf-card"))
    h.mail._counts["amocrm"] = (70, h.mail._counts["amocrm"][1])
    assert await b.write(run, cls="new", company="new") == "full_new"
    assert h.crm.writes == []
    assert 'name="company" value="new"' not in (await b.http.get(f"/ru/mail/{run}")).text
    assert await b.write(run, cls="new", company="none") == "written"
    assert kinds(h) == ["deal", "note", "task"]


# --- what the person may choose ---


async def test_an_unbound_letter_cannot_make_a_new_company_until_looked_again(make: Any, engine: Any) -> None:
    h: Harness = await make()
    h.crm.fail_on = "look"
    b = Browser(h.app)
    run = run_id(await b.send())
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "не проверен" in page.lower() or "не провер" in page
    assert await b.write(run, cls="new", company="new") == "no_choice"
    looked = await b.act(run, "look")
    assert looked.headers["location"].endswith("n=looked")
    assert await b.write(run, cls="new", company="1") == "written"


async def test_looks_into_the_crm_are_bounded(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send())
    said = [(await b.act(run, "look")).headers["location"].split("n=")[1] for _ in range(6)]
    assert said == ["looked"] * 5 + ["looks"]


async def test_a_deal_search_adds_only_demo_deals(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("reply", {}))
    b = Browser(h.app)
    run = run_id(await b.send(sample="reply"))
    found = await b.act(run, "search", q="Навруз")
    assert found.headers["location"].endswith("n=found")
    assert await b.write(run, cls="reply", deal="12") == "written"
    nothing = await b.act(run, "search", q="чужая")
    assert nothing.headers["location"] == f"/ru/mail/{run}"  # written: no more searching


async def test_the_choice_is_checked_against_the_page(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send())
    assert await b.write(run, cls="spam") == "no_choice"
    assert await b.write(run, cls="new", company="999") == "no_choice"
    assert await b.write(run, cls="new", company="none") == "written"
    assert kinds(h) == ["deal", "note", "task"]
    deal = h.crm.writes[0][2]
    assert deal.company_id is None and deal.contact_id == "c1"


# --- input and its limits ---


async def test_a_pasted_letter_with_its_headers(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    letter = (
        "От: Алишер Каримов <a.karimov@romashka-demo.uz>\nТема: Запрос КП\n\n"
        "Просим КП на щиты.\n\nООО «Ромашка», ИНН 305123456\nГлавный инженер\nмоб.: +998 90 100 11 01\n"
        "Алишер Каримов\nromashka-demo.uz"
    )
    run = run_id(await b.send(sample=None, text=letter))
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "Запрос КП" in page and '<mark data-f="inn">ИНН 305123456</mark>' in page


async def test_input_is_refused_with_its_reason_never_cut(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    empty = await b.send(sample=None)
    assert empty.status_code == 400
    big = await b.send(sample=None, text="Добрый день. " + "Очень длинное письмо. " * 1200)
    assert big.status_code == 422 and "20" in html.unescape(big.text)
    not_mail = await b.send(sample=None, file=("x.eml", b"%PDF-1.7 not a letter"))
    assert not_mail.status_code == 422
    own_sample = await b.send(sample="nope")
    assert own_sample.status_code == 400
    assert await rows(engine, "SELECT status FROM mail_runs") == [("rejected",), ("rejected",)]
    assert h.messages.requests == []  # no word of them reached the model


async def test_an_eml_file_is_read_in_a_child_process(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("new", NAVRUZ))
    b = Browser(h.app)
    run = run_id(await b.send(sample=None, file=("card.eml", sample_bytes("new-pdf-card.eml"))))
    prompt = h.messages.requests[0]["messages"][0]["content"][0]["text"]
    assert '<attachment id="a1"' in prompt and "306234567" in prompt
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert '<mark data-f="inn">ИНН 306234567</mark>' in page or "306234567" in page


async def test_the_eleventh_run_of_a_session_is_refused(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    for _ in range(Limits().runs_per_owner):
        run = run_id(await b.send())
        assert (await b.act(run, "erase")).headers["location"] == "/ru/mail?n=erased"
    over = await b.send()
    assert over.status_code == 429  # erasing never gives a run back
    assert await rows(engine, "SELECT DISTINCT status FROM mail_runs") == [("erased",)]


async def test_demo_writes_of_a_session_are_counted(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("reply"))
    b = Browser(h.app)
    said = []
    for n in range(6):
        h.messages.script.append(answer("reply"))
        run = run_id(await b.send(sample="reply"))
        said.append(await b.write(run, cls="reply", deal="11"))
        if n == 0:
            await b.act(run, "erase")
    assert said == ["written"] * 5 + ["quota"]


async def test_the_money_ceiling_refuses_before_the_model(make: Any, engine: Any) -> None:
    h: Harness = await make(mail_usd_per_day=Decimal("0.01"))
    b = Browser(h.app)
    refused = await b.send()
    assert refused.status_code == 422 and h.messages.requests == []
    h2: Harness = await make(mail_usd_per_day=Decimal("0.5"))
    await sql(
        engine,
        "INSERT INTO mail_spend (id, owner, kind, usd, units, ok, created_at) "
        "VALUES (:i, 'x', 'triage', 0.49, 1, true, now())",
        i=uuid.uuid4(),
    )
    over = await Browser(h2.app, "203.0.113.30").send()
    assert over.status_code == 422 and h2.messages.requests == []


async def test_one_address_takes_its_share_of_the_money_not_all_of_it(make: Any, engine: Any) -> None:
    """A quarter of the day's dollars an address (at least three worst bookings, at most the whole budget):
    past it that address hears its own limit, the others still go."""
    h: Harness = await make(mail_usd_per_day=Decimal("1.5"))
    await sql(
        engine,
        "INSERT INTO mail_spend (id, owner, kind, usd, units, ok, created_at, ip_key) "
        "VALUES (:i, 'x', 'triage', 0.40, 1, true, now(), '203.0.113.7')",
        i=uuid.uuid4(),
    )
    over = await Browser(h.app).send()
    assert over.status_code == 422 and h.messages.requests == []
    assert "Ваш лимит на разбор писем на сегодня исчерпан" in html.unescape(over.text)
    other = await Browser(h.app, "203.0.113.31").send()
    assert other.status_code == 303 and len(h.messages.requests) == 1
    spent = await rows(engine, "SELECT ip_key FROM mail_spend WHERE owner <> 'x'")
    assert spent == [("203.0.113.31",)]


async def test_a_big_letter_does_not_raise_its_address_share(make: Any, engine: Any) -> None:
    """The share is fixed by the bounds of the intake (a quarter of the day, one heaviest reading at least):
    a letter's own booking never moves it."""
    from alnuzum_mail.model import MAX_BOOKING

    from alnuzum_orders.model import ModelError

    h: Harness = await make(mail_usd_per_day=Decimal("1.5"))
    gate = h.mail.gate(store_of(h), "x", "203.0.113.40")
    await sql(
        engine,
        "INSERT INTO mail_spend (id, owner, kind, usd, units, ok, created_at, ip_key) "
        "VALUES (:i, 'x', 'triage', 0.30, 1, true, now(), '203.0.113.40')",
        i=uuid.uuid4(),
    )
    share = max(Decimal("0.375"), MAX_BOOKING)
    with pytest.raises(ModelError) as big:
        await gate("triage", share - Decimal("0.30") + Decimal("0.01"))
    assert big.value.code == "e_ceiling_ip"
    await gate("triage", share - Decimal("0.30") - Decimal("0.01"))  # the same share for a smaller one
    with pytest.raises(ModelError) as past:
        await gate("triage", MAX_BOOKING + Decimal("0.01"))
    assert past.value.code == "e_too_big"


async def test_on_a_small_budget_an_address_still_reads_letters(make: Any, engine: Any) -> None:
    """The stand's $0.5: a quarter is less than one heaviest booking, the share is never less than one."""
    h: Harness = await make(mail_usd_per_day=Decimal("0.5"))
    b = Browser(h.app)
    assert run_id(await b.send()) and run_id(await b.send())
    assert len(h.messages.requests) == 2


# --- keeping and erasing ---


async def test_the_letter_is_sealed_and_erased_in_24_hours(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send())
    ((sealed, summary),) = await rows(engine, "SELECT sealed, summary FROM mail_runs")
    assert "Ромашка".encode() not in sealed and b"305123456" not in sealed
    assert set(summary) == {"cls", "fields", "dropped", "attachments"}
    assert await b.write(run, cls="new", company="1") == "written"
    await sql(engine, "UPDATE mail_runs SET created_at = now() - interval '25 hours'")
    assert (await b.http.get(f"/ru/mail/{run}")).status_code == 404
    await web._sweep(h.app)
    assert await rows(engine, "SELECT status, sealed, summary FROM mail_runs") == [("erased", None, None)]
    await sql(engine, "UPDATE mail_runs SET written_at = now() - interval '25 hours'")
    await web._sweep(h.app)
    assert await rows(engine, "SELECT count(*) FROM mail_runs") == [(0,)]
    assert await rows(engine, "SELECT count(*) FROM mail_demo_records") == [(1,)]  # the journal stays


async def test_erase_while_writing_waits_for_the_write_then_erases(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send())
    task = h.crm.add_task
    said: list[str] = []

    async def erase_meanwhile(deal_id: str, item: Any) -> str:
        said.append((await b.act(run, "erase")).headers["location"])
        return await task(deal_id, item)

    h.crm.add_task = erase_meanwhile  # type: ignore[method-assign]
    assert await b.write(run, cls="new", company="1") == "written"
    assert said == [f"/ru/mail/{run}?n=erase_pending"]
    ((status, sealed, written_at),) = await rows(engine, "SELECT status, sealed, written_at FROM mail_runs")
    assert status == "erased" and sealed is None and written_at is not None


async def test_runs_left_writing_or_reading_are_released_at_start(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    ids = [run_id(await b.send()) for _ in range(2)]
    await sql(
        engine,
        "UPDATE mail_runs SET status = 'writing', writing_at = now() - interval '31 minutes', "
        "created_at = now() - interval '32 minutes' WHERE id = :i",
        i=uuid.UUID(ids[0]),
    )
    await sql(
        engine,
        "UPDATE mail_runs SET status = 'reading', created_at = now() - interval '31 minutes' WHERE id = :i",
        i=uuid.UUID(ids[1]),
    )
    await web._startup(h.app)
    got = {str(r[0]): r[1] for r in await rows(engine, "SELECT id, status FROM mail_runs")}
    assert got == {ids[0]: "failed", ids[1]: "rejected"}


async def test_somebody_else_finds_nothing(make: Any, engine: Any) -> None:
    h: Harness = await make()
    run = run_id(await Browser(h.app).send())
    other = Browser(h.app, "203.0.113.99")
    await other.csrf()
    assert (await other.http.get(f"/ru/mail/{run}")).status_code == 404
    assert (await other.http.get("/ru/mail/not-a-uuid")).status_code == 404
    assert (await other.http.get("/de/mail")).status_code == 404


# --- the CRM refusing ---


async def test_a_test_account_over_its_limit_makes_the_demo_unavailable(make: Any, engine: Any) -> None:
    from alnuzum_crm.amocrm import ACCOUNT_LIMIT
    from alnuzum_crm.errors import CrmPermissionError

    h: Harness = await make()
    b = Browser(h.app)

    async def full(*_a: Any, **_k: Any) -> str:
        raise CrmPermissionError("Payment Required", crm="bitrix24", status=400, code=ACCOUNT_LIMIT)

    h.crm.add_note = full  # type: ignore[method-assign]
    run = run_id(await b.send())
    assert await b.write(run, cls="new", company="1") == "down"
    run = run_id(await b.send())
    assert await b.write(run, cls="new", company="1") == "down"
    assert await rows(engine, "SELECT count(*) FROM crm_writes") == [(1,)]


async def test_an_unexpected_failure_does_not_leave_the_run_writing(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send())

    async def broken(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("boom")

    h.crm.add_note = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await b.act(run, "write", cls="new", company="1")
    assert await rows(engine, "SELECT status, error FROM mail_runs") == [("failed", "error")]


# --- an own CRM and its rules ---


async def own(h: Harness, sid: str = "s" * 43) -> tuple[Browser, Any]:
    info = await h.app.state.store.create(
        Sessions.owner(sid), CrmKind.BITRIX24, "own", OWN_HOOK, label="own", account="own"
    )
    b = Browser(h.app, "203.0.113.9")
    b.http.cookies.update(signed(sid))
    return b, info


async def test_rules_route_a_new_deal_of_an_own_crm(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b, info = await own(h)
    page = await b.http.get(f"/ru/mail/rules/{info.id}")
    assert page.status_code == 200 and "КП · Продажи" in page.text and "Мария Петрова" in page.text
    form = {"when0": "keyword", "value0": "щиты", "stage0": "C0:QUOTE", "user0": "8"}
    token = __import__("re").search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    bad = await b.http.post(
        f"/ru/mail/rules/{info.id}",
        data={"csrf": token, **form, "stage0": "C0:WON"},
        headers={"origin": ORIGIN},
    )
    assert bad.status_code == 400
    saved = await b.http.post(
        f"/ru/mail/rules/{info.id}", data={"csrf": token, **form}, headers={"origin": ORIGIN}
    )
    assert saved.status_code == 303
    run = run_id(
        await b.send(target=f"conn:{info.id}", sample=None, file=("l.eml", sample_bytes("new-signature.eml")))
    )
    page = (await b.http.get(f"/ru/mail/{run}")).text
    assert "КП · Продажи" in page and "Иван Сидоров" in page
    assert await b.write(run, cls="new", company="1") == "written"
    deal = h.crm.writes[1][2]
    assert deal.stage_id == "C0:QUOTE" and deal.responsible_id == "8" and deal.tags == ("al-nuzum",)
    assert not deal.title.startswith(DEMO_NEW)
    task = h.crm.writes[3][2]
    assert task.responsible_id == "8"
    url = (await rows(engine, "SELECT deal_url FROM mail_runs"))[0][0]
    assert url.endswith("/crm/deal/details/d1/")


async def test_demo_rules_never_show_the_people_of_a_test_account(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    await b.csrf()
    page = await b.http.get("/ru/mail/rules/demo:bitrix24")
    assert page.status_code == 200 and "Мария Петрова" not in page.text and "Новая · Продажи" in page.text


async def test_rules_of_a_crm_that_does_not_answer(make: Any, engine: Any) -> None:
    h: Harness = await make()
    b, info = await own(h)
    h.crm.fail_on = "stages"
    page = await b.http.get(f"/ru/mail/rules/{info.id}")
    assert page.status_code == 200


# --- the pages in both languages ---


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_the_start_page_lists_the_samples(make: Any, lang: str) -> None:
    h: Harness = await make()
    page = (await Browser(h.app).http.get(f"/{lang}/mail")).text
    for s in samples().values():
        assert html.escape(s["title"][lang], quote=False) in page or s["title"][lang] in html.unescape(page)
    assert len(samples()) >= 6


async def test_the_english_run_page(make: Any, engine: Any) -> None:
    h: Harness = await make(answer("new", ROMASHKA, language="en", request="Asks for a quote."))
    b = Browser(h.app)
    run = run_id(await b.send(lang="en"))
    page = (await b.http.get(f"/en/mail/{run}")).text
    assert "Asks for a quote." in page and 'lang="en"' in page


async def test_writes_going_on_hold_their_records_of_the_demo_budget(make: Any, engine: Any) -> None:
    h: Harness = await make(mail_demo_new_per_day=3)
    b = Browser(h.app)
    first = run_id(await b.send())
    second = run_id(await b.send())
    await sql(engine, "UPDATE mail_runs SET status = 'writing' WHERE id = :i", i=uuid.UUID(first))
    assert await b.write(second, cls="new", company="1") == "full_new"  # a deal, and three held by the first
    await sql(engine, "UPDATE mail_runs SET status = 'failed' WHERE id = :i", i=uuid.UUID(first))
    assert await b.write(second, cls="new", company="1") == "written"


async def test_the_demo_quota_of_a_connection_refuses_and_closes_the_adapter(
    make: Any, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alnuzum_platform.store import ConnectionStore, QuotaExceeded

    h: Harness = await make()
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    async def over(*_a: Any, **_k: Any) -> Any:
        raise QuotaExceeded("mail_write")

    h.crm.aclose = aclose  # type: ignore[attr-defined]
    monkeypatch.setattr(ConnectionStore, "start_write", over)
    b = Browser(h.app)
    run = run_id(await b.send())
    assert await b.write(run, cls="new", company="1") == "quota"
    assert closed == [True] and h.crm.writes == []


# --- what the narrow check found small ---


async def test_a_crm_answer_the_binding_cannot_read_leaves_the_letter_unbound(make: Any, engine: Any) -> None:
    h: Harness = await make()

    async def odd(**_k: Any) -> Any:
        raise KeyError("ENTITY_ID")  # an answer of the CRM in a shape the adapter did not expect

    h.crm.find_companies = odd  # type: ignore[method-assign]
    b = Browser(h.app)
    run = run_id(await b.send())
    page = html.unescape((await b.http.get(f"/ru/mail/{run}")).text)
    assert "привязка не проверена" in page
    assert await rows(engine, "SELECT status FROM mail_runs") == [("ready",)]


async def test_an_own_crm_that_hides_its_users_still_routes_by_stage(make: Any, engine: Any) -> None:
    from alnuzum_crm.errors import CrmPermissionError

    h: Harness = await make()

    async def admins_only() -> Any:
        raise CrmPermissionError("only an administrator", crm="bitrix24", status=403)

    h.crm.users = admins_only  # type: ignore[method-assign]
    b, info = await own(h)
    page = await b.http.get(f"/ru/mail/rules/{info.id}")
    assert page.status_code == 200 and "КП · Продажи" in page.text
    token = __import__("re").search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    saved = await b.http.post(
        f"/ru/mail/rules/{info.id}",
        data={"csrf": token, "when0": "keyword", "value0": "щиты", "stage0": "C0:QUOTE"},
        headers={"origin": ORIGIN},
    )
    assert saved.status_code == 303


@pytest.mark.parametrize(
    ("lang", "said"),
    [("ru", "остаются в тестовой CRM и видны другим посетителям"), ("en", "stay in the test CRM")],
)
async def test_the_demo_says_its_records_stay_and_others_see_them(make: Any, lang: str, said: str) -> None:
    h: Harness = await make()
    b = Browser(h.app)
    run = run_id(await b.send(lang=lang))
    assert said in html.unescape((await b.http.get(f"/{lang}/mail/{run}")).text)


async def test_the_request_reaches_a_task_with_no_markup(make: Any, engine: Any) -> None:
    h: Harness = await make(answer(request="Нужен КП [url=https://evil.example]тут[/url] <a href=x>да</a>"))
    b = Browser(h.app)
    run = run_id(await b.send())
    assert await b.write(run, cls="new", company="1") == "written"
    task = next(w[2] for w in h.crm.writes if w[0] == "task")
    deal = next(w[2] for w in h.crm.writes if w[0] == "deal")
    for text_ in (task.text, deal.title):
        assert not set("<>[]") & set(text_), text_
