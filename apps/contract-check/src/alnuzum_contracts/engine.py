"""A comparison becomes what a person sees and what a CRM gets.

What a deal may get is fixed here: one note made of our words (the topic and level of each change, the clause
number) and quotes of the two drafts, cleaned and bounded; one task for the lawyer of our wording with a date.
There is nothing else: no text of the contract or of the model decides what is written, where, or whether.
"""

import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from alnuzum_contracts.clauses import Change, Clause, Comparison
from alnuzum_crm.base import CRM
from alnuzum_crm.types import TaskIn
from alnuzum_platform.web.i18n import fill, plural

TZ = ZoneInfo("Asia/Tashkent")
DEMO_PREFIX = "Демо · "
NOTE_MAX = 3500
NOTE_ITEMS = 15
QUOTE_MAX = 160
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069]")
# How a CRM shows a note, read back live on 09.11 (API and the page): Zoho and Bitrix24 render it as HTML
# («<b>x</b>» bold, «&lt;b&gt;» shown as «<b>»), so the note goes to them HTML-escaped, «&» first, and shows
# what the contract says letter for letter; Bitrix24's adapter also turns [ ] into fullwidth brackets, so
# BBCode cannot form. amoCRM, Kommo and Odoo escape a note themselves: escaped again it would show «&lt;».
HTML_NOTES = frozenset({"zoho", "bitrix24"})

TOPICS = {
    "ru": {
        "liability": "Ответственность и неустойки",
        "payment": "Сроки и порядок оплаты",
        "jurisdiction": "Подсудность",
        "termination": "Расторжение",
        "acceptance": "Приёмка",
        "warranty": "Гарантии",
        "confidentiality": "Конфиденциальность",
        "rights": "Права",
        "delivery": "Поставка",
        "requisites": "Реквизиты",
        "wording": "Редакция",
        "other": "Другое",
    },
    "en": {
        "liability": "Liability and penalties",
        "payment": "Payment terms",
        "jurisdiction": "Jurisdiction",
        "termination": "Termination",
        "acceptance": "Acceptance",
        "warranty": "Warranties",
        "confidentiality": "Confidentiality",
        "rights": "Rights",
        "delivery": "Delivery",
        "requisites": "Requisites",
        "wording": "Wording",
        "other": "Other",
    },
}
RISKS = {
    "ru": {"high": "высокий риск", "medium": "средний риск", "low": "низкий риск"},
    "en": {"high": "high risk", "medium": "medium risk", "low": "low risk"},
}
KINDS = {
    "ru": {"changed": "изменён", "added": "добавлен", "deleted": "удалён", "moved": "перенесён"},
    "en": {"changed": "changed", "added": "added", "deleted": "deleted", "moved": "moved"},
}
WORDS = {
    "ru": {
        "head": "Сверка договора с типовым",
        "count": "Изменений: {n} (высокий риск: {high}, средний: {medium}, низкий: {low})",
        "same": "Редакция контрагента совпадает с типовым по смыслу",
        "cosmetic": "правок оформления: {n}",
        "clause": "п. {name}",
        "new": "новый пункт",
        "was": "Было",
        "now": "Стало",
        "more": "…и ещё изменений: {n}. Полный разбор — на странице решения 24 часа.",
        "unrated": "не оценено",
    },
    "en": {
        "head": "The contract against the standard",
        "count": "Changes: {n} (high risk: {high}, medium: {medium}, low: {low})",
        "same": "The counterparty's draft means the same as the standard",
        "cosmetic": "formatting edits: {n}",
        "clause": "cl. {name}",
        "new": "a new clause",
        "was": "Was",
        "now": "Now",
        "more": (
            "…and {n:more change|more changes}. The whole comparison is on the solution's page for 24 hours."
        ),
        "unrated": "not rated",
    },
}


def crm_text(value: str, limit: int) -> str:
    """A quote of the contract for a CRM: one line, no control or direction characters, bounded. Markup is
    neutralised for the whole note by ``for_crm``."""
    text = " ".join(_CONTROL.sub(" ", value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def for_crm(note: str, crm: str) -> str:
    """The note as the CRM must get it to show it letter for letter."""
    return html.escape(note, quote=False) if crm in HTML_NOTES else note


# --- the stored result ---


def to_result(
    cmp: Comparison, *, kind_ours: str, kind_theirs: str, side: str, standard: str, usage: dict[str, Any]
) -> dict[str, Any]:
    return {
        "ours": [[c.label, c.key, c.text] for c in cmp.ours],
        "theirs": [[c.label, c.key, c.text] for c in cmp.theirs],
        "changes": [c.to_json() for c in cmp.changes],
        "cosmetic": cmp.cosmetic,
        "same": cmp.same,
        "kind_ours": kind_ours,
        "kind_theirs": kind_theirs,
        "side": side,
        "standard": standard,
        "usage": usage,
    }


def comparison(result: dict[str, Any]) -> Comparison:
    return Comparison(
        [Clause(*c) for c in result["ours"]],
        [Clause(*c) for c in result["theirs"]],
        [Change.of(c) for c in result["changes"]],
        int(result.get("cosmetic") or 0),
        int(result.get("same") or 0),
    )


def summary(cmp: Comparison) -> dict[str, Any]:
    """Counts only: what may sit beside the sealed result in the clear."""
    real = [c for c in cmp.changes if c.kind != "moved"]
    return {
        "changes": len(cmp.changes),
        "high": sum(1 for c in real if c.risk == "high"),
        "medium": sum(1 for c in real if c.risk == "medium"),
        "low": sum(1 for c in cmp.changes if c.risk == "low"),
        "unrated": sum(1 for c in real if not c.rated),
        "cosmetic": cmp.cosmetic,
        "substantive": sum(1 for c in real if c.substantive),
    }


def ordered(cmp: Comparison) -> list[Change]:
    return sorted(cmp.changes, key=lambda c: (RISK_ORDER.get(c.risk, 1), c.id))


def clause_name(cmp: Comparison, c: Change) -> str:
    clause = cmp.theirs[c.b] if c.b is not None else cmp.ours[c.a] if c.a is not None else None
    return clause.name if clause else ""


def quotes(cmp: Comparison, c: Change) -> tuple[str | None, str | None]:
    """The two quotes of a change: slices of the clauses' own texts."""
    was = cmp.ours[c.a].text[c.qa[0] : c.qa[1]] if c.a is not None and c.qa else None
    now = cmp.theirs[c.b].text[c.qb[0] : c.qb[1]] if c.b is not None and c.qb else None
    if c.kind == "moved":
        was = None
    return was, now


# --- the note and the task ---


def note_text(cmp: Comparison, lang: str, now: datetime | None = None) -> str:
    """Our template: a head with the counts, then the changes by risk, each with its clause, topic, level and
    the two quotes, until NOTE_MAX characters."""
    w, topics, risks, kinds = WORDS[lang], TOPICS[lang], RISKS[lang], KINDS[lang]
    now = (now or datetime.now(UTC)).astimezone(TZ)
    s = summary(cmp)
    lines = [f"{w['head']} · {now:%Y-%m-%d %H:%M} · Al-Nuzum"]
    if s["substantive"]:
        lines.append(w["count"].format(n=s["changes"], high=s["high"], medium=s["medium"], low=s["low"]))
    else:
        lines.append(w["same"])
    if cmp.cosmetic:
        lines.append(w["cosmetic"].format(n=cmp.cosmetic))
    size = sum(len(x) + 1 for x in lines)
    listed = 0
    items = ordered(cmp)
    for n, c in enumerate(items, 1):
        name = clause_name(cmp, c)
        where = w["clause"].format(name=crm_text(name, 20)) if name else w["new"]
        level = risks.get(c.risk, "") if c.rated else w["unrated"]
        block = [f"{n}. {where} · {topics.get(c.topic, '')} · {level} · {kinds.get(c.kind, '')}"]
        was, now_q = quotes(cmp, c)
        if was:
            block.append(f"{w['was']}: «{crm_text(was, QUOTE_MAX)}»")
        if now_q:
            block.append(f"{w['now']}: «{crm_text(now_q, QUOTE_MAX)}»")
        cost = sum(len(x) + 1 for x in block) + 1
        if listed >= NOTE_ITEMS or size + cost > NOTE_MAX - 120:
            break
        lines += ["", *block]
        size += cost
        listed += 1
    if listed < len(items):
        lines += ["", fill(w["more"], n=len(items) - listed)]
    return "\n".join(lines)[:NOTE_MAX]


def task_text(cmp: Comparison, lang: str) -> str:
    high = summary(cmp)["high"]
    if lang == "ru":
        if high:
            word = plural(high, ("пункт", "пункта", "пунктов"))
            return f"Юристу: {high} {word} высокого риска в редакции договора · Al-Nuzum"
        return "Юристу: проверить редакцию договора контрагента · Al-Nuzum"
    if high:
        clauses = plural(high, ("clause", "clauses"))
        return f"Lawyer: {high} high-risk {clauses} in the counterparty's draft · Al-Nuzum"
    return "Lawyer: review the counterparty's draft of the contract · Al-Nuzum"


def task_due(now: datetime | None = None) -> datetime:
    """The next working day, 18:00 in Tashkent."""
    day: date = (now or datetime.now(UTC)).astimezone(TZ).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(18, 0), TZ).astimezone(UTC)


@dataclass(frozen=True)
class Step:
    """One write into the CRM, named so a retry after a failure can skip it once it is done."""

    name: str
    method: str  # add_note | add_task
    arg: Any


def steps(cmp: Comparison, *, lang: str, crm: str, now: datetime | None = None) -> list[Step]:
    """The note always; the task when something in the draft changes obligations."""
    out = [Step("note", "add_note", for_crm(note_text(cmp, lang, now), crm))]
    if summary(cmp)["substantive"]:
        out.append(Step("task", "add_task", TaskIn(task_text(cmp, lang), task_due(now))))
    return out


async def _nothing(_name: str) -> None:
    return None


async def write(
    crm: CRM,
    deal_id: str,
    cmp: Comparison,
    *,
    lang: str,
    kind: str,
    now: datetime | None = None,
    done: frozenset[str] = frozenset(),
    mark: Callable[[str], Awaitable[None]] = _nothing,
) -> list[str]:
    """The steps not ``done`` yet, in order, each ``mark``-ed as done before the next begins. ``kind``: the
    CRM, for how its note is escaped."""
    written = []
    for step in steps(cmp, lang=lang, crm=kind, now=now):
        if step.name in done:
            continue
        await getattr(crm, step.method)(deal_id, step.arg)
        await mark(step.name)
        written.append(step.name)
    return written


# --- the page ---


@dataclass
class Piece:
    text: str
    change: int | None = None
    risk: str | None = None


@dataclass
class Row:
    """A clause of their draft as the page shows it, or a clause of ours they deleted, in its place."""

    label: str
    pieces: list[Piece]
    changes: list[int]
    risk: str | None
    deleted: bool = False


def _pieces(text: str, spans: list[tuple[int, int]], change: Change | None) -> list[Piece]:
    out, at = [], 0
    for s, e in sorted(spans):
        if s < at or e <= s:
            continue
        if s > at:
            out.append(Piece(text[at:s]))
        out.append(Piece(text[s:e], change.id if change else None, change.risk if change else None))
        at = e
    if at < len(text):
        out.append(Piece(text[at:]))
    return out


def rows(cmp: Comparison) -> list[Row]:
    by_b: dict[int, Change] = {c.b: c for c in cmp.changes if c.b is not None}
    deleted: dict[float, list[Change]] = {}
    order = {c.id: n for n, c in enumerate(cmp.changes)}
    for c in cmp.changes:
        if c.kind == "deleted":
            nxt = next((x.b for x in cmp.changes[order[c.id] :] if x.b is not None), len(cmp.theirs))
            deleted.setdefault(nxt, []).append(c)
    out: list[Row] = []
    for j, clause in enumerate([*cmp.theirs, None]):
        for c in deleted.get(j, []):
            text = cmp.ours[c.a].text if c.a is not None else ""
            out.append(
                Row(
                    cmp.ours[c.a].label if c.a is not None else "",
                    [Piece(text, c.id, c.risk)],
                    [c.id],
                    c.risk,
                    True,
                )
            )
        if clause is None:
            break
        hit = by_b.get(j)
        if hit is None:
            out.append(Row(clause.label, [Piece(clause.text)], [], None))
        elif hit.kind == "changed":
            out.append(Row(clause.label, _pieces(clause.text, hit.b_spans, hit), [hit.id], hit.risk))
        else:  # added, moved: the whole clause
            out.append(Row(clause.label, [Piece(clause.text, hit.id, hit.risk)], [hit.id], hit.risk))
    return out
