"""Document -> priced order lines -> what goes into the CRM.

The result of one run is plain JSON (stored for a day, never the file itself). What a deal may contain
is decided here, from that JSON only: catalog products that were among a line's candidates, quantities
that passed the checks, prices from the CRM catalog and the company's rules.
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from alnuzum_crm.types import DealLine
from alnuzum_orders.catalog import Catalog, CatalogItem
from alnuzum_orders.intake import Source
from alnuzum_orders.match import Scored, analogues, decide, to_item_unit
from alnuzum_orders.model import Customer, Extraction, Gate, Item, ModelError, Question, Reader, Usage
from alnuzum_orders.pricing import Priced, Rules, price
from alnuzum_orders.text import norm, plain, unit_of

ASK_ABOVE = 0.8  # a model answer below this goes to a person
MAX_QTY = Decimal(1_000_000)


@dataclass
class Cand:
    id: str
    name: str
    sku: str | None
    unit: str
    price: str | None
    stock: str | None
    category: str | None
    score: float

    @classmethod
    def of(cls, s: Scored | CatalogItem, score: float | None = None) -> "Cand":
        item = s.item if isinstance(s, Scored) else s
        sc = s.score if isinstance(s, Scored) else (score or 1.0)
        return cls(
            item.id,
            item.name,
            item.sku,
            item.unit,
            None if item.price is None else str(item.price),
            None if item.stock is None else str(item.stock),
            item.category,
            round(sc, 3),
        )


@dataclass
class Line:
    key: int
    src: list[int]
    text: str
    name: str
    qty: str | None
    unit: str | None
    sku: str | None
    status: str = "disputed"  # ok | disputed | not_found
    by: str = ""  # article | name | model | person
    confidence: float = 0.0
    candidates: list[Cand] = field(default_factory=list)
    choice: str | None = None
    qty_item: str | None = None
    rounded: bool = False
    short: bool = False
    problem: str | None = None  # qty | unit | stock
    analogs: list[Cand] = field(default_factory=list)

    def options(self) -> dict[str, Cand]:
        return {c.id: c for c in [*self.candidates, *self.analogs]}

    @property
    def chosen(self) -> Cand | None:
        return self.options().get(self.choice or "")


@dataclass
class Result:
    source: dict[str, Any]
    customer: dict[str, str | None]
    lines: list[Line]
    catalog: dict[str, Any]
    usage: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Result":
        lines = []
        for raw in data["lines"]:
            raw = dict(raw)
            raw["candidates"] = [Cand(**c) for c in raw.get("candidates", [])]
            raw["analogs"] = [Cand(**c) for c in raw.get("analogs", [])]
            lines.append(Line(**raw))
        return cls(data["source"], data["customer"], lines, data["catalog"], data["usage"])

    @property
    def disputed(self) -> list[Line]:
        return [ln for ln in self.lines if ln.status != "ok"]


# --- extraction ---

_NUMBER = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*([^\d\s].*)?$")


def _num(value: str) -> tuple[Decimal | None, str | None]:
    m = _NUMBER.match(value.replace(" ", " ").replace(" ", "") if value.strip()[:1].isdigit() else value)
    if not m:
        return None, None
    try:
        return Decimal(m.group(1).replace(",", ".")), (m.group(2) or None)
    except InvalidOperation:
        return None, None


def from_table(source: Source) -> Extraction:
    """A spreadsheet with a recognised header needs no model."""
    cols = source.columns or {}
    first = len(source.lines) - len(source.rows)
    items: list[Item] = []
    for j, row in enumerate(source.rows):

        def cell(role: str, row: list[str] = row) -> str:
            i = cols.get(role)
            return row[i].strip() if i is not None and i < len(row) else ""

        name = cell("name")
        if not name or len(name) < 2:
            continue
        qty, tail = _num(cell("qty"))
        unit = cell("unit") or (tail.strip() if tail else "") or None
        items.append(
            Item(
                (first + j + 1,),
                " | ".join(c for c in row if c)[:300],
                name[:200],
                qty,
                unit,
                cell("sku") or None,
            )
        )
    return Extraction(items, Customer())


# --- the run ---


def _question(line: Line) -> Question:
    def label(c: Cand) -> str:
        parts = [c.name]
        if c.sku:
            parts.append(f"article {c.sku}")
        parts.append(f"sold by {c.unit}")
        return "; ".join(parts)

    return Question(
        line.key, f"{line.text} (as named: {line.name})", tuple(label(c) for c in line.candidates)
    )


def _check_quantity(line: Line, item: CatalogItem, catalog: Catalog) -> None:
    qty = Decimal(line.qty) if line.qty is not None else None
    if qty is None or qty <= 0 or qty > MAX_QTY:
        line.problem = "qty"
        line.status = "disputed"
        return
    q = to_item_unit(qty, unit_of(line.unit), item)
    if q.value is None or q.value <= 0:
        line.problem = "unit"
        line.status = "disputed"
        return
    line.qty_item, line.rounded = str(q.value), q.rounded
    if item.stock is not None and q.value > item.stock:
        line.short, line.problem = True, "stock"
        line.analogs = [Cand.of(s) for s in analogues(line.name, catalog, chosen=item, need=q.value)]


async def run(source: Source, catalog: Catalog, reader: Reader, gate: Gate) -> Result:
    usage = Usage()
    extraction = from_table(source) if source.columns else None
    if extraction is None or not extraction.items:
        extraction = await reader.extract(source, gate)
        usage.add(extraction.usage)
    shown = extraction.transcript or source.lines
    lines: list[Line] = []
    unsure: list[Line] = []
    for key, it in enumerate(extraction.items, 1):
        line = Line(
            key,
            list(it.lines),
            it.text,
            it.name,
            None if it.quantity is None else str(it.quantity),
            it.unit,
            it.sku,
        )
        verdict = decide(it.name, it.sku, it.text, catalog)
        line.candidates = [Cand.of(s) for s in verdict.candidates]
        if verdict.item is not None:
            line.choice, line.status, line.by, line.confidence = (
                verdict.item.id,
                "ok",
                verdict.by,
                verdict.confidence,
            )
        elif verdict.by == "none":
            line.status = "not_found"
            line.analogs = [Cand.of(s) for s in analogues(it.name, catalog)]
            line.candidates = []
        else:
            unsure.append(line)
        lines.append(line)
    if unsure:
        try:
            answers, used = await reader.choose([_question(ln) for ln in unsure], gate)
            usage.add(used)
        except ModelError:
            answers = []
        for line, answer in zip(unsure, answers, strict=False):
            line.confidence = answer.confidence
            if answer.choice is not None:
                line.choice = line.candidates[answer.choice].id
                line.by = "model"
                line.status = "ok" if answer.confidence >= ASK_ABOVE else "disputed"
            elif answer.confidence >= ASK_ABOVE:
                line.status = "not_found"
                line.analogs = [Cand.of(s) for s in analogues(line.name, catalog)]
    for line in lines:
        item = catalog.by_id(line.choice) if line.choice else None
        if item is not None:
            _check_quantity(line, item, catalog)
    return Result(
        source={"kind": source.kind, "name": source.name[:120], "lines": shown, "pages": source.pages},
        customer=asdict(extraction.customer),
        lines=lines,
        catalog={
            "currency": catalog.currency,
            "count": len(catalog.items),
            "has_stock": catalog.has_stock,
            "warning": catalog.warning,
        },
        usage={
            "calls": usage.calls,
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "usd": str(usage.usd),
        },
    )


# --- a person's choices ---


def apply_choices(
    result: Result, form: dict[str, str], catalog_items: dict[str, CatalogItem] | None = None
) -> None:
    """``c<key>`` = a candidate's or an analogue's id, or "skip"; ``q<key>`` = a quantity in the item's unit.
    Anything that is not one of the line's own options is ignored."""
    for line in result.lines:
        pick = form.get(f"c{line.key}")
        if pick == "skip":
            line.choice, line.status, line.by, line.problem = None, "not_found", "person", None
            continue
        if pick and pick in line.options():
            line.choice, line.by, line.confidence = pick, "person", 1.0
            line.status = "ok"
            if line.problem == "stock":
                line.problem, line.short = None, False
        typed = form.get(f"q{line.key}", "").strip().replace(",", ".")
        if typed:
            try:
                qty = Decimal(typed)
            except InvalidOperation:
                continue
            if not qty.is_finite() or not Decimal(0) < qty <= MAX_QTY:
                continue
            qty = qty.quantize(Decimal("0.001"))
            if qty <= 0:
                # nothing left after the three decimals: no quantity, the line waits for a person
                line.qty_item, line.problem = None, "qty"
                if line.status == "ok":
                    line.status = "disputed"
                continue
            line.qty_item = str(plain(qty))
            if line.problem in ("qty", "unit"):
                line.problem = None
                line.status = "ok" if line.choice else line.status
    for line in result.lines:
        if line.status == "ok" and line.choice and line.qty_item is None:
            line.status = "disputed"


# --- prices and the deal ---


def priced(result: Result, rules: Rules) -> Priced:
    rows = []
    for line in result.lines:
        c = line.chosen
        if line.status == "ok" and c is not None and line.qty_item is not None:
            base, qty = Decimal(c.price or 0), Decimal(line.qty_item)
            if base > 0 and qty > 0:  # a product without a price is never sold at 0
                rows.append((line.key, base, c.category, qty))
    return price(rows, rules)


@dataclass(frozen=True)
class Plan:
    title: str
    lines: tuple[DealLine, ...]
    total: Decimal | None
    note: str
    task: str | None
    left: int


_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$")
_PHONE = re.compile(r"^\+?[0-9 ()-]{7,20}$")
# a company is letters, digits and the marks of legal names; no addresses, links or markup
_COMPANY = re.compile(r"^[\w \"'«»“”„().,&№+-]{2,120}$")
# a person: at most five words of letters, with dots, hyphens and apostrophes
_PERSON = re.compile(r"^[^\W\d_][^\W\d_.'’-]*(?:[ .'’-]+[^\W\d_][^\W\d_.'’-]*){0,4}\.?$")


def clean_customer(raw: dict[str, str | None]) -> Customer:
    """Requisites as they may reach a CRM: each checked by its format (no control characters, bounded),
    or dropped. These are the only words of a document that reach a CRM, and only as the client's
    company and contact, never in the deal, the note or the task."""

    def field(v: str | None, pattern: re.Pattern[str], n: int) -> str | None:
        v = v or ""
        if any(not ch.isprintable() and ch not in " \t\n" for ch in v):
            return None
        v = " ".join(v.split())
        if not v or len(v) > n or not pattern.match(v) or not any(ch.isalpha() for ch in v):
            return None
        return v

    inn = re.sub(r"\D", "", raw.get("inn") or "")
    phone = " ".join((raw.get("phone") or "").split())
    phone_digits = re.sub(r"\D", "", phone)
    email = (raw.get("email") or "").strip()
    return Customer(
        company=field(raw.get("company"), _COMPANY, 120),
        inn=inn if 9 <= len(inn) <= 14 else None,
        person=field(raw.get("person"), _PERSON, 80),
        phone=phone if _PHONE.match(phone) and 7 <= len(phone_digits) <= 15 else None,
        email=email if _EMAIL.match(email) else None,
    )


def _fmt(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"))
    s = f"{q:,.2f}".replace(",", " ")
    return s[:-3] if s.endswith(".00") else s


def plan(result: Result, rules: Rules, *, line_prices: bool, lang: str, now: datetime | None = None) -> Plan:
    ru = lang == "ru"
    p = priced(result, rules)
    by_key = {ln.key: ln for ln in p.lines}
    currency = result.catalog.get("currency") or ""
    merged: dict[str, tuple[Cand, Decimal, Decimal]] = {}
    note = []
    when = now or datetime.now(UTC)
    stamp, day = when.strftime("%Y-%m-%d %H:%M UTC"), when.strftime("%Y-%m-%d")
    # every text below is ours: catalog names and articles, numbers, prices; nothing of the document
    # (its lines, its file name) and nothing the model wrote
    note.append(f"Al-Nuzum · {'заявка' if ru else 'order'} · {stamp}")
    unpriced: list[Line] = []
    for line in result.lines:
        pl = by_key.get(line.key)
        c = line.chosen
        if c is None or line.status != "ok":
            continue
        unit_price = pl.unit_price if pl and line_prices else (pl.base if pl else Decimal(0))
        if pl is None or pl.quantity <= 0 or unit_price <= 0:
            unpriced.append(line)
            continue
        if c.id in merged:
            prev = merged[c.id]
            merged[c.id] = (c, prev[1] + pl.quantity, unit_price)
        else:
            merged[c.id] = (c, pl.quantity, unit_price)
        rule = f"{pl.percent:+} %" if pl.percent else ("без наценки" if ru else "no markup")
        note.append(
            f"{line.key}. {c.name} ({c.sku or '—'}) — {_fmt(pl.quantity)} {c.unit} × {_fmt(pl.unit_price)}"
            f" = {_fmt(pl.total)} [{'база' if ru else 'base'} {_fmt(pl.base)}, {rule}]"
        )
    note.append(
        f"Сумма по каталогу: {_fmt(p.base_total)}; по правилам категорий: {_fmt(p.subtotal)}"
        if ru
        else f"At catalog prices: {_fmt(p.base_total)}; after category rules: {_fmt(p.subtotal)}"
    )
    if p.tier:
        note.append(
            f"Скидка от суммы {_fmt(p.tier.percent)} % (от {_fmt(p.tier.start)})"
            if ru
            else f"Volume discount {_fmt(p.tier.percent)} % (from {_fmt(p.tier.start)})"
        )
    note.append(f"{'Итого' if ru else 'Total'}: {_fmt(p.total)} {currency}")
    if not line_prices and p.total != p.base_total:
        note.append(
            "Строки сделки по цене каталога, бюджет сделки — итог по правилам."
            if ru
            else "Deal lines at catalog prices; the deal budget is the total after the rules."
        )
    left = result.disputed + unpriced
    if left:
        note.append("Не добавлено, проверить:" if ru else "Not added, to check:")
        for line in left:
            why = {
                "not_found": "нет в каталоге" if ru else "not in the catalog",
                "ok": "нет цены в каталоге" if ru else "no price in the catalog",
            }.get(line.status, "спорная" if ru else "unsure")
            where = ", ".join(str(int(n)) for n in line.src[:5])
            at = (f" (строки документа {where})" if ru else f" (document lines {where})") if where else ""
            note.append(f"- {line.key}.{at} — {why}")
    title = f"Заявка · {len(merged)} поз. · {day}" if ru else f"Order · {len(merged)} line(s) · {day}"
    task = None
    if left:
        task = (
            f"Проверить {len(left)} поз. заявки от {day}"
            if ru
            else f"Check {len(left)} line(s) of the order of {day}"
        )
    deal_lines = tuple(
        DealLine(c.name, qty, unit_price, product_id=c.id, sku=c.sku)
        for c, qty, unit_price in merged.values()
    )
    return Plan(
        title[:200], deal_lines, None if line_prices else p.total, "\n".join(note)[:9000], task, len(left)
    )


def norm_name(value: str) -> str:
    return norm(value)
