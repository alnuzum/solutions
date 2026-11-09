"""Order line -> catalog item, deterministically first.

1. An article in the line (its own column or anywhere in the text) that the catalog has -> certain.
2. The whole name equal to a product name or a synonym -> certain.
3. Otherwise a score over comparable tokens (Dice, sizes and codes weigh more than words, a size the
   customer named that the product does not have costs a lot). A clear leader is taken; a close race or
   a weak leader goes to the model with the candidates, and whatever it is not sure about goes to a person.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from alnuzum_orders.catalog import Catalog, CatalogItem
from alnuzum_orders.text import is_spec, norm, plain, sku_key, tokens

SURE = 0.9
MARGIN = 0.15
DOMINANT = 0.7
WEAK = 0.3
CANDIDATES = 8
SPEC_WEIGHT = 2.5
# whole-unit sales: a pack, a box or a piece is not sold in parts
WHOLE = frozenset({"шт", "уп", "кор", "компл", "бухта"})


@dataclass(frozen=True, slots=True)
class Scored:
    item: CatalogItem
    score: float


def _weight(token: str) -> float:
    return SPEC_WEIGHT if is_spec(token) else 1.0


def similarity(query: list[str], item: CatalogItem) -> float:
    if not query:
        return 0.0
    q = set(query)
    best = 0.0
    for phrase in item.phrases:
        p = set(tokens(phrase))
        if not p:
            continue
        common = sum(_weight(t) for t in q & p)
        total = sum(_weight(t) for t in q) + sum(_weight(t) for t in p)
        best = max(best, 2 * common / total)
    # how much of what the customer wrote this product has under any of its names
    have = item.all_tokens
    coverage = sum(_weight(t) for t in q if t in have) / sum(_weight(t) for t in q)
    best = 0.6 * best + 0.4 * coverage
    # a size or a rating the customer named that this product has under none of its names
    missing = [t for t in q if is_spec(t) and t not in item.all_tokens]
    if missing:
        best *= 0.55 ** min(len(missing), 3)
    return round(best, 4)


def rank(text: str, catalog: Catalog, *, limit: int = CANDIDATES) -> list[Scored]:
    query = tokens(text)
    scored = [Scored(item, similarity(query, item)) for item in catalog.items]
    scored.sort(key=lambda s: (-s.score, s.item.name))
    return [s for s in scored[:limit] if s.score > 0]


def by_article(line_sku: str | None, text: str, catalog: Catalog) -> CatalogItem | None:
    skus = catalog.by_sku()
    if line_sku and (hit := skus.get(sku_key(line_sku))):
        return hit
    for raw in norm(text).split():
        if len(raw) >= 4 and (hit := skus.get(sku_key(raw))):
            return hit
    return None


def by_name(text: str, catalog: Catalog) -> CatalogItem | None:
    want = norm(text)
    hits = [i for i in catalog.items if any(norm(p) == want for p in i.phrases)]
    return hits[0] if len(hits) == 1 else None


@dataclass(frozen=True, slots=True)
class Verdict:
    """``item`` when the deterministic part is sure; otherwise the candidates for the model or a person."""

    item: CatalogItem | None
    by: str
    confidence: float
    candidates: tuple[Scored, ...]


def decide(name: str, sku: str | None, raw: str, catalog: Catalog) -> Verdict:
    if hit := by_article(sku, raw, catalog):
        return Verdict(hit, "article", 1.0, (Scored(hit, 1.0),))
    if hit := by_name(name, catalog):
        return Verdict(hit, "name", 0.99, (Scored(hit, 0.99),))
    ranked = rank(name or raw, catalog)
    if not ranked or ranked[0].score < WEAK:
        return Verdict(None, "none", 0.0, tuple(ranked[:3]))
    top = ranked[0].score
    second = ranked[1].score if len(ranked) > 1 else 0.0
    # a clear leader, or a fair one with nothing near it
    if (top >= SURE and top - second >= MARGIN) or (top >= DOMINANT and second < top * 0.35):
        return Verdict(ranked[0].item, "name", top, tuple(ranked))
    return Verdict(None, "unsure", top, tuple(ranked))


# --- units ---


@dataclass(frozen=True, slots=True)
class Quantity:
    """The ordered quantity in the item's own unit; ``rounded`` when whole packs were rounded up."""

    value: Decimal | None
    rounded: bool = False


def to_item_unit(qty: Decimal | None, unit: str | None, item: CatalogItem) -> Quantity:
    if qty is None or qty <= 0:
        return Quantity(None)
    if unit is None or unit == item.unit:
        value = qty
    elif unit in item.packs:
        value = qty * item.packs[unit]
    else:
        return Quantity(None)
    if item.unit in WHOLE and value != value.to_integral_value():
        return Quantity(value.to_integral_value(rounding=ROUND_CEILING), True)
    return Quantity(plain(value))


# --- analogues ---


def analogues(
    text: str,
    catalog: Catalog,
    *,
    chosen: CatalogItem | None = None,
    need: Decimal | None = None,
    limit: int = 3,
) -> list[Scored]:
    """What else fits the customer's own words: the products next in the ranking, of the chosen item's
    category, with enough in stock. The customer's sizes and ratings carry the ranking, the brand does not
    have to match."""
    out: list[Scored] = []
    for s in rank(text, catalog, limit=CANDIDATES + 4):
        other = s.item
        if chosen is not None and (other.id == chosen.id or other.category != chosen.category):
            continue
        if other.stock is not None and (other.stock <= 0 or (need is not None and other.stock < need)):
            continue
        if s.score < 0.45:
            continue
        out.append(s)
    return out[:limit]
