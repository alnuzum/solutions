"""From a letter read to what the CRM gets, and to what the page shows.

The plan is built here out of the values the solution kept and the choices a person made on the page: never
out of the letter's words directly. Every text that reaches a CRM is our template with the letter's values put
in, one line each, bounded and HTML-escaped for the CRMs that show notes as HTML."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from alnuzum_contracts.engine import crm_text, for_crm, task_due

from alnuzum_crm.base import CRM
from alnuzum_crm.types import CompanyIn, ContactIn, DealIn, TaskIn
from alnuzum_mail.intake import Letter
from alnuzum_mail.model import FIELDS, Reading

DEMO_PREFIX = "Демо · "  # the demo deals a reply may go into
DEMO_NEW = "Демо-письмо · "  # deals the demo makes: not «Демо · », so the other demos never list them
NOTE_MAX = 3500
VALUE_MAX = 160
EXCERPT = 300

LABELS: dict[str, dict[str, Any]] = {
    "ru": {
        "head": "Письмо клиента · Al-Nuzum",
        "subject": "Тема",
        "from": "От",
        "company": "Компания",
        "inn": "ИНН",
        "request": "Суть",
        "bound": "Привязка",
        "excerpt": "Начало письма",
        "found_by": "найдена ({by})",
        "new_company": "новая",
        "no_company": "без компании",
        "contact_found": "контакт найден",
        "contact_new": "контакт новый",
        "by": {
            "inn": "по ИНН",
            "domain": "по домену",
            "email": "по почте",
            "name": "по названию",
            "phone": "по телефону",
            "inn_other": "ИНН в CRM другой",
        },
        "task": "Ответить на письмо: {request} · Al-Nuzum",
        "task_reply": "Ответить клиенту по сделке: {request} · Al-Nuzum",
        "attachments": "Вложения",
    },
    "en": {
        "head": "A customer's letter · Al-Nuzum",
        "subject": "Subject",
        "from": "From",
        "company": "Company",
        "inn": "Tax ID",
        "request": "Request",
        "bound": "Bound",
        "excerpt": "The letter begins",
        "found_by": "found ({by})",
        "new_company": "new",
        "no_company": "no company",
        "contact_found": "contact found",
        "contact_new": "new contact",
        "by": {
            "inn": "by tax ID",
            "domain": "by domain",
            "email": "by e-mail",
            "name": "by name",
            "phone": "by phone",
            "inn_other": "another INN in the CRM",
        },
        "task": "Reply to the letter: {request} · Al-Nuzum",
        "task_reply": "Reply to the customer on the deal: {request} · Al-Nuzum",
        "attachments": "Attachments",
    },
}


def values(reading: Reading) -> dict[str, str]:
    return {k: reading.fields[k].value for k in FIELDS if k in reading.fields}


@dataclass(frozen=True)
class Plan:
    kind: str  # new | reply | junk
    company_id: str | None = None  # an existing company chosen
    company: CompanyIn | None = None  # one to create
    contact_id: str | None = None  # the existing contact found
    contact: ContactIn | None = None  # one to create
    deal_id: str | None = None  # a reply: the deal chosen
    deal: DealIn | None = None  # a new request: the deal to create
    note: str = ""
    task: TaskIn | None = None

    def creates(self, made: dict[str, str]) -> list[str]:
        """The records this plan still has to create, by kind."""
        if self.kind != "new":
            return []
        out = []
        if "company" not in made and self.company is not None and not self.company_id:
            out.append("company")
        if "contact" not in made and self.contact is not None and not self.contact_id:
            out.append("contact")
        if "deal" not in made:
            out.append("deal")
        return out


def note_text(
    letter: Letter, reading: Reading, binding: dict[str, Any], choice: dict[str, Any], lang: str
) -> str:
    t = LABELS["en" if lang == "en" else "ru"]
    v = values(reading)

    def one(value: str | None) -> str:
        return crm_text(value or "", VALUE_MAX)

    lines = [t["head"]]
    if letter.subject:
        lines.append(f"{t['subject']}: {one(letter.subject)}")
    sender = " · ".join(one(v.get(k)) for k in ("person", "position", "email", "phone") if v.get(k))
    if sender:
        lines.append(f"{t['from']}: {sender}")
    company = " · ".join(
        x
        for x in (
            one(v.get("company")),
            f"{t['inn']} {one(v.get('inn'))}" if v.get("inn") else "",
            one(v.get("website")),
        )
        if x
    )
    if company:
        lines.append(f"{t['company']}: {company}")
    if reading.request:
        lines.append(f"{t['request']}: {one(reading.request)}")
    if choice.get("cls") == "new":
        chosen = choice.get("company")
        found = next((c for c in binding.get("companies") or [] if str(c["id"]) == chosen), None)
        if found:
            company_state = t["found_by"].format(by=", ".join(t["by"][b] for b in found["by"]))
        else:
            company_state = t["new_company"] if chosen == "new" else t["no_company"]
        contact_state = t["contact_found"] if binding.get("contact") else t["contact_new"]
        lines.append(f"{t['bound']}: {t['company'].lower()} {company_state}; {contact_state}")
    read = [a.name for a in letter.attachments if a.text]
    if read:
        lines.append(f"{t['attachments']}: {one(', '.join(read))}")
    excerpt = crm_text(letter.body, EXCERPT)
    if excerpt:
        lines.append(f"{t['excerpt']}: «{excerpt}»")
    return "\n".join(lines)[:NOTE_MAX]


def deal_title(reading: Reading, letter: Letter, *, demo: bool) -> str:
    v = values(reading)
    who = v.get("company") or v.get("person") or v.get("email") or letter.from_email or "—"
    title = f"{crm_text(who, 80)}: {crm_text(reading.request or letter.subject, 100)}"
    return (DEMO_NEW + title if demo else title)[:250]


def plan_of(
    letter: Letter,
    reading: Reading,
    binding: dict[str, Any],
    route: dict[str, Any],
    choice: dict[str, Any],
    *,
    crm: str,
    lang: str,
    demo: bool,
) -> Plan:
    kind = choice.get("cls") or reading.cls
    if kind == "junk":
        return Plan("junk")
    note = for_crm(note_text(letter, reading, binding, choice, lang), crm)
    t = LABELS["en" if lang == "en" else "ru"]
    request = crm_text(reading.request or letter.subject, 120)
    user = route.get("user") if not demo else None
    if kind == "reply":
        task = TaskIn(t["task_reply"].format(request=request), task_due(), responsible_id=user)
        return Plan("reply", deal_id=choice.get("deal"), note=note, task=task)
    v = values(reading)
    chosen = str(choice.get("company") or "none")
    known = {str(c["id"]) for c in binding.get("companies") or []}
    company_id = chosen if chosen in known else None
    company = None
    if chosen == "new" and v.get("company"):
        company = CompanyIn(
            crm_text(v["company"], 200), inn=v.get("inn"), phone=v.get("phone"), email=v.get("email")
        )
    contact = None
    allow_new = binding.get("allow_new", True)  # the demo of amoCRM and Kommo: no room for a new client now
    if chosen == "new" and not allow_new:
        raise ValueError("a new company where the demo makes none")
    if allow_new and not binding.get("contact") and (v.get("person") or v.get("email") or v.get("phone")):
        name = crm_text(v.get("person") or v.get("email") or v.get("phone") or "", 120)
        contact = ContactIn(name, email=v.get("email"), phone=v.get("phone"))
    deal = DealIn(
        deal_title(reading, letter, demo=demo),
        tags=("al-nuzum", "demo") if demo else ("al-nuzum",),
        stage_id=route.get("stage"),
        responsible_id=user,
    )
    task = TaskIn(t["task"].format(request=request), task_due(), responsible_id=user)
    return Plan("new", company_id, company, binding.get("contact"), contact, None, deal, note, task)


# --- the steps ---


def made_of(steps: list[str]) -> dict[str, str]:
    """``company:<id>``, ``contact:<id>``, ``deal:<id>``, ``note``, ``task`` → by name (an empty id: none
    made)."""
    out = {}
    for step in steps:
        name, _, value = step.partition(":")
        out[name] = value
    return out


# a step done: its name with what it made, and the kind of record it created (None when it created none)
Mark = Callable[[str, str | None], Awaitable[None]]


async def write(crm: CRM, plan: Plan, *, done: list[str], mark: Mark) -> str | None:
    """The steps not ``done`` yet, in order, each ``mark``-ed before the next begins: a retry after a failure
    goes on with the records the first attempt made and never makes a second deal. Returns the deal."""
    if plan.kind == "junk":
        return None
    made = made_of(done)
    if plan.kind == "new":
        if plan.deal is None:
            raise ValueError("a new request without its deal")
        if "company" not in made:
            if plan.company_id:
                made["company"] = plan.company_id
                await mark(f"company:{plan.company_id}", None)
            elif plan.company is not None:
                made["company"] = await crm.create_company(plan.company)
                await mark(f"company:{made['company']}", "company")
            else:
                made["company"] = ""
                await mark("company:", None)
        company_id = made["company"] or None
        if "contact" not in made:
            if plan.contact_id:
                if company_id:
                    await crm.link_contact(plan.contact_id, company_id)
                made["contact"] = plan.contact_id
                await mark(f"contact:{plan.contact_id}", None)
            elif plan.contact is not None:
                made["contact"] = await crm.create_contact(plan.contact, company_id=company_id)
                await mark(f"contact:{made['contact']}", "contact")
            else:
                made["contact"] = ""
                await mark("contact:", None)
        if "deal" not in made:
            deal = replace(plan.deal, company_id=company_id, contact_id=made["contact"] or None)
            made["deal"] = (await crm.create_deal(deal)).id
            await mark(f"deal:{made['deal']}", "deal")
        deal_id = made["deal"]
    else:
        if not plan.deal_id:
            raise ValueError("a reply without its deal")
        deal_id = plan.deal_id
    if "note" not in made:
        await crm.add_note(deal_id, plan.note)
        await mark("note", None)
    if plan.task is not None and "task" not in made:
        await crm.add_task(deal_id, plan.task)
        await mark("task", None)
    return deal_id


# --- the page ---


def pieces(text: str, spans: list[tuple[int, int, str]]) -> list[tuple[str, str | None]]:
    """``text`` cut into runs, each lit by the field whose fragment it is (overlaps: the first one wins)."""
    out: list[tuple[str, str | None]] = []
    at = 0
    for start, end, key in sorted(spans):
        if start < at or start >= end or end > len(text):
            continue
        if start > at:
            out.append((text[at:start], None))
        out.append((text[start:end], key))
        at = end
    if at < len(text):
        out.append((text[at:], None))
    return out


def lit(letter: Letter, reading: Reading) -> dict[str, list[tuple[str, str | None]]]:
    """Every source of the letter in runs, the fields' fragments marked."""
    spans: dict[str, list[tuple[int, int, str]]] = {}
    for key, f in reading.fields.items():
        if f.src:
            spans.setdefault(f.src, []).append((f.start, f.end, key))
    return {src: pieces(text, spans.get(src, [])) for src, text in letter.sources().items()}
