"""The company's price rules: a markup or a discount per category, a discount by order sum.

The model never sees prices and never sets them: every price in a deal comes from the CRM's catalog
through these rules.

    {"default": 0,                                    # percent for categories without their own rule
     "categories": [{"name": "Кабель и провод", "percent": 5}],   # > 0 markup, < 0 discount
     "volume": [{"from": 50000, "percent": 3}],       # discount from this order sum (before it)
     "round": "0.01"}                                 # step of a unit price: 0.01, 1, 100
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from alnuzum_orders.text import norm

MIN_PERCENT = Decimal(-90)
MAX_PERCENT = Decimal(300)
MAX_CATEGORIES = 50
MAX_TIERS = 5
ROUNDS = ("0.01", "1", "10", "100")


class RulesError(ValueError):
    """Rules that cannot be kept; ``code`` names the message for the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Tier:
    start: Decimal
    percent: Decimal


@dataclass(frozen=True)
class Rules:
    default: Decimal = Decimal(0)
    categories: dict[str, Decimal] = field(default_factory=dict)
    volume: tuple[Tier, ...] = ()
    round: Decimal = Decimal("0.01")

    def percent_for(self, category: str | None) -> Decimal:
        if category:
            key = norm(category)
            for name, percent in self.categories.items():
                if norm(name) == key:
                    return percent
        return self.default

    def tier_for(self, subtotal: Decimal) -> Tier | None:
        fits = [t for t in self.volume if subtotal >= t.start]
        return max(fits, key=lambda t: t.start) if fits else None

    def to_json(self) -> dict[str, Any]:
        return {
            "default": str(self.default),
            "categories": [{"name": k, "percent": str(v)} for k, v in self.categories.items()],
            "volume": [{"from": str(t.start), "percent": str(t.percent)} for t in self.volume],
            "round": str(self.round),
        }


def _dec(value: Any) -> Decimal:
    try:
        out = Decimal(str(value).strip().replace(",", ".").replace(" ", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        raise RulesError("e_rules_number") from None
    if not out.is_finite():
        raise RulesError("e_rules_number")
    return out


def _percent(value: Any) -> Decimal:
    out = _dec(value)
    if not MIN_PERCENT <= out <= MAX_PERCENT:
        raise RulesError("e_rules_percent")
    return out


def parse_rules(data: dict[str, Any] | None) -> Rules:
    """Rules from JSON (stored or posted); anything out of bounds is refused, not clipped."""
    data = data or {}
    cats: dict[str, Decimal] = {}
    for row in data.get("categories") or []:
        name = str(row.get("name", "")).strip()[:80]
        if not name or str(row.get("percent", "")).strip() == "":
            continue
        cats[name] = _percent(row["percent"])
    if len(cats) > MAX_CATEGORIES:
        raise RulesError("e_rules_many")
    tiers: list[Tier] = []
    for row in data.get("volume") or []:
        if str(row.get("from", "")).strip() == "" or str(row.get("percent", "")).strip() == "":
            continue
        start, percent = _dec(row["from"]), _percent(row["percent"])
        if start < 0 or not Decimal(0) <= percent <= Decimal(90):
            raise RulesError("e_rules_percent")
        tiers.append(Tier(start, percent))
    if len(tiers) > MAX_TIERS:
        raise RulesError("e_rules_many")
    step = str(data.get("round") or "0.01")
    if step not in ROUNDS:
        raise RulesError("e_rules_round")
    default = data.get("default")
    return Rules(
        default=_percent(default) if default not in (None, "") else Decimal(0),
        categories=cats,
        volume=tuple(sorted(tiers, key=lambda t: t.start)),
        round=Decimal(step),
    )


def demo_rules(currency: str | None) -> Rules:
    """What a demo visitor starts with: something visible on the default order."""
    scale = {"UZS": Decimal(155), "USD": Decimal("0.0125"), "EUR": Decimal("0.0115")}.get(
        (currency or "RUB").upper(), Decimal(1)
    )
    return Rules(
        default=Decimal(0),
        categories={"Кабель и провод": Decimal(5), "Инструмент": Decimal(-7)},
        volume=(Tier(Decimal(30000) * scale, Decimal(3)), Tier(Decimal(100000) * scale, Decimal(5))),
        round=Decimal(100) if (currency or "").upper() == "UZS" else Decimal("0.01"),
    )


CENT = Decimal("0.01")


def money(value: Decimal, step: Decimal) -> Decimal:
    """``value`` rounded to the step; a price never rounds to nothing: below half a step it keeps the
    cent (at least 0.01)."""
    out = (value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step
    if value > 0 and out <= 0:
        out = max(value.quantize(CENT, rounding=ROUND_HALF_UP), CENT)
    return out


@dataclass(frozen=True)
class PricedLine:
    key: int
    base: Decimal
    percent: Decimal
    unit_price: Decimal  # after the category rule and the volume discount
    quantity: Decimal

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class Priced:
    lines: tuple[PricedLine, ...]
    subtotal: Decimal  # after category rules, before the volume discount
    tier: Tier | None
    total: Decimal
    base_total: Decimal  # at catalog prices


def price(rows: list[tuple[int, Decimal, str | None, Decimal]], rules: Rules) -> Priced:
    """``rows``: (key, catalog price, category, quantity in the item's unit)."""
    staged = []
    for key, base, category, qty in rows:
        percent = rules.percent_for(category)
        unit = money(base * (1 + percent / 100), rules.round)
        staged.append((key, base, percent, unit, qty))
    subtotal = sum((u * q for _, _, _, u, q in staged), Decimal(0))
    tier = rules.tier_for(subtotal)
    cut = (1 - tier.percent / 100) if tier else Decimal(1)
    lines = tuple(PricedLine(k, b, p, money(u * cut, rules.round), q) for k, b, p, u, q in staged)
    return Priced(
        lines,
        subtotal,
        tier,
        sum((ln.total for ln in lines), Decimal(0)),
        sum((b * q for _, b, _, _, q in staged), Decimal(0)),
    )
