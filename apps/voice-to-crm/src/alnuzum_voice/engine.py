"""A note becomes a plan of changes of one deal; after a person confirms it, the plan is written.

What the plan may hold is fixed here: a stage from the CRM's own list, a sum above zero, a closing date,
catalog products (matched by solution 1), a task of a known kind with a date, and the note. There is no
delete, and no text of the speech or of the model reaches the CRM except the transcript quoted in the note.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from alnuzum_crm.base import CRM
from alnuzum_crm.errors import CrmValidationError
from alnuzum_crm.types import DealHit, DealLine, DealStatus, DealUpdate, Stage, TaskIn
from alnuzum_orders.catalog import Catalog, CatalogItem
from alnuzum_orders.match import decide, to_item_unit
from alnuzum_orders.model import Gate, ModelError, Question
from alnuzum_orders.text import unit_of
from alnuzum_voice.model import Intent, VoiceReader
from alnuzum_voice.text import NOTE_TEXT, clean, fold, variants

TZ = ZoneInfo("Asia/Tashkent")
DEMO_PREFIX = "Демо · "
MAX_CANDIDATES = 6
SURE = 0.6  # the model's confidence in a product it chose among candidates

TASKS = {
    "ru": {
        "call": "Позвонить клиенту",
        "meeting": "Встреча с клиентом",
        "offer": "Отправить коммерческое предложение",
        "invoice": "Выставить счёт",
        "payment": "Проверить оплату",
        "contract": "Подготовить договор",
        "follow_up": "Связаться с клиентом",
    },
    "en": {
        "call": "Call the client",
        "meeting": "Meeting with the client",
        "offer": "Send the commercial offer",
        "invoice": "Issue the invoice",
        "payment": "Check the payment",
        "contract": "Prepare the contract",
        "follow_up": "Get in touch with the client",
    },
}
NOTE_HEAD = {"ru": "Голосовая заметка менеджера", "en": "A manager's voice note"}
NOTE_DONE = {"ru": "Записано после подтверждения", "en": "Written after confirmation"}
NOTE_PARTS = {
    "ru": {
        "stage": "стадия",
        "amount": "сумма",
        "close_date": "дата закрытия",
        "product": "товаров",
        "task": "задача",
    },
    "en": {
        "stage": "stage",
        "amount": "amount",
        "close_date": "closing date",
        "product": "products",
        "task": "task",
    },
}


@dataclass
class Change:
    """One line of the plan; ``state`` "ok" is written, "unsure" waits for a person, "off" is never written
    (with ``why``)."""

    kind: str  # stage | amount | close_date | product | task | refused
    phrases: list[int]
    state: str = "ok"
    why: str | None = None
    stage_id: str | None = None
    stage_name: str | None = None
    value: str | None = None  # amount or date, as text
    product_id: str | None = None
    name: str | None = None
    sku: str | None = None
    quantity: str | None = None
    unit: str | None = None
    price: str | None = None
    said: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    task_kind: str | None = None
    due: str | None = None
    at: str | None = None
    what: str | None = None

    @classmethod
    def of(cls, data: dict[str, Any]) -> "Change":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Result:
    phrases: list[str]
    language: str | None
    queries: list[str]
    deal_phrases: list[int]
    candidates: list[dict[str, Any]]
    deal_id: str | None
    changes: list[Change]
    stages: list[dict[str, Any]]
    usage: dict[str, Any]
    source: str  # "voice" | "text"
    today: str
    deal_by: str = "auto"  # how the deal was picked: auto | person

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Result":
        raw = dict(data)
        raw["changes"] = [Change.of(c) for c in raw.get("changes", [])]
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

    @property
    def deal(self) -> dict[str, Any] | None:
        return next((c for c in self.candidates if c["id"] == self.deal_id), None)

    def writable(self) -> list[Change]:
        return [c for c in self.changes if c.state == "ok" and c.kind != "refused"]


def _hit(h: DealHit) -> dict[str, Any]:
    return {
        "id": h.id,
        "title": h.title,
        "company": h.company,
        "contact": h.contact,
        "stage_id": h.stage_id,
        "amount": str(h.amount) if h.amount is not None else None,
        "status": h.status.value,
        "by": h.by,
    }


def _stage(s: Stage) -> dict[str, Any]:
    return {"id": s.id, "name": s.name, "kind": s.kind.value, "pipeline": s.pipeline}


async def find_candidates(crm: CRM, queries: list[str], *, demo: bool) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for query in queries:
        for text in variants(query):
            for hit in await crm.find_deals(text, limit=MAX_CANDIDATES * 2):
                if demo and not hit.title.startswith(DEMO_PREFIX):
                    continue
                seen.setdefault(hit.id, _hit(hit))
    folded = [fold(q) for q in queries]

    def score(c: dict[str, Any]) -> tuple[int, int]:
        names = [fold(c.get(k) or "") for k in ("title", "company", "contact")]
        exact = sum(1 for q in folded if q and any(q in n for n in names))
        return (-exact, 0 if c["status"] == DealStatus.OPEN.value else 1)

    return sorted(seen.values(), key=score)[:MAX_CANDIDATES]


def pick(candidates: list[dict[str, Any]], queries: list[str]) -> str | None:
    """A deal the system may take alone: the only one found, or the only one whose names hold what was
    said."""
    if len(candidates) == 1:
        return str(candidates[0]["id"])
    folded = [fold(q) for q in queries if fold(q)]
    holding = [
        c
        for c in candidates
        if any(q in fold(c.get(k) or "") for q in folded for k in ("title", "company", "contact"))
    ]
    return str(holding[0]["id"]) if len(holding) == 1 else None


def _label(item: CatalogItem) -> str:
    return "; ".join(
        x for x in (item.name, f"article {item.sku}" if item.sku else "", f"sold by {item.unit}") if x
    )


def _product_change(ask: Any, item: CatalogItem | None, by_hand: list[CatalogItem]) -> Change:
    ch = Change("product", list(ask.phrases), said=ask.name, quantity=str(ask.quantity), unit=ask.unit)
    ch.candidates = [
        {
            "id": c.id,
            "name": c.name,
            "sku": c.sku,
            "unit": c.unit,
            "price": None if c.price is None else str(c.price),
            "packs": {k: str(v) for k, v in c.packs.items()},
            "said_unit": ask.unit,
        }
        for c in by_hand[:4]
    ]
    if item is None:
        ch.state, ch.why = ("unsure", "unsure") if by_hand else ("off", "not_in_catalog")
        return ch
    qty = to_item_unit(ask.quantity, unit_of(ask.unit) if ask.unit else None, item)
    if qty.value is None:
        ch.state, ch.why = "unsure", "unit"
        return ch
    if item.price is None:
        ch.state, ch.why = "off", "no_price"
    ch.product_id, ch.name, ch.sku = item.id, item.name, item.sku
    ch.quantity, ch.unit = str(qty.value), item.unit
    ch.price = str(item.price) if item.price is not None else None
    return ch


def choose_product(ch: Change, item: CatalogItem, said_unit: str | None) -> None:
    """A person (or the model) picked ``item`` for an unsure product line."""
    qty = to_item_unit(Decimal(str(ch.quantity or 0)), unit_of(said_unit) if said_unit else None, item)
    ch.product_id, ch.name, ch.sku, ch.unit = item.id, item.name, item.sku, item.unit
    ch.price = str(item.price) if item.price is not None else None
    if qty.value is None or item.price is None:
        ch.state, ch.why = "unsure", "unit" if qty.value is None else "no_price"
        return
    ch.quantity, ch.state, ch.why = str(qty.value), "ok", None


async def analyze(
    *,
    phrases: list[str],
    language: str | None,
    source: str,
    crm: CRM,
    catalog: Catalog,
    reader: VoiceReader,
    gate: Gate,
    demo: bool,
    today: date | None = None,
    usage: dict[str, Any] | None = None,
) -> Result:
    today = today or datetime.now(TZ).date()
    stages = await crm.stages()
    intent: Intent = await reader.intent(phrases, [(s.id, s.name) for s in stages], today, gate)
    spent = dict(usage or {})
    spent["calls"] = spent.get("calls", 0) + intent.usage.calls
    spent["usd"] = str(Decimal(str(spent.get("usd", "0"))) + intent.usage.usd)
    candidates = await find_candidates(crm, intent.deal_queries, demo=demo) if intent.deal_queries else []
    changes: list[Change] = []
    by_id = {s.id: s for s in stages}
    if intent.stage and intent.stage.stage_id in by_id:
        s = by_id[intent.stage.stage_id]
        changes.append(Change("stage", list(intent.stage.phrases), stage_id=s.id, stage_name=s.name))
    if intent.amount:
        changes.append(Change("amount", list(intent.amount.phrases), value=str(intent.amount.value)))
    if intent.close_date:
        ch = Change("close_date", list(intent.close_date.phrases), value=intent.close_date.value.isoformat())
        if "close_date" not in crm.capabilities:
            ch.state, ch.why = "off", "no_close_date"
        changes.append(ch)
    questions: list[tuple[Change, Any, list[CatalogItem]]] = []
    for ask in intent.products:
        verdict = decide(ask.name, None, ask.name, catalog)
        cands = [s.item for s in verdict.candidates]
        ch = _product_change(ask, verdict.item, cands)
        changes.append(ch)
        if verdict.item is None and cands:
            questions.append((ch, ask, cands))
    if questions:
        qs = [
            Question(
                i, f"{ask.name} ({ask.quantity} {ask.unit or ''})".strip(), tuple(_label(c) for c in cands)
            )
            for i, (_, ask, cands) in enumerate(questions)
        ]
        try:
            answers, used = await reader.choose(qs, gate)
            spent["calls"] += used.calls
            spent["usd"] = str(Decimal(spent["usd"]) + used.usd)
            for ans in answers:
                ch, ask, cands = questions[ans.key]
                if ans.choice is not None and ans.confidence >= SURE:
                    choose_product(ch, cands[ans.choice], ask.unit)
        except ModelError:
            pass  # the lines stay for a person to pick
    if intent.task:
        t = intent.task
        changes.append(
            Change(
                "task",
                list(t.phrases),
                task_kind=t.kind,
                due=t.due.isoformat(),
                at=t.at.strftime("%H:%M") if t.at else None,
            )
        )
    for r in intent.refused:
        changes.append(Change("refused", list(r.phrases), state="off", what=r.what))
    deal_id = pick(candidates, intent.deal_queries)
    return Result(
        phrases=phrases,
        language=language,
        queries=intent.deal_queries,
        deal_phrases=list(intent.deal_phrases),
        candidates=candidates,
        deal_id=deal_id,
        changes=changes,
        stages=[_stage(s) for s in stages],
        usage=spent,
        source=source,
        today=today.isoformat(),
    )


def check_stage(result: Result) -> None:
    """A stage move stays within the pipeline of the chosen deal; another pipeline's stage is dropped."""
    deal = result.deal
    stages = {s["id"]: s for s in result.stages}
    current = stages.get(deal.get("stage_id")) if deal else None
    for ch in result.changes:
        if ch.kind == "stage" and ch.state == "ok":
            target = stages.get(ch.stage_id or "")
            if target is None or (current and target.get("pipeline") != current.get("pipeline")):
                ch.state, ch.why = "off", "other_pipeline"


def note_text(result: Result, lang: str, now: datetime | None = None) -> str:
    """Our template: a head, the transcript quoted (cleaned, at most NOTE_TEXT characters), what was
    written."""
    now = (now or datetime.now(UTC)).astimezone(TZ)
    words = NOTE_PARTS[lang]
    done = [c for c in result.writable()]
    kinds = [k for k in ("stage", "amount", "close_date", "task") if any(c.kind == k for c in done)]
    products = sum(1 for c in done if c.kind == "product")
    parts = [words[k] for k in kinds] + ([f"{words['product']}: {products}"] if products else [])
    quote = clean(" ".join(result.phrases), NOTE_TEXT)
    lines = [f"{NOTE_HEAD[lang]} · {now:%Y-%m-%d %H:%M}", f"«{quote}»"]
    if parts:
        lines.append(f"{NOTE_DONE[lang]}: {', '.join(parts)}. Al-Nuzum")
    return "\n".join(lines)


def task_due(ch: Change) -> datetime:
    day = date.fromisoformat(str(ch.due))
    at = time.fromisoformat(ch.at) if ch.at else time(18, 0)
    return datetime.combine(day, at, TZ).astimezone(UTC)


@dataclass
class Written:
    deal_id: str
    readback: dict[str, Any]


async def write(crm: CRM, result: Result, *, lang: str, now: datetime | None = None) -> Written:
    """Products, then the deal's fields (a sum after the products: some CRMs count it from the lines), the
    task, the note; then the deal is read back."""
    if not result.deal_id:
        raise CrmValidationError("no deal chosen", crm=crm.kind.value)
    check_stage(result)
    ok = result.writable()
    lines = tuple(
        DealLine(
            str(c.name), Decimal(str(c.quantity)), Decimal(str(c.price)), product_id=c.product_id, sku=c.sku
        )
        for c in ok
        if c.kind == "product"
    )
    if lines:
        await crm.add_lines(result.deal_id, lines)
    update = DealUpdate(
        stage_id=next((c.stage_id for c in ok if c.kind == "stage"), None),
        amount=next((Decimal(str(c.value)) for c in ok if c.kind == "amount"), None),
        close_date=next((date.fromisoformat(str(c.value)) for c in ok if c.kind == "close_date"), None),
    )
    if not update.empty:
        await crm.update_deal(result.deal_id, update)
    for c in ok:
        if c.kind == "task":
            await crm.add_task(
                result.deal_id, TaskIn(f"{TASKS[lang][str(c.task_kind)]} · Al-Nuzum", task_due(c))
            )
    await crm.add_note(result.deal_id, note_text(result, lang, now))
    deal = await crm.get_deal(result.deal_id)
    names = {s["id"]: s["name"] for s in result.stages}
    return Written(
        deal.id,
        {
            "id": deal.id,
            "title": deal.title,
            "stage": names.get(deal.stage_id or "", deal.stage_id),
            "amount": str(deal.amount) if deal.amount is not None else None,
            "currency": deal.currency,
            "lines": [
                {"name": x.name, "quantity": str(x.quantity), "price": str(x.unit_price)} for x in deal.lines
            ][:30],
        },
    )
