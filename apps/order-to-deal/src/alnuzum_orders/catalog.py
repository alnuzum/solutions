"""A company's catalog as the matching sees it, read from its CRM.

What a CRM keeps natively (name, article, price, unit, category, stock) is taken as is. What CRMs have
no field for lives in the product's description, one line each:

    Категория: Крепёж            (Category: …)    when the CRM has no product sections
    Единица: уп                  (Unit: …)        the unit the price is for, when the CRM's list lacks it
    Синонимы: саморез 3,5x35; шуруп 3.5x35        (Aliases: …)   other names customers use
    Упаковка: уп = 100 шт; кор = 10 уп            (Pack: …)      what a pack, a box or a coil holds
"""

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal

from alnuzum_crm.base import CRM
from alnuzum_crm.types import Product
from alnuzum_orders.text import norm, sku_key, tokens, unit_of

MAX_ITEMS = 5000
_LINE = re.compile(r"^\s*([^:]{2,20}):\s*(.+?)\s*$")
_KEYS = {
    "категория": "category", "category": "category",
    "единица": "unit", "unit": "unit",
    "синонимы": "synonyms", "aliases": "synonyms", "synonyms": "synonyms",
    "упаковка": "pack", "pack": "pack", "packs": "pack",
}  # fmt: skip
_PACK = re.compile(r"([^\s=;,]+)\s*=\s*([0-9]+(?:[.,][0-9]+)?)\s*([^\s;,]+)")


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: str
    name: str
    sku: str | None
    price: Decimal | None
    currency: str | None
    unit: str
    category: str | None
    stock: Decimal | None
    synonyms: tuple[str, ...] = ()
    #: how many base units one of a unit holds: {"шт": 0.01, "кор": 10} for a pack of 100 sold by the pack
    packs: dict[str, Decimal] = field(default_factory=dict)

    @property
    def phrases(self) -> tuple[str, ...]:
        return (self.name, *self.synonyms)

    @property
    def all_tokens(self) -> frozenset[str]:
        out: set[str] = set()
        for phrase in self.phrases:
            out.update(tokens(phrase))
        return frozenset(out)


def parse_description(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if m and (key := _KEYS.get(norm(m.group(1)))):
            out[key] = m.group(2)
    return out


def _packs(spec: str, base: str) -> dict[str, Decimal]:
    """«уп = 100 шт; кор = 10 уп» for an item sold by the pack -> {"шт": 1/100, "кор": 10}."""
    raw: dict[str, tuple[Decimal, str]] = {}
    for unit, number, of in _PACK.findall(spec):
        u, o = unit_of(unit), unit_of(of)
        if u and o:
            raw[u] = (Decimal(number.replace(",", ".")), o)
    factors: dict[str, Decimal] = {base: Decimal(1)}
    for _ in range(3):  # chains like кор -> уп -> шт
        for u, (n, of) in raw.items():
            if of in factors and u not in factors:
                factors[u] = n * factors[of]
            elif u in factors and of not in factors and n:
                factors[of] = factors[u] / n
    factors.pop(base)
    return factors


def item_of(product: Product) -> CatalogItem:
    about = parse_description(product.description)
    unit = unit_of(about.get("unit")) or unit_of(product.unit) or "шт"
    synonyms = tuple(s.strip() for s in about.get("synonyms", "").split(";") if s.strip())
    return CatalogItem(
        id=product.id,
        name=product.name.strip(),
        sku=product.sku,
        price=product.price,
        currency=product.currency,
        unit=unit,
        category=product.category or about.get("category"),
        stock=product.stock,
        synonyms=synonyms[:20],
        packs=_packs(about.get("pack", ""), unit),
    )


@dataclass(frozen=True)
class Catalog:
    items: tuple[CatalogItem, ...]
    currency: str | None
    has_stock: bool
    # what the CRM would not give, for the user: "w_b24_catalog" (a Bitrix24 webhook without «catalog»)
    warning: str | None = None

    def by_id(self, item_id: str) -> CatalogItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def by_sku(self) -> dict[str, CatalogItem]:
        return {sku_key(i.sku): i for i in self.items if i.sku and len(sku_key(i.sku)) >= 4}


async def read_catalog(crm: CRM, *, prefix: str | None = None) -> Catalog:
    """Every saleable product of the CRM (``prefix``: only articles starting with it, for the demo
    accounts that also hold test records)."""
    items: list[CatalogItem] = []
    cursor: str | None = None
    while len(items) < MAX_ITEMS:
        page = await crm.list_products(cursor=cursor, limit=200)
        for product in page.items:
            if prefix and not (product.sku or "").startswith(prefix):
                continue
            if product.name.strip():
                items.append(item_of(product))
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    currency = next((i.currency for i in items if i.currency), None)
    warning = "w_b24_catalog" if getattr(crm, "stock_denied", False) else None
    return Catalog(tuple(items), currency, any(i.stock is not None for i in items), warning)


class CatalogCache:
    """Catalogs read in the last ``ttl`` seconds, per connection: an order does not re-read 200 products."""

    def __init__(self, ttl: float = 600) -> None:
        self.ttl = ttl
        self._kept: dict[str, tuple[float, Catalog]] = {}

    def get(self, key: str) -> Catalog | None:
        hit = self._kept.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        return None

    def put(self, key: str, catalog: Catalog) -> None:
        if len(self._kept) > 200:
            self._kept.clear()
        self._kept[key] = (time.monotonic(), catalog)

    def drop(self, key: str) -> None:
        self._kept.pop(key, None)
